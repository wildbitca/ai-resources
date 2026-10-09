"""The host findings of `ai-resources verify` (S9): read-only, one collector, canned probes.

The runner answers from canned output: nothing here reaches systemd, ss or openclaw. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import os
import pathlib

import pytest

from ai_resources import openclaw_host as host
from ai_resources.setup import state
from ai_resources.setup.cockpits import _openclaw_host as section

NOW = 1_800_000_000.0


class HostRunner:
    def __init__(self, *, active="active", enabled="enabled", health_rc=0, timers_enabled=True,
                 ss="LISTEN 0 511 127.0.0.1:18789 0.0.0.0:*\n", timeout_on=()):
        self.active, self.enabled, self.health_rc = active, enabled, health_rc
        self.timers_enabled, self.ss, self.timeout_on = timers_enabled, ss, timeout_on
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        a = list(argv)
        self.calls.append(a)
        if any(t in a for t in self.timeout_on):
            return 124, "timed out"
        if a[:3] == ["systemctl", "--user", "is-active"]:
            return (0, "active") if self.active == "active" else (3, self.active)
        if a[:3] == ["systemctl", "--user", "is-enabled"]:
            if a[3] == host.GATEWAY_UNIT:
                return (0, self.enabled) if self.enabled.startswith("enabled") else (1, self.enabled)
            return (0, "enabled") if self.timers_enabled else (1, "disabled")
        if a[:3] == ["systemctl", "--user", "show"]:
            return 0, "-"
        if a[0] == "loginctl":
            return 0, "yes"
        if a[0] == "ss":
            return 0, self.ss
        if os.path.basename(a[0]) == "openclaw" and a[1] == "health":
            return self.health_rc, ""
        raise AssertionError(f"verify must not run {argv}")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(home / ".openclaw" / "openclaw.json"))
    backups = tmp_path / "backups"
    (backups / "daily").mkdir(parents=True)
    f = backups / "daily" / "openclaw-1.tar.gz"
    f.write_bytes(b"x")
    import time
    os.utime(f, (time.time() - 3600, time.time() - 3600))
    env_file = home / ".openclaw" / "kit-host.env"
    env_file.write_text(f"OPENCLAW_BACKUP_DIR={backups}\n", encoding="utf-8")
    monkeypatch.setattr(host, "HOST_ENV_PATH", env_file)
    monkeypatch.setattr(host, "resolve_openclaw_bin", lambda: "/opt/oc/openclaw")
    return env_file


def _run(runner, **openclaw_state):
    s = state.SetupState()
    for k, v in openclaw_state.items():
        setattr(s.openclaw, k, v)
    return section.verify({"state": s}, runner=runner)


def _levels(findings):
    return [(f.level, f.message) for f in findings if f.level != "ok"]


def test_empty_offbox_command_is_exactly_one_warn_with_the_literal_env_line(box):
    found = _run(HostRunner())
    warns = [f for f in found if f.level == "warn"]
    assert len(warns) == 1
    assert "OPENCLAW_OFFBOX_LIST_CMD" in warns[0].remedy and "kit-host.env" in warns[0].remedy
    assert "will not choose a destination" in warns[0].remedy
    assert not [f for f in found if f.level == "error"]


def test_a_configured_offbox_command_is_neither_run_nor_warned(box):
    box.write_text(box.read_text() + "OPENCLAW_OFFBOX_LIST_CMD=remote-ls bucket\n", encoding="utf-8")
    runner = HostRunner()
    found = _run(runner)
    assert not [f for f in found if f.level == "warn"]
    assert not any(c[0] == "remote-ls" for c in runner.calls)   # no network call from verify


def test_an_inactive_unit_is_one_error_and_health_is_not_a_second(box):
    found = _run(HostRunner(active="inactive", health_rc=1))
    errors = [f for f in found if f.level == "error"]
    assert len(errors) == 1 and host.GATEWAY_UNIT in errors[0].message


def test_a_disabled_unit_is_an_error(box):
    assert [f.level for f in _run(HostRunner(enabled="disabled")) if f.level == "error"] == ["error"]


def test_health_that_does_not_answer_on_a_running_unit_is_an_error(box):
    errors = [f for f in _run(HostRunner(health_rc=1)) if f.level == "error"]
    assert len(errors) == 1 and "health" in errors[0].message


def test_a_disabled_timer_is_a_warn_naming_it(box):
    found = _run(HostRunner(timers_enabled=False))
    named = [f for f in found if f.level == "warn" and host.TIMER_NAMES[0] in f.message]
    assert named


def test_a_timeout_is_at_most_a_warn_never_an_error(box):
    for probe in ("health", "is-active"):
        found = _run(HostRunner(timeout_on=(probe,)))
        assert not [f for f in found if f.level == "error"], (probe, _levels(found))
    assert any("could not measure" in f.message for f in _run(HostRunner(timeout_on=("health",))))


def test_a_wildcard_listener_is_a_warn(box):
    found = _run(HostRunner(ss="LISTEN 0 511 0.0.0.0:18789 0.0.0.0:*\n"))
    assert any(f.level == "warn" and "all interfaces" in f.message for f in found)


def test_a_missing_daily_backup_is_a_warn(box):
    for p in (box.parent.parent.parent / "backups" / "daily").glob("*"):
        p.unlink()
    assert any(f.level == "warn" and "no daily backup" in f.message for f in _run(HostRunner()))


def test_a_machine_without_the_gateway_unit_skips_the_host_checks(box):
    found = _run(HostRunner(active="inactive", enabled="Failed to get unit file state"))
    assert [f.level for f in found] == ["ok"] and "skipped" in found[0].message


def test_verify_never_stops_restarts_or_runs_doctor_and_writes_nothing(box):
    runner = HostRunner()
    before = {p: p.stat().st_mtime_ns for p in box.parent.parent.rglob("*") if p.is_file()}
    _run(runner)
    for call in runner.calls:
        assert not {"stop", "restart", "start", "enable", "disable", "doctor", "--fix"} & set(call), call
    assert {p: p.stat().st_mtime_ns for p in box.parent.parent.rglob("*") if p.is_file()} == before


# --- T39: stability bundle and the watchdog unit ------------------------------------------------------------------

import datetime as _dt
import json
import time

FIX = pathlib.Path(__file__).parent / "fixtures" / "stability"
B_1006 = "openclaw-stability-2026-10-06T17-14-23-353Z-1051-gateway.stop_shutdown_timeout.json"
B_0918 = "openclaw-stability-2026-09-18T01-25-07-371Z-485771-gateway.stop_shutdown_timeout.json"


def _bundle(box, fixture: str, age_days: float):
    """The fixture bundle, regenerated `age_days` ago (name and generatedAt both move)."""
    home = box.parent.parent
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=age_days)
    data = json.loads((FIX / fixture).read_text(encoding="utf-8"))
    data["generatedAt"] = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    name = "openclaw-stability-" + when.strftime("%Y-%m-%dT%H-%M-%S-000Z") + "-" + fixture.split("Z-", 1)[1]
    d = home / ".openclaw" / "logs" / "stability"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data), encoding="utf-8")


def _stability_warns(found):
    return [f for f in found if f.level == "warn" and "gateway stop" in f.message]


def test_a_recent_stop_held_by_blocked_tool_calls_is_one_warn_naming_the_tools_and_t39(box):
    _bundle(box, B_1006, 1)
    warns = _stability_warns(_run(HostRunner()))
    assert len(warns) == 1
    assert ">=6 blocked tool calls" in warns[0].message and "Bash" in warns[0].message
    assert "T39" in warns[0].remedy and "openclaw-operations" in warns[0].remedy


def test_the_same_bundle_after_the_window_is_ok(box):
    _bundle(box, B_1006, 8)
    assert not _stability_warns(_run(HostRunner()))


def test_a_bundle_without_stalls_is_ok(box):
    _bundle(box, B_0918, 1)
    assert not _stability_warns(_run(HostRunner()))


def test_an_unreadable_bundle_is_a_warn(box):
    d = box.parent.parent / ".openclaw" / "logs" / "stability"
    d.mkdir(parents=True)
    (d / "openclaw-stability-2099-01-01T00-00-00-000Z-1-gateway.stop_close_failed.json").write_text("{x", encoding="utf-8")
    assert any(f.level == "warn" and "stability bundle" in f.message for f in _run(HostRunner()))


def _custom_bundle(box, reason: str, stalled: list[dict], age_days: float = 1):
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=age_days)
    d = box.parent.parent / ".openclaw" / "logs" / "stability"
    d.mkdir(parents=True, exist_ok=True)
    name = "openclaw-stability-" + when.strftime("%Y-%m-%dT%H-%M-%S-000Z") + f"-1-{reason}.json"
    (d / name).write_text(json.dumps({"version": 1, "reason": reason,
                                      "generatedAt": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                      "snapshot": {"events": stalled}}), encoding="utf-8")


def test_a_recent_close_failed_bundle_with_stalls_is_a_warn(box):
    _custom_bundle(box, "gateway.stop_close_failed",
                   [{"type": "session.stalled", "reason": "blocked_tool_call", "toolName": "Bash"}] * 2)
    warns = _stability_warns(_run(HostRunner()))
    assert len(warns) == 1 and ">=2 blocked tool calls" in warns[0].message


def test_active_work_without_progress_only_uses_the_stalled_sessions_wording(box):
    _custom_bundle(box, "gateway.stop_shutdown_timeout",
                   [{"type": "session.stalled", "reason": "active_work_without_progress"}] * 3)
    warns = _stability_warns(_run(HostRunner()))
    assert len(warns) == 1
    assert ">=3 stalled sessions" in warns[0].message and "blocked tool calls" not in warns[0].message


def test_a_recent_bundle_with_a_non_matching_reason_stays_ok(box):
    _custom_bundle(box, "gateway.restart_requested",
                   [{"type": "session.stalled", "reason": "blocked_tool_call", "toolName": "Bash"}] * 2)
    assert not _stability_warns(_run(HostRunner()))


def test_no_stability_finding_is_ever_an_error(box):
    _bundle(box, B_1006, 1)
    assert not [f for f in _run(HostRunner()) if f.level == "error"]


def _watchdog_unit(box, exec_start: str):
    d = box.parent.parent.parent / "xdg" / "systemd" / "user"
    d.mkdir(parents=True)
    (d / "openclaw-watchdog.service").write_text(f"[Service]\nExecStart={exec_start}\n", encoding="utf-8")


def test_a_stale_watchdog_execstart_is_one_warn_naming_install_units(box):
    _watchdog_unit(box, "/home/u/.local/bin/openclaw-watchdog.sh")
    warns = [f for f in _run(HostRunner()) if f.level == "warn" and "watchdog" in f.message]
    assert len(warns) == 1 and "install-units" in warns[0].remedy


def test_the_kit_watchdog_execstart_is_not_a_warn(box):
    _watchdog_unit(box, f"/bin/bash {host.kit_root()}/scripts/openclaw/openclaw-watchdog.sh")
    assert not [f for f in _run(HostRunner()) if f.level == "warn" and "watchdog" in f.message]


# --- the models update findings --------------------------------------------------------------------------------

def _model_findings(box, overlay, config=None):
    import json
    from ai_resources import model_pins
    cfg = box.parent / "openclaw.json"
    cfg.write_text(json.dumps(config or {"agents": {"defaults": {"model": {"primary": "anthropic/claude-haiku-4-5"}},
                                                     "entries": {"main": {"model": {"primary": "anthropic/claude-sonnet-5"}}}}}),
                   encoding="utf-8")
    model_pins.save_overlay(overlay)
    return _run(HostRunner())


def test_drift_between_the_config_and_the_pins_is_one_warn(box):
    found = _model_findings(box, {"pins": {"sonnet": "claude-sonnet-5-5"}})
    drift = [f for f in found if "lag the pins" in f.message]
    assert len(drift) == 1 and drift[0].level == "warn" and "main" in drift[0].message


def test_matching_references_give_no_model_finding(box):
    found = _model_findings(box, {})
    assert not [f for f in found if "pins" in f.message or "models update" in f.message]


def test_pending_approvals_are_advisory(box):
    found = _model_findings(box, {"pending": {"haiku": {"to": "claude-haiku-5-5", "reason": "major jump"}}})
    pend = [f for f in found if "await approval" in f.message]
    assert len(pend) == 1 and pend[0].level == "ok" and "models approve haiku claude-haiku-5-5" in pend[0].remedy


@pytest.mark.parametrize("result,level", [("rolled_back", "warn"), ("rollback_failed", "error")])
def test_a_failed_last_run_is_surfaced(box, result, level):
    found = _model_findings(box, {"state": {"last_result": result}})
    assert [f.level for f in found if "models update" in f.message] == [level]


def test_a_stale_last_run_warns_only_while_the_timer_is_enabled(box):
    old = {"state": {"last_run": "2020-01-01T00:00:00+00:00"}}
    assert any("has not run" in f.message for f in _model_findings(box, old))
    found = _run_disabled(box, old)
    assert not any("has not run" in f.message for f in found)


def _run_disabled(box, overlay):
    from ai_resources import model_pins
    model_pins.save_overlay(overlay)
    return _run(HostRunner(timers_enabled=False))
