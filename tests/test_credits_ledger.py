"""Credit ledger (credits.py): declared opening credit, measured spend from OpenClaw transcripts, remaining, the 80% alert.

Fixtures only: a scratch agents directory with hand-built transcript databases, a scratch credits.json. No provider
is called (neither exposes a balance API) and nothing under the real HOME is read. Run with: pytest tests/ -q
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ai_resources import billing, credits, pool_usage, verify

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
MS = lambda y, m, d: int(datetime(y, m, d, 12, tzinfo=timezone.utc).timestamp() * 1000)  # noqa: E731


def _event(model, provider, resp, inp=0, out=0, cr=0, cw=0) -> str:
    return json.dumps({"type": "message", "id": "e", "message": {
        "role": "assistant", "provider": provider, "model": model, "responseId": resp, "content": [],
        "usage": {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw, "cacheWrite1h": 0}}})


def _db(agents: Path, agent: str, rows: list[tuple]) -> Path:
    d = agents / agent / "agent"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "openclaw-agent.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table if not exists transcript_events (session_id text, seq integer, event_json text, "
                "event_zstd blob, created_at integer, event_utf8_bytes integer, navigation_json text)")
    for i, (event, created) in enumerate(rows):
        con.execute("insert into transcript_events (session_id, seq, event_json, event_zstd, created_at) values (?,?,?,?,?)",
                    ("s", i, event, None, created))
    con.commit()
    con.close()
    return db


@pytest.fixture
def env(tmp_path):
    return type("Env", (), {"agents": tmp_path / "agents", "ledger": tmp_path / "cfg" / "credits.json"})()


def _collect(e, **kw):
    return credits.collect(path=e.ledger, now=NOW, agents=e.agents, claude_roots=[], **kw)


# --- measuring ---------------------------------------------------------------------------------------------------

def test_spend_is_priced_per_pool_from_the_matching_provider_only(env):
    # sonnet-5-5: $2 in / $10 out per Mtok -> 1M in + 100k out = $3.00 ; gemini-3.8-flash: $0.75 in -> 1M in = $0.75
    _db(env.agents, "main", [
        (_event("claude-sonnet-5-5", "anthropic", "a", inp=1_000_000, out=100_000), MS(2026, 10, 20)),
        (_event("gemini-3.8-flash", "google", "g", inp=1_000_000), MS(2026, 10, 21)),
        (_event("claude-sonnet-5-5", "claude-cli", "m", inp=5_000_000), MS(2026, 10, 22))])        # Max: not USD
    credits.declare("anthropic-api", 200, on=date(2026, 10, 1), path=env.ledger, today=TODAY)
    credits.declare("gemini-api", 10, on=date(2026, 10, 1), path=env.ledger, today=TODAY)
    doc = _collect(env)
    a, g, mx = doc["pools"]["anthropic-api"], doc["pools"]["gemini-api"], doc["pools"]["claude-max"]
    assert (a["measured_usd"], a["remaining_usd"]) == (3.0, 197.0)
    assert (g["measured_usd"], g["remaining_usd"]) == (0.75, 9.25)
    assert mx["tokens"]["input"] == 5_000_000 and mx["measured_usd"] is None and mx["usd"] is None
    assert a["balance_api"] == "none" and "no provider balance API" in doc["note"]


def test_usage_before_the_declared_date_is_not_counted(env):
    _db(env.agents, "main", [(_event("claude-sonnet-5-5", "anthropic", "old", inp=1_000_000), MS(2026, 9, 1)),
                              (_event("claude-sonnet-5-5", "anthropic", "new", inp=1_000_000), MS(2026, 10, 20))])
    credits.declare("anthropic-api", 100, on=date(2026, 10, 1), path=env.ledger, today=TODAY)
    assert _collect(env)["pools"]["anthropic-api"]["measured_usd"] == 2.0


def test_an_unpriced_model_is_named_and_not_guessed(env):
    _db(env.agents, "main", [(_event("claude-newest-9", "anthropic", "x", inp=1_000_000), MS(2026, 10, 20))])
    credits.declare("anthropic-api", 100, on=date(2026, 10, 1), path=env.ledger, today=TODAY)
    e = _collect(env)["pools"]["anthropic-api"]
    assert e["measured_usd"] == 0.0 and e["unpriced_models"] == ["claude-newest-9"]


def test_measured_spend_never_decreases_when_old_transcripts_are_pruned(env):
    db = _db(env.agents, "main", [(_event("claude-sonnet-5-5", "anthropic", "a", inp=1_000_000), MS(2026, 10, 20))])
    credits.declare("anthropic-api", 100, on=date(2026, 10, 1), path=env.ledger, today=TODAY)
    assert _collect(env)["pools"]["anthropic-api"]["measured_usd"] == 2.0
    con = sqlite3.connect(db)
    con.execute("delete from transcript_events")
    con.commit()
    con.close()
    e = _collect(env)["pools"]["anthropic-api"]
    assert (e["measured_usd"], e["remaining_usd"]) == (2.0, 98.0)


def test_without_a_declared_amount_remaining_is_unknown_not_zero(env):
    _db(env.agents, "main", [(_event("claude-sonnet-5-5", "anthropic", "a", inp=1_000_000), MS(2026, 10, 20))])
    e = _collect(env)["pools"]["anthropic-api"]
    assert e["declared_usd"] is None and e["remaining_usd"] is None and e["measured_usd"] == 2.0
    assert "declared: unknown" in "\n".join(credits.lines(env.ledger))


# --- the file ----------------------------------------------------------------------------------------------------

def test_the_ledger_is_written_atomically_with_mode_0600_and_keeps_the_legacy_keys(env):
    env.ledger.parent.mkdir(parents=True)
    env.ledger.write_text(json.dumps({"prepaid_usd": 12.5, "updated": "2026-10-20"}))
    os.chmod(env.ledger, 0o644)
    _collect(env)
    assert stat.S_IMODE(env.ledger.stat().st_mode) == 0o600
    assert [p.name for p in env.ledger.parent.iterdir()] == ["credits.json"]          # no temp file left behind
    doc = json.loads(env.ledger.read_text())
    assert doc["prepaid_usd"] == 12.5 and doc["version"] == credits.LEDGER_VERSION and doc["updated"] == "2026-10-27"


def test_a_failed_write_leaves_the_previous_ledger_intact(env, monkeypatch):
    credits.declare("gemini-api", 10, path=env.ledger, today=TODAY)
    before = env.ledger.read_text()
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        credits.write_ledger({"x": 1}, env.ledger)
    assert env.ledger.read_text() == before and [p.name for p in env.ledger.parent.iterdir()] == ["credits.json"]


# --- declaring (setup, once per pool) ----------------------------------------------------------------------------

def test_setup_records_the_opening_credit_once_flag_then_env_then_file(env):
    said = credits.record_opening(env={"AI_RESOURCES_CREDIT_ANTHROPIC_USD": "50", "AI_RESOURCES_CREDIT_GEMINI_USD": "7"},
                                  flags={"anthropic-api": 80.0}, path=env.ledger, today=TODAY)
    assert said["anthropic-api"].startswith("declared $80.00 (flag)") and said["gemini-api"].startswith("declared $7.00 (env)")
    again = credits.record_opening(env={"AI_RESOURCES_CREDIT_ANTHROPIC_USD": "999"}, path=env.ledger, today=date(2026, 11, 1))
    assert set(again.values()) == {"already declared"}
    pools = json.loads(env.ledger.read_text())["pools"]
    assert pools["anthropic-api"]["declared_usd"] == 80.0 and pools["anthropic-api"]["declared_on"] == "2026-10-27"


def test_auto_takes_the_amount_already_in_credits_json_else_leaves_it_unknown_without_asking(env):
    env.ledger.parent.mkdir(parents=True)
    env.ledger.write_text(json.dumps({"prepaid_usd": 33.0}))

    def never(_pool):
        raise AssertionError("--auto must not ask")

    said = credits.record_opening(env={}, auto=True, ask=never, path=env.ledger, today=TODAY)
    assert said["anthropic-api"].startswith("declared $33.00 (credits.json)") and said["gemini-api"] == "declared: unknown"
    pools = json.loads(env.ledger.read_text())["pools"]
    assert pools["gemini-api"]["declared_usd"] is None


def test_an_interactive_run_asks_only_for_the_pools_still_unknown(env):
    asked = []
    said = credits.record_opening(env={"AI_RESOURCES_CREDIT_GEMINI_USD": "5"}, auto=False, path=env.ledger, today=TODAY,
                                  ask=lambda pool: (asked.append(pool), 120.0)[1])
    assert asked == ["anthropic-api"] and said["anthropic-api"].endswith("(operator)")


def test_credits_set_declares_and_restarts_the_measured_window(env):
    credits.declare("anthropic-api", 100, on=date(2026, 10, 1), path=env.ledger, today=TODAY)
    entry = credits.declare("anthropic-api", 300, path=env.ledger, today=TODAY)
    assert (entry["declared_usd"], entry["declared_on"], entry["measured_usd"]) == (300.0, "2026-10-27", 0.0)
    with pytest.raises(ValueError):
        credits.declare("openai-api", 1, path=env.ledger)
    with pytest.raises(ValueError):
        credits.declare("gemini-api", -1, path=env.ledger)


# --- the 80% alert -----------------------------------------------------------------------------------------------

def _ledger_with(env, declared, measured, pool="anthropic-api"):
    env.ledger.parent.mkdir(parents=True, exist_ok=True)
    env.ledger.write_text(json.dumps({"version": 2, "updated": TODAY.isoformat(), "pools": {pool: {
        "declared_usd": declared, "declared_on": "2026-10-01", "measured_usd": measured,
        "remaining_usd": None if declared is None else declared - measured}}}))
    os.chmod(env.ledger, 0o600)


@pytest.mark.parametrize("measured,codes", [(79.0, []), (80.0, ["credit_pool_80"]), (99.0, ["credit_pool_80"]),
                                            (100.0, ["credit_pool_exhausted"]), (130.0, ["credit_pool_exhausted"])])
def test_alert_thresholds(env, measured, codes):
    _ledger_with(env, 100.0, measured)
    assert [c for c, _s, _m in credits.alerts(env.ledger)] == codes


def test_no_alert_without_a_declared_amount(env):
    _ledger_with(env, None, 500.0)
    assert credits.alerts(env.ledger) == []


def test_billing_alerts_carry_the_pool_alert_and_the_summary_shows_the_ledger(env, monkeypatch):
    _ledger_with(env, 100.0, 85.0)
    monkeypatch.setattr(billing, "credits_path", lambda: env.ledger)
    snap = billing.snapshot(today=TODAY)
    assert snap["credit"]["state"] == "known"
    assert [a.code for a in billing.alerts_for(snap)] == ["credit_pool_80"]
    assert any("anthropic-api: declared $100.00" in ln for ln in billing.summary_lines(snap))


def test_verify_reports_the_80_percent_alert_as_a_warning(env, monkeypatch):
    _ledger_with(env, 100.0, 90.0, pool="gemini-api")
    monkeypatch.setattr(billing, "credits_path", lambda: env.ledger)
    found = verify.credit_findings()
    assert [f.level for f in found] == ["warn"] and "gemini-api" in found[0].message


def test_doctor_pool_usage_uses_the_ledger_before_the_spend_feed(env, monkeypatch):
    _ledger_with(env, 200.0, 50.0)
    monkeypatch.setattr(billing, "credits_path", lambda: env.ledger)
    snap = pool_usage.snapshot(env={"ANTHROPIC_API_CREDITS_USD": "1"})
    assert snap["anthropic-api"]["used_fraction"] == 0.25 and snap["anthropic-api"]["source"] == "credits.json"


# --- the command -------------------------------------------------------------------------------------------------

def test_cli_set_show_and_collect_check_exit_code(env, monkeypatch, capsys):
    monkeypatch.setattr(billing, "credits_path", lambda: env.ledger)
    monkeypatch.setattr(credits, "agents_dir", lambda: env.agents)
    _db(env.agents, "main", [(_event("claude-sonnet-5-5", "anthropic", "a", inp=9_000_000), MS(2026, 10, 26))])
    ns = lambda **k: argparse.Namespace(**k)  # noqa: E731
    assert credits.cmd_credits(ns(credits_action="set", pool="anthropic-api", usd=20.0, on="2026-10-01")) == 0
    rc = credits.cmd_credits(ns(credits_action="collect", check=True, quiet=True))
    out = capsys.readouterr().out
    assert rc == 3 and "ALERT anthropic-api" in out                     # $18 of $20 is 90%
    assert credits.cmd_credits(ns(credits_action="show")) == 0
    assert credits.cmd_credits(ns(credits_action="set", pool="gemini-api", usd=-5, on=None)) == 2


# --- setup (apply-time, no wizard screen) -------------------------------------------------------------------------

def test_setup_records_the_flag_amounts_once_and_stays_silent_otherwise(env, monkeypatch):
    from ai_resources.setup import state, ui, wizard
    monkeypatch.setattr(billing, "credits_path", lambda: env.ledger)
    monkeypatch.setattr(credits, "agents_dir", lambda: env.agents)
    seen: list[str] = []
    for name in ("ok", "warn", "detail", "info"):
        monkeypatch.setattr(ui, name, seen.append)
    monkeypatch.setattr(ui, "text", lambda *a, **k: pytest.fail("the recommended run must not ask"))
    monkeypatch.setattr(wizard, "_CREDIT_FLAGS", {"anthropic-api": 75.0})
    wizard._credits_and_keys(state.SetupState())
    wizard._credits_and_keys(state.SetupState())                                    # second run: nothing new
    pools = json.loads(env.ledger.read_text())["pools"]
    assert pools["anthropic-api"]["declared_usd"] == 75.0 and pools["gemini-api"]["declared_usd"] is None
    assert [ln for ln in seen if "credit anthropic-api" in ln] == ["credit anthropic-api: declared $75.00 (flag)"]
