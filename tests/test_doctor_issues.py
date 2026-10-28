"""doctor: every issue has a stable name and an owner (setup | operator | history); the banner counts what is open.

Measured 2026-10-28 on the live host after a setup: "10 issue(s) found; Re-run `ai-resources setup` to fix" when
no open issue was something setup could fix (a LITELLM_MASTER_KEY demanded in single-model mode, an MCP-child
overflow, six history rows of the dispatch ledger, two legacy scratch dirs). Fakes and tmp_path only.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from ai_resources import dispatch_ledger as dl, doctor, run_ledger
from ai_resources.setup import state, ui


class _Console:
    def print(self, *_a, **_k):
        pass


@pytest.fixture(autouse=True)
def _fresh_registry():
    doctor._ISSUES.clear()
    yield
    doctor._ISSUES.clear()


@pytest.fixture
def seen(monkeypatch):
    out = {k: [] for k in ("ok", "warn", "detail", "info", "error", "banner")}
    for k in ("ok", "warn", "detail", "info", "error"):
        monkeypatch.setattr(ui, k, out[k].append)
    monkeypatch.setattr(ui, "banner", lambda title, subtitle="", version="": out["banner"].append((title, subtitle)))
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _Console())
    return out


def _names():
    return {i.name: i for i in doctor._ISSUES}


# --- the banner -------------------------------------------------------------------------------------------

def test_no_issue_is_the_green_banner():
    assert doctor.banner_text([]) == ("✓ Doctor: all checks passed", "")


def test_a_setup_fixable_issue_says_to_rerun_setup():
    title, sub = doctor.banner_text([doctor.Issue("state-missing", "x", doctor.SETUP)])
    assert "1 issue(s) open" in title and "Re-run `ai-resources setup`" in sub


def test_only_operator_issues_never_say_rerun_setup_and_name_the_commands():
    issues = [doctor.Issue("scratch-legacy-cache", "m", doctor.OPERATOR, "ai-resources doctor --fix --apply"),
              doctor.Issue("mcp-children", "m", doctor.OPERATOR, "ai-resources openclaw reconcile")]
    title, sub = doctor.banner_text(issues)
    assert "2 issue(s) open" in title
    assert "setup" not in sub.lower()
    assert "ai-resources doctor --fix --apply" in sub


def test_history_rows_are_not_open_issues():
    issues = [doctor.Issue("matrix-deviation", "m", doctor.HISTORY)]
    title, sub = doctor.banner_text(issues)
    assert "all checks passed" in title and "1 historical" in sub and "setup" not in sub.lower()


def test_the_label_names_who_fixes_it():
    assert doctor.Issue("a", "m", doctor.SETUP).label() == "fixed by: setup"
    assert doctor.Issue("a", "m", doctor.OPERATOR, "cmd x").label() == "fixed by: operator (cmd x)"
    assert doctor.Issue("a", "m", doctor.HISTORY).label().startswith("fixed by: nobody")


# --- the checks register stable names -----------------------------------------------------------------------

def test_matrix_deviations_are_one_named_history_issue_not_six(monkeypatch, tmp_path, seen):
    monkeypatch.setattr(dl, "ledger_path", lambda: tmp_path / "dispatches.jsonl")
    monkeypatch.setattr(run_ledger, "ledger_path", lambda: tmp_path / "runs.jsonl")
    for i in range(3):
        for role in ("verifier", "security-auditor"):
            dl.record(role, "ok", cell="claude-code:claude-opus-5-5", fallback_cause="", fallback_from="", tokens_in=1,
                      tokens_out=1, run_id=f"r{i}", now=time.time() - 3600 - i)
    doctor._check_matrix_actual()
    issue = _names()["matrix-deviation"]
    assert issue.fixed_by == doctor.HISTORY and "6" in issue.message
    assert len(doctor._ISSUES) == 1


def test_mcp_overflow_is_fixed_by_the_drained_restart_setup_runs(monkeypatch, seen):
    from ai_resources import openclaw_reconcile as rec
    monkeypatch.setattr(rec, "gateway_mcp_children", lambda *a, **k: {"engram": 21, "supabase": 33})
    doctor._check_mcp_children(runner=lambda *a, **k: (0, ""))
    issue = _names()["mcp-children"]
    assert issue.fixed_by == doctor.SETUP


def test_legacy_scratch_is_operator_work_with_the_exact_command(monkeypatch, tmp_path, seen):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = cache / "gcp-bootstrap-2026-10-22.sh"
    old.write_text("#!/bin/sh\n", encoding="utf-8")
    import os
    os.utime(old, (time.time() - 5 * 86400,) * 2)
    monkeypatch.setattr(doctor.residue, "openclaw_legacy_report", lambda apply=False: [
        {"path": str(tmp_path / "x.py"), "bytes": 10, "protected": False, "in_use": False, "age_s": 9 * 86400,
         "removed": False}])
    monkeypatch.setattr(doctor.residue, "sizes", lambda: {})
    monkeypatch.setattr(doctor.scratch, "entries", lambda: [])
    monkeypatch.setattr(doctor.scratch, "scan_results_bytes", lambda: 0)
    doctor._check_scratch(argparse.Namespace(fix=False, apply=False), cache_root=cache)
    names = _names()
    assert names["scratch-legacy-openclaw"].fixed_by == doctor.OPERATOR
    assert names["scratch-legacy-cache"].fixed_by == doctor.OPERATOR
    assert names["scratch-legacy-cache"].command == "ai-resources doctor --fix --apply"


# --- cmd_doctor ---------------------------------------------------------------------------------------------

def _quiet_checks(monkeypatch):
    for name in ("_check_antigravity_quota", "_check_antigravity_lane", "_check_mcp_children", "_check_credit_ledger",
                 "_check_billing", "_check_pool_usage", "_check_matrix_actual", "_check_gemini_tools",
                 "_check_weekly_summary", "_check_keys", "_check_poll_share", "_check_vendor_pins",
                 "_check_openclaw_cost", "_check_scratch"):
        monkeypatch.setattr(doctor, name, lambda *a, **k: 0)
    monkeypatch.setattr(doctor.verify, "run_all", lambda *a, **k: [])


def _run(monkeypatch, tmp_path, s, **checks):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "xdg" / "ai-resources"
    cfg.mkdir(parents=True)
    (cfg / "setup-state.yaml").write_text("{}\n", encoding="utf-8")
    env = cfg / ".env"
    env.write_text("ANTHROPIC_API_KEY=x\n", encoding="utf-8")
    env.chmod(0o600)
    monkeypatch.setattr(state, "load", lambda: s)
    monkeypatch.setattr(state, "state_path", lambda: cfg / "setup-state.yaml")
    monkeypatch.setattr(state, "env_path", lambda: env)
    monkeypatch.setattr(state, "executors_path", lambda: cfg / "executors.yaml")
    _quiet_checks(monkeypatch)
    for name, fn in checks.items():
        monkeypatch.setattr(doctor, name, fn)
    return doctor.cmd_doctor(argparse.Namespace(skip_smoke=True, no_collect=True))


def test_single_model_mode_does_not_demand_a_litellm_master_key(monkeypatch, tmp_path, seen):
    s = state.SetupState()
    s.mode = "single-model"
    s.cockpits["claude"] = state.CockpitState(configured=True)
    rc = _run(monkeypatch, tmp_path, s)
    assert "litellm-master-key" not in _names()
    assert rc == 0 and "all checks passed" in seen["banner"][-1][0]


def test_multi_model_mode_still_names_a_missing_master_key_as_setup_work(monkeypatch, tmp_path, seen):
    s = state.SetupState()
    s.mode = "multi-model"
    s.cockpits["claude"] = state.CockpitState(configured=True)
    rc = _run(monkeypatch, tmp_path, s)
    assert rc == 1
    assert _names()["litellm-master-key"].fixed_by == doctor.SETUP
    assert "Re-run `ai-resources setup`" in seen["banner"][-1][1]


def test_every_open_issue_is_listed_by_name_with_its_owner(monkeypatch, tmp_path, seen):
    s = state.SetupState()
    s.mode = "single-model"
    s.cockpits["claude"] = state.CockpitState(configured=True)
    rc = _run(monkeypatch, tmp_path, s, _check_scratch=lambda *a, **k: doctor._flag(
        "scratch-legacy-cache", "1 legacy scratch entry", doctor.OPERATOR, "ai-resources doctor --fix --apply"))
    listing = "\n".join(seen["warn"] + seen["detail"] + seen["error"])
    assert rc == 1
    assert "scratch-legacy-cache" in listing and "fixed by: operator (ai-resources doctor --fix --apply)" in listing
    assert "setup" not in seen["banner"][-1][1].lower()


def test_exit_code_ignores_history_rows(monkeypatch, tmp_path, seen):
    s = state.SetupState()
    s.mode = "single-model"
    s.cockpits["claude"] = state.CockpitState(configured=True)
    assert _run(monkeypatch, tmp_path, s, _check_matrix_actual=lambda *a, **k: doctor._flag(
        "matrix-deviation", "6 deviations", doctor.HISTORY)) == 0
    assert "matrix-deviation" in "\n".join(seen["detail"] + seen["warn"])
