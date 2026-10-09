"""The `ai-resources models` verbs. Fakes only (Env is shared with test_models_update)."""
from __future__ import annotations

import json

import pytest

from ai_resources import cli, models, models_cmd
from ai_resources import model_pins as mp
from ai_resources.setup import ui
from test_models_update import Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    return e


def run(*argv):
    return cli.main(["models", *argv])


def test_help_lists_every_verb(capsys):
    with pytest.raises(SystemExit):
        cli.main(["models", "--help"])
    out = capsys.readouterr().out
    for verb in models_cmd.VERBS:
        assert verb in out


def test_check_with_an_auto_candidate_is_11_and_writes_nothing(env, capsys):
    assert run("check") == 11
    assert not env.overlay.exists() and env.patches == [] and env.smokes == []
    assert "sonnet" in capsys.readouterr().out


def test_check_with_only_approvals_pending_is_10(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    assert run("check") == 10
    assert not e.overlay.exists()


def test_check_refreshes_only_when_asked(env):
    run("check")
    assert not any("refresh" in a for a in env.runner_calls)
    env.runner_calls.clear()
    run("check", "--refresh")
    assert any("refresh" in a for a in env.runner_calls)


def test_approve_persists_and_the_next_plan_applies_it(tmp_path, monkeypatch, capsys):
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    assert run("approve", "haiku", "claude-haiku-5-5") == 0
    assert mp.load_overlay(e.overlay)["approvals"] == {"anthropic:haiku": "claude-haiku-5-5"}
    r = models.run_update(models.Options(check=True), e.deps())
    assert {p.cls: p.decision for p in r.proposals}["haiku"] == "approved" and r.rc == 11
    assert run("revoke", "haiku") == 0
    assert "anthropic:haiku" not in mp.load_overlay(e.overlay)["approvals"]


def test_approve_validates_class_and_id(env, capsys):
    assert run("approve", "sonnet", "claude-opus-5") == 2
    assert run("approve", "nope", "claude-opus-5") == 2
    assert not env.overlay.exists()


def test_pin_unpin_and_exclude_persist(env):
    assert run("pin", "sonnet", "claude-sonnet-5") == 0
    ov = mp.load_overlay(env.overlay)
    assert ov["pins"]["anthropic:sonnet"] == "claude-sonnet-5" and ov["policy"]["slots"]["anthropic:sonnet"]["frozen"] is True
    assert run("exclude", "claude-opus-5-5") == 0
    assert mp.load_overlay(env.overlay)["policy"]["exclude"] == ["claude-opus-5-5"]
    assert run("unpin", "sonnet") == 0
    ov = mp.load_overlay(env.overlay)
    assert "anthropic:sonnet" not in ov["pins"] and "anthropic:sonnet" not in ov["policy"]["slots"]


def test_a_pinned_class_is_not_updated(env):
    run("pin", "sonnet", "claude-sonnet-5")
    assert run("update", "--unattended", "--class", "sonnet") == 0 and env.patches == []


def test_update_unattended_never_prompts(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("prompted")
    monkeypatch.setattr(ui, "select", boom)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    assert run("update", "--unattended", "--class", "sonnet") == 0
    assert env.restarts == 1


def test_update_without_a_tty_never_prompts(env, monkeypatch):
    monkeypatch.setattr(ui, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert run("update", "--class", "sonnet") == 0


def test_update_interactive_offers_the_pending_ones(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    asked = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: asked.append(msg) or (
        "Approve and apply" if msg.startswith("haiku") else "Skip"))
    run("update", "--no-restart")
    assert len(asked) == 3 and mp.load_overlay(e.overlay)["approvals"] == {"anthropic:haiku": "claude-haiku-5-5"}


def test_update_json_output(env, capsys):
    run("update", "--dry-run", "--json", "--class", "sonnet")
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "dry_run" and data["applied"]["anthropic:sonnet"] == ["claude-sonnet-5", "claude-sonnet-5-5"]


def test_rollback_verb(env):
    run("update", "--unattended", "--class", "sonnet")
    env.restarts = 0
    assert run("rollback") == 0
    assert "anthropic:sonnet" not in mp.load_overlay(env.overlay)["pins"]
    assert env.doc["agents"]["entries"]["main"]["model"]["primary"] == "anthropic/claude-sonnet-5"
    assert run("rollback") == 4                      # nothing left to undo


def test_status_reports_drift_and_agy(env, capsys):
    mp.save_overlay({"pins": {"sonnet": "claude-sonnet-5-5"}}, env.overlay)
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "DRIFT agents.entries.main.model.primary" in out
    assert "gemini-3.8-flash-low" in out and "pool" in out


def test_check_prints_up_to_date_rows(env, capsys):
    run("check")
    out = capsys.readouterr().out
    assert "fable" in out and "up to date" in out


def test_check_json_has_a_current_row_for_fable(env, capsys):
    run("check", "--json")
    rows = json.loads(capsys.readouterr().out)["proposals"]
    assert any(r["cls"] == "fable" and r["decision"] == "current" for r in rows)


# --- provider-agnostic verbs and findings (S8) ----------------------------------------------------

import dataclasses

from ai_resources import model_accounts as ma
from ai_resources.selection import ProviderSel, Selection, SlotSel


def _google_selection():
    s = Selection(shape="multi-provider", smoke_path="litellm")
    s.providers.update({"anthropic": ProviderSel(), "google": ProviderSel()})
    s.slots.update({"anthropic:sonnet": SlotSel(ref="anthropic/claude-sonnet-5"),
                    "google:gemini-flash": SlotSel(ref="google/gemini-3.7-flash")})
    return s


def test_pin_by_class_equals_pin_by_slot(tmp_path, monkeypatch):
    results = []
    for argv in (["pin", "opus", "claude-opus-5"], ["pin", "--slot", "anthropic:opus", "claude-opus-5"]):
        (tmp_path / f"r{len(results)}").mkdir()
        e = Env(tmp_path / f"r{len(results)}", monkeypatch)
        monkeypatch.setattr(models_cmd, "get_deps", e.deps)
        monkeypatch.setattr(mp, "overlay_path", lambda e=e: e.overlay)
        assert run(*argv) == 0
        ov = mp.load_overlay(e.overlay)
        ov.pop("history")
        results.append(ov)
    assert results[0] == results[1]


def test_pin_and_approve_validate_a_non_claude_slot(env, capsys):
    assert run("pin", "--slot", "google:gemini-flash", "gemini-3.8-flash") == 0
    assert mp.load_overlay(env.overlay)["pins"]["google:gemini-flash"] == "gemini-3.8-flash"
    assert run("approve", "--slot", "google:gemini-flash", "gemini-3.5-flash-lite") == 2     # another family
    assert run("approve", "--slot", "nope:thing", "x") == 2
    assert "slot" in capsys.readouterr().err


def test_update_slot_filter_alias(env):
    assert run("update", "--unattended", "--slot", "anthropic:sonnet") == 0
    assert env.restarts == 1


def test_status_lists_every_slot_with_its_answer_and_the_skipped_providers(env, monkeypatch, capsys):
    deps = dataclasses.replace(env.deps(), selection=_google_selection,
                               accounts=lambda: {"anthropic": ma.Account(True, ["synthetic"]), "google": ma.Account(False, [], "no credentials")})
    monkeypatch.setattr(models_cmd, "get_deps", lambda: deps)
    ov = mp.empty_overlay()
    ov["policy"]["slots"]["google:gemini-flash"] = {"answer": "always", "never_ids": ["gemini-9-flash"]}
    mp.save_overlay(ov, env.overlay)
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "google:gemini-flash" in out and "answer always, 1 id(s) refused" in out
    assert "anthropic:sonnet" in out and "answer always" in out
    assert "openai     skipped: not enabled" in out
    assert "google     skipped: no credentials" in out


def test_status_reports_drift_for_a_stale_google_reference(env, monkeypatch, capsys):
    env.doc["agents"]["entries"]["ai"]["model"] = {"primary": "google/gemini-3.7-flash"}
    deps = dataclasses.replace(env.deps(), selection=_google_selection,
                               read_config=lambda: env.doc, accounts=lambda: {})
    monkeypatch.setattr(models_cmd, "get_deps", lambda: deps)
    run("status")
    out = capsys.readouterr().out
    assert "DRIFT agents.entries.ai.model.primary = google/gemini-3.7-flash (effective: google/gemini-3.8-flash)" in out


def test_findings_name_lost_credentials_missing_ids_and_preview_pins():
    sel = _google_selection()
    ov = {"state": {"catalog_missing": ["google:gemini-flash"]},
          "pins": {"google:gemini-flash": "gemini-3.9-flash"}}
    found = models.model_findings({"models": [], "timers": []}, ov, selection=sel,
                                  accounts={"google": ma.Account(False, [], "no credentials"), "anthropic": ma.Account(True, ["synthetic"])})
    texts = [m for _l, m, _r in found]
    assert any("google is enabled but has lost its credentials" in t for t in texts)
    assert any("google:gemini-flash: the running model id is no longer listed" in t for t in texts)
    sel.slots["google:gemini-pro"] = SlotSel(ref="google/gemini-3.1-pro-preview")
    found = models.model_findings({"models": [], "timers": []}, {}, selection=sel)
    assert any("google:gemini-pro runs gemini-3.1-pro-preview, a preview id" in m for _l, m, _r in found)


def test_a_claude_only_host_gets_no_selection_findings():
    assert models.model_findings({"models": [], "timers": []}, {}) == []
