"""The host findings of `ai-resources verify` (S9): read-only, one collector, canned probes.

The runner answers from canned output: nothing here reaches systemd, ss or openclaw. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import os

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
