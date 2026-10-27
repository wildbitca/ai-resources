"""Doctor: dispatch ledger (last 7 days) against the routing matrix, and the weekly summary (ADR-0019 D7)."""
import time

import pytest

from ai_resources import dispatch_ledger as dl, doctor, run_ledger
from ai_resources.setup import ui


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "ledger_path", lambda: tmp_path / "dispatches.jsonl")
    monkeypatch.setattr(run_ledger, "ledger_path", lambda: tmp_path / "runs.jsonl")
    seen = {"ok": [], "warn": [], "detail": [], "info": []}
    for k in seen:
        monkeypatch.setattr(ui, k, seen[k].append)
    return seen


def _rec(role, cell, cause="", fb="", outcome="ok", age=3600, **kw):
    return dl.record(role, outcome, cell=cell, fallback_cause=cause, fallback_from=fb, tokens_in=100, tokens_out=50,
                     run_id="r1", now=time.time() - age, **kw)


def test_clean_ledger_is_ok(ledger):
    _rec("explore", "agy:gemini-3.8-flash-high")
    _rec("implementer", "claude-code:sonnet")
    assert doctor._check_matrix_actual() == 0 and not ledger["warn"] and "every dispatch" in ledger["ok"][0]


def test_uncaused_deviation_is_an_issue(ledger):
    _rec("explore", "claude-code:haiku")
    assert doctor._check_matrix_actual() == 1
    assert "explore ran claude-code:haiku" in ledger["warn"][0] and "no recorded fallback cause" in ledger["warn"][0]


def test_spill_with_cause_is_ok(ledger):
    _rec("explore", "claude-code:haiku", cause="pool-exhausted", fb="agy:gemini-3.8-flash-high")
    assert doctor._check_matrix_actual() == 0 and not ledger["warn"]


def test_checker_dynamic_rung_is_ok_and_off_ladder_is_not(ledger):
    _rec("verifier", "claude-code:opus")
    _rec("security-auditor", "agy:claude-opus-5-5")
    assert doctor._check_matrix_actual() == 0
    _rec("verifier", "claude-code:haiku")
    assert doctor._check_matrix_actual() == 1


def test_old_rows_blocked_rows_and_empty_ledger(ledger):
    assert doctor._check_matrix_actual() == 0 and "no dispatches" in ledger["ok"][0]
    _rec("explore", "claude-code:haiku", age=9 * 86400)           # outside the 7 days
    _rec("verifier", "", outcome="blocked", cause="no-independent-checker")
    assert doctor._check_matrix_actual() == 0


def test_weekly_summary_lines(ledger, monkeypatch):
    from ai_resources import credits, pool_usage
    monkeypatch.setattr(pool_usage, "snapshot", lambda: {"claude-subscription": {"label": "Max", "detail": "12%", "used_fraction": 0.12}})
    monkeypatch.setattr(credits, "lines", lambda: ["anthropic-api: declared $10.00, measured $1.00, remaining $9.00 (estimate)"])
    _rec("implementer", "claude-code:sonnet")
    _rec("explore", "claude-code:haiku", cause="timeout", fb="agy:gemini-3.8-flash-high")
    assert doctor._check_weekly_summary() == 0
    text = "\n".join(ledger["detail"])
    assert "claude-subscription" in text and "anthropic-api: declared" in text
    assert "claude-subscription: 2 dispatches, 300 tokens, 1 fallbacks" in text
    assert "top roles:" in text and "fallbacks by cause: timeout 1" in text and "runs: none recorded yet" in text


def test_dispatch_list_summary_shares_the_aggregation(ledger, capsys):
    import argparse
    _rec("explore", "claude-code:haiku")
    args = argparse.Namespace(run=None, since=None, last=0, summary=True)
    assert dl.cmd_list(args) == 1
    assert "DEVIATION explore" in capsys.readouterr().out
