"""ai-resources doctor — sub-check 4d, Antigravity's two weekly quota pools.

The repo's first doctor test. Narrow by design: monkeypatch `_agy_quota.read_usage`
and `detection._which_extra`, never the subprocess (the parser itself is covered in
tests/test_agy_quota.py). Run with: pytest tests/test_doctor_quota.py -q
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import doctor  # noqa: E402
from ai_resources.setup import state, detection, ui  # noqa: E402
from ai_resources.setup.cockpits import _agy_quota  # noqa: E402

FIXTURE = (REPO / "tests" / "fixtures" / "agy_usage.txt").read_text()
FIXTURE_POOLS = _agy_quota.parse_usage(FIXTURE)


def _state(applied: bool, model: str = "") -> state.SetupState:
    s = state.SetupState()
    s.openclaw.antigravity_applied = applied
    s.openclaw.model = model
    return s


class _FakeConsole:
    """Stand-in for `ui.console()`, which hard-requires rich (absent in CI).

    `rule` (not just `print`) because `cmd_doctor` calls `ui.section`, which is
    styled output rich provides no plain-text fallback for.
    """

    def print(self, *_a, **_k):
        pass

    def rule(self, *_a, **_k):
        pass


def _drive_cmd_doctor(monkeypatch, s: state.SetupState) -> None:
    """Run the real `cmd_doctor` entry point against a stubbed environment.

    `require_deps` and `console` are stubbed because CI runs pytest without
    rich/questionary installed (see tests/test_openclaw_cockpit.py's identical
    `_FakeConsole`), not because cmd_doctor's own behaviour is being narrowed.
    """
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    monkeypatch.setattr(state, "load", lambda: s)
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))


def test_antigravity_applied_false_means_no_agy_call_at_all(monkeypatch):
    # Drive the real entry point (cmd_doctor) rather than re-implementing its
    # `if s.openclaw.antigravity_applied:` guard (doctor.py:210) here — a test that
    # re-implements the guard passes even if the production guard is removed.
    calls = []
    monkeypatch.setattr(doctor, "_check_antigravity_quota", lambda s: calls.append(1) or 0)

    _drive_cmd_doctor(monkeypatch, _state(applied=False))

    assert calls == []


def test_antigravity_applied_true_means_the_guard_is_taken(monkeypatch):
    # Mirror of the test above: with the guard's condition true, the sub-check must
    # run exactly once. Together the two pin the guard in both directions, so either
    # removing it or inverting it fails one of them.
    calls = []
    monkeypatch.setattr(doctor, "_check_antigravity_quota", lambda s: calls.append(1) or 0)

    _drive_cmd_doctor(monkeypatch, _state(applied=True, model="gemini-3.8-flash-low"))

    assert calls == [1]


def test_gemini_applied_model_at_zero_percent_is_one_issue_with_pool_reset_and_remedy(monkeypatch):
    monkeypatch.setattr(detection, "_which_extra", lambda _b: "/usr/local/bin/agy")
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_k: (FIXTURE_POOLS, ""))
    errors, details = [], []
    monkeypatch.setattr(ui, "error", lambda msg: errors.append(msg))
    monkeypatch.setattr(ui, "detail", lambda msg: details.append(msg))
    monkeypatch.setattr(ui, "warn", lambda msg: (_ for _ in ()).throw(AssertionError("no warn expected")))

    s = _state(applied=True, model="gemini-3.8-flash-low")
    issues = doctor._check_antigravity_quota(s)

    assert issues == 1
    assert len(errors) == 1
    assert _agy_quota.POOL_GEMINI in errors[0] and "0%" in errors[0]
    assert "2026-09-24T15:45:01Z" in errors[0]  # the error line carries the reset stamp
    assert any("Claude and GPT" in d for d in details)  # remedy names the other pool
    assert any("voice" in d.lower() for d in details)  # agy voice shares the Gemini pool


def test_same_usage_but_applied_model_is_claude_sonnet_4_6_reports_both_pools_no_warning(monkeypatch):
    monkeypatch.setattr(detection, "_which_extra", lambda _b: "/usr/local/bin/agy")
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_k: (FIXTURE_POOLS, ""))
    calls = {"warn": 0, "error": 0, "detail": 0}
    monkeypatch.setattr(ui, "error", lambda msg: calls.__setitem__("error", calls["error"] + 1))
    monkeypatch.setattr(ui, "warn", lambda msg: calls.__setitem__("warn", calls["warn"] + 1))
    monkeypatch.setattr(ui, "detail", lambda msg: calls.__setitem__("detail", calls["detail"] + 1))

    s = _state(applied=True, model="claude-sonnet-4-6")
    issues = doctor._check_antigravity_quota(s)

    assert issues == 0
    assert calls["warn"] == 0 and calls["error"] == 0
    assert calls["detail"] == 2  # one detail line per pool, both graded "ok"


def test_near_limit_on_the_applied_pool_is_one_issue_as_a_warning(monkeypatch):
    monkeypatch.setattr(detection, "_which_extra", lambda _b: "/usr/local/bin/agy")
    near_limit = [_agy_quota.Pool(_agy_quota.POOL_GEMINI, 5, "2027-01-01T00:00:00Z")]
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_k: (near_limit, ""))
    warnings = []
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))
    monkeypatch.setattr(ui, "error", lambda msg: (_ for _ in ()).throw(AssertionError("no error expected")))
    monkeypatch.setattr(ui, "detail", lambda *_a, **_k: None)

    s = _state(applied=True, model="gemini-3.8-flash-low")
    issues = doctor._check_antigravity_quota(s)

    assert issues == 1
    assert len(warnings) == 1
    assert "5%" in warnings[0]


def test_agy_missing_skips_with_zero_issues_never_a_crash(monkeypatch):
    monkeypatch.setattr(detection, "_which_extra", lambda _b: "")
    calls = []
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_k: calls.append(1) or ([], ""))
    details = []
    monkeypatch.setattr(ui, "detail", lambda msg: details.append(msg))

    s = _state(applied=True, model="gemini-3.8-flash-low")
    issues = doctor._check_antigravity_quota(s)

    assert issues == 0
    assert calls == [], "read_usage must not run when agy is absent"
    assert any("agy" in d.lower() for d in details)


def test_read_failure_warns_with_the_reason_zero_issues(monkeypatch):
    monkeypatch.setattr(detection, "_which_extra", lambda _b: "/usr/local/bin/agy")
    monkeypatch.setattr(_agy_quota, "read_usage", lambda **_k: ([], "agy -p /usage timed out"))
    warnings, details = [], []
    monkeypatch.setattr(ui, "warn", lambda msg: warnings.append(msg))
    monkeypatch.setattr(ui, "detail", lambda msg: details.append(msg))
    monkeypatch.setattr(ui, "error", lambda msg: (_ for _ in ()).throw(AssertionError("no error expected")))

    s = _state(applied=True, model="gemini-3.8-flash-low")
    issues = doctor._check_antigravity_quota(s)

    assert issues == 0
    assert len(warnings) == 1 and "timed out" in warnings[0]
    assert any("Singleflight" in d for d in details)


# --- ADR-0014 D2: billing and quota awareness (sub-check 4g) ---------------------------------------------------------

def _capture_ui(monkeypatch):
    seen = {"warn": [], "info": [], "detail": [], "ok": [], "error": []}
    for name in seen:
        monkeypatch.setattr(ui, name, lambda msg, _n=name: seen[_n].append(msg))
    return seen


def _write_billing(monkeypatch, tmp_path, billing_type="prepaid", week=100.0, five=0.0, credits=None):
    import json
    from ai_resources import billing
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"oauthAccount": {"billingType": billing_type, "emailAddress": "who@example.com"}}))
    sc = tmp_path / "limits.json"
    sc.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": five, "resets_at": 4102444800},
                                              "seven_day": {"used_percentage": week, "resets_at": 4102444800}}}))
    monkeypatch.setattr(billing, "claude_json_path", lambda: cj)
    monkeypatch.setattr(billing, "sidecar_path", lambda: sc)
    if credits is not None:
        cp = tmp_path / "credits.json"
        cp.write_text(json.dumps(credits))
        cp.chmod(0o600)
        monkeypatch.setattr(billing, "credits_path", lambda: cp)


def test_doctor_warns_when_prepaid_and_weekly_quota_is_exhausted(monkeypatch, tmp_path):
    seen = _capture_ui(monkeypatch)
    _write_billing(monkeypatch, tmp_path, "prepaid", week=100.0)

    assert doctor._check_billing() == 0           # a warning about money, not a defect of the install
    warns = " | ".join(seen["warn"])
    assert "prepaid" in warns.lower() and "Weekly Claude quota exhausted" in warns
    assert "who@example.com" not in " ".join(sum(seen.values(), []))


def test_doctor_is_quiet_on_a_subscription_with_headroom(monkeypatch, tmp_path):
    seen = _capture_ui(monkeypatch)
    _write_billing(monkeypatch, tmp_path, "stripe_subscription", week=20.0, five=10.0)

    doctor._check_billing()
    assert seen["warn"] == []
    assert any("nothing to flag" in m for m in seen["ok"])


def test_doctor_billing_with_missing_files_says_unknown_and_does_not_crash(monkeypatch):
    seen = _capture_ui(monkeypatch)

    assert doctor._check_billing() == 0
    assert "auth mode: unknown" in seen["detail"]
    assert any(m.startswith("credit: unknown") for m in seen["detail"])


def test_doctor_billing_with_corrupt_files_does_not_crash(monkeypatch, tmp_path):
    from ai_resources import billing
    seen = _capture_ui(monkeypatch)
    for name in ("claude_json_path", "sidecar_path", "credits_path"):
        bad = tmp_path / f"{name}.json"
        bad.write_text("\x00{broken")
        monkeypatch.setattr(billing, name, lambda b=bad: b)

    assert doctor._check_billing() == 0
    assert seen["warn"] == []


def test_cmd_doctor_runs_the_billing_check(monkeypatch):
    calls = []
    monkeypatch.setattr(doctor, "_check_billing", lambda: calls.append(1) or 0)
    _drive_cmd_doctor(monkeypatch, _state(applied=False))
    assert calls == [1]
