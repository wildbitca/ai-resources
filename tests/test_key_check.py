"""Gemini / Anthropic key check (key_check.py): probe the gateway env key and the OpenClaw profile key, repair a rejected env key.

A fake HTTP callable answers by key, so no provider is called; the env file and the state store live under tmp_path.
Key values must never appear in any line the check prints. Run with: pytest tests/ -q
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat

import pytest

from ai_resources import doctor, key_check as kc, openclaw_reconcile, openclaw_host
from ai_resources.setup import state, ui

BAD_GEMINI, GOOD_GEMINI = "AIza-env-rejected-0001", "AIza-profile-works-0002"
GOOD_ANTHROPIC = "sk-ant-profile-0003"


def fake_http(good: set[str], seen: list | None = None):
    def http(url, headers):
        key = next((v for k, v in headers.items() if k in ("x-goog-api-key", "x-api-key")), "")
        if seen is not None:
            seen.append((url, sorted(headers)))
        return 200 if key in good else 401
    return http


@pytest.fixture
def host(tmp_path):
    oc = tmp_path / ".openclaw"
    (oc / "state").mkdir(parents=True)
    env = oc / "gateway.systemd.env"
    env.write_text(f"GH_TOKEN=ghp_x\nGEMINI_API_KEY={BAD_GEMINI}\nSLACK_BOT_TOKEN=xoxb-1\n")
    os.chmod(env, 0o600)

    def store(profiles: dict, secrets: dict | None = None):
        con = sqlite3.connect(oc / "state" / "openclaw.sqlite")
        con.execute("create table if not exists config_machine_state (state_key text primary key, value_json text, "
                    "updated_at_ms integer)")
        con.execute("create table if not exists secret_store_entries (scope_kind text, scope_id text, name text, "
                    "value text, kind text, deleted_at_ms integer)")
        con.execute("delete from config_machine_state")
        con.execute("insert into config_machine_state values ('authProfiles.store', ?, 1)",
                    (json.dumps({"version": 1, "profiles": profiles}),))
        for name, value in (secrets or {}).items():
            con.execute("insert into secret_store_entries values ('team', '', ?, ?, 'secret', null)", (name, value))
        con.commit()
        con.close()

    store({"google:default": {"type": "api_key", "provider": "google", "key": GOOD_GEMINI},
           "anthropic:default": {"type": "api_key", "provider": "anthropic", "key": GOOD_ANTHROPIC}})
    return type("Host", (), {"env": env, "db": oc / "state" / "openclaw.sqlite", "store": staticmethod(store)})()


def run(host, good, **kw):
    return kc.check(http=fake_http(set(good)), env_path=host.env, db=host.db, now=1_792_900_000, **kw)


def by(results, prov):
    return next(r for r in results if r.provider == prov)


# --- probing -----------------------------------------------------------------------------------------------------

def test_a_cheap_models_list_call_is_made_per_key_and_never_a_billed_one():
    seen: list = []
    kc.probe("gemini", "k1", fake_http({"k1"}, seen))
    kc.probe("anthropic", "k2", fake_http({"k2"}, seen))
    assert [u for u, _h in seen] == ["https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                     "https://api.anthropic.com/v1/models?limit=1"]
    assert seen[0][1] == ["x-goog-api-key"] and seen[1][1] == ["anthropic-version", "x-api-key"]


@pytest.mark.parametrize("code,expected", [(200, "ok"), (401, "rejected"), (403, "rejected"), (400, "rejected"),
                                           (429, "error"), (500, "error"), (0, "error")])
def test_probe_outcomes(code, expected):
    assert kc.probe("gemini", "k", lambda u, h: code) == expected


def test_no_key_is_absent_and_makes_no_call():
    assert kc.probe("gemini", None, lambda u, h: pytest.fail("called")) == "absent"


def test_profile_key_resolves_a_literal_key_and_a_store_reference(host):
    assert kc.profile_key("google:default", host.db) == GOOD_GEMINI
    host.store({"google:default": {"type": "api_key", "keyRef": {"source": "store", "provider": "default", "id": "G_KEY"}}},
               secrets={"G_KEY": "from-store"})
    assert kc.profile_key("google:default", host.db) == "from-store"
    assert kc.profile_key("anthropic:default", host.db) is None
    assert kc.profile_key("google:default", host.db.with_name("missing.sqlite")) is None


# --- the repair --------------------------------------------------------------------------------------------------

def test_a_rejected_env_key_is_rewritten_from_the_working_profile_key(host):
    results = run(host, {GOOD_GEMINI, GOOD_ANTHROPIC})
    g = by(results, "gemini")
    assert (g.env, g.profile, g.fixed) == ("rejected", "ok", True)
    assert g.line() == "gemini key: env rejected / profile ok / mismatch fixed"
    text = host.env.read_text()
    assert f"GEMINI_API_KEY={GOOD_GEMINI}" in text and BAD_GEMINI not in text
    assert "GH_TOKEN=ghp_x" in text and "SLACK_BOT_TOKEN=xoxb-1" in text            # every other line kept
    assert stat.S_IMODE(host.env.stat().st_mode) == 0o600
    backup = host.env.parent / os.path.basename(g.backup)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600 and BAD_GEMINI in backup.read_text()
    assert kc.restart_owed(results)


def test_no_line_the_check_produces_contains_a_key_value(host):
    results = run(host, {GOOD_GEMINI, GOOD_ANTHROPIC})
    shown = "\n".join(r.line() + r.backup for r in results)
    for secret in (BAD_GEMINI, GOOD_GEMINI, GOOD_ANTHROPIC):
        assert secret not in shown


def test_both_keys_ok_changes_nothing(host):
    host.env.write_text(f"GEMINI_API_KEY={GOOD_GEMINI}\n")
    before = host.env.stat().st_mtime_ns
    results = run(host, {GOOD_GEMINI, GOOD_ANTHROPIC})
    assert by(results, "gemini").line() == "gemini key: env ok / profile ok"
    assert host.env.stat().st_mtime_ns == before and not kc.restart_owed(results)
    assert not list(host.env.parent.glob("*.bak-keycheck-*"))


def test_an_absent_env_key_is_reported_and_not_added(host):
    results = run(host, {GOOD_GEMINI, GOOD_ANTHROPIC})
    a = by(results, "anthropic")
    assert (a.env, a.profile, a.fixed) == ("absent", "ok", False) and "ANTHROPIC_API_KEY" not in host.env.read_text()


def test_a_rejected_profile_key_is_a_problem_and_nothing_is_written(host):
    before = host.env.read_text()
    results = run(host, set())
    assert by(results, "gemini").problem and by(results, "gemini").line() == "gemini key: env rejected / profile rejected"
    assert host.env.read_text() == before


def test_no_answer_from_the_provider_never_triggers_a_rewrite(host):
    before = host.env.read_text()
    results = kc.check(http=lambda u, h: 0, env_path=host.env, db=host.db)
    assert host.env.read_text() == before and not any(r.fixed for r in results)


def test_fix_false_reports_the_mismatch_and_leaves_the_file(host):
    before = host.env.read_text()
    g = by(run(host, {GOOD_GEMINI}, fix=False), "gemini")
    assert g.mismatch and not g.fixed and g.line().endswith("mismatch NOT fixed") and host.env.read_text() == before


def test_two_repairs_in_the_same_second_keep_both_backups(host):
    host.env.write_text(f"GEMINI_API_KEY={BAD_GEMINI}\nANTHROPIC_API_KEY=sk-bad\n")
    results = run(host, {GOOD_GEMINI, GOOD_ANTHROPIC})
    assert all(r.fixed for r in results)
    backups = sorted(p.name for p in host.env.parent.glob("gateway.systemd.env.bak-keycheck-*"))
    assert len(backups) == 2


def test_a_refused_value_is_not_written(host):
    with pytest.raises(ValueError):
        kc.rewrite_env_entry("GEMINI_API_KEY", "a\nb", host.env)
    assert BAD_GEMINI in host.env.read_text()


# --- the restart the repair owes ---------------------------------------------------------------------------------

def test_the_env_file_newer_than_the_gateway_owes_a_restart_and_older_does_not(host):
    mtime = host.env.stat().st_mtime
    assert kc.env_newer_than_gateway(mtime - 100, host.env) == kc.RESTART_REASON
    assert kc.env_newer_than_gateway(mtime + 100, host.env) == ""
    assert kc.env_newer_than_gateway(None, host.env) == ""


def test_reconcile_adds_an_env_changed_reason_through_the_existing_drained_window(host, monkeypatch):
    monkeypatch.setattr(kc, "gateway_env_path", lambda: host.env)
    monkeypatch.setattr(openclaw_host, "gateway_started_at", lambda *a, **k: host.env.stat().st_mtime - 50)
    lines: list = []
    runner = lambda argv, **k: (1, "")  # noqa: E731 - no systemd: no MCP count
    out = openclaw_reconcile.reconcile(runner, plugin_dir="/nope", stale_reason=lambda *a: None, dry_run=True,
                                       check_env=True, out=lines.append)
    assert [r.kind for r in out.reasons] == ["env-changed"] and any("dry run" in ln for ln in lines)
    quiet = openclaw_reconcile.reconcile(runner, plugin_dir="/nope", stale_reason=lambda *a: None, dry_run=True)
    assert quiet.reasons == []                                             # opt-in: other callers are unchanged


# --- the doctor line ---------------------------------------------------------------------------------------------

def test_doctor_reports_the_three_states_without_values(host, monkeypatch):
    monkeypatch.setattr(kc, "gateway_env_path", lambda: host.env)
    monkeypatch.setattr(kc, "state_db_path", lambda: host.db)
    monkeypatch.setattr(kc, "_default_http", fake_http({GOOD_GEMINI, GOOD_ANTHROPIC}))
    seen: list[str] = []
    for name in ("ok", "warn", "detail", "info"):
        monkeypatch.setattr(ui, name, seen.append)
    issues = doctor._check_keys(state.SetupState(), argparse.Namespace(no_fix_keys=False))
    assert issues == 0
    joined = "\n".join(seen)
    assert "gemini key: env rejected / profile ok / mismatch fixed" in joined
    assert "anthropic key: env absent / profile ok" in joined
    for secret in (BAD_GEMINI, GOOD_GEMINI, GOOD_ANTHROPIC):
        assert secret not in joined


def test_doctor_with_no_fix_keys_leaves_the_env_and_counts_the_rejected_key(host, monkeypatch):
    monkeypatch.setattr(kc, "gateway_env_path", lambda: host.env)
    monkeypatch.setattr(kc, "state_db_path", lambda: host.db)
    monkeypatch.setattr(kc, "_default_http", fake_http({GOOD_GEMINI, GOOD_ANTHROPIC}))
    before = host.env.read_text()
    for name in ("ok", "warn", "detail", "info"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    assert doctor._check_keys(state.SetupState(), argparse.Namespace(no_fix_keys=True)) == 1
    assert host.env.read_text() == before
