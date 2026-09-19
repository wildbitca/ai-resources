"""`ai-resources openclaw doctor`: the drained `openclaw doctor --fix` (pitfall T01).

The whole sequence runs against a fake runner and a recording marker, so the order of every
step is asserted and nothing can stop a real gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

DOCTOR_OK = "...\nDoctor complete.\n"


class Fake:
    """Runner + marker + sleep in one place, sharing a single event list."""

    def __init__(self, drains_on_poll: int | None = 1, doctor_out: str = DOCTOR_OK, doctor_rc: int = 0,
                 health_ok_on: int | None = 1, marker_present: bool = False, boom_at: str = ""):
        self.events: list[str] = []
        self.drains_on_poll, self.doctor_out, self.doctor_rc = drains_on_poll, doctor_out, doctor_rc
        self.health_ok_on, self.boom_at = health_ok_on, boom_at
        self.polls = self.health_polls = 0
        self.marker = self
        self._present = marker_present
        self.path = pathlib.Path("/fake/watchdog.off")

    # marker protocol
    def exists(self):
        return self._present

    def touch(self):
        self.events.append("touch")
        self._present = True

    def remove(self):
        self.events.append("rm")
        self._present = False

    def sleep(self, _s):
        self.events.append("sleep")

    def __call__(self, argv, **_kw):
        tail = argv[-1]
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.events.append("stop")
            if self.boom_at == "stop":
                raise self.boom_exc
            return 0, ""
        if argv[:3] == ["systemctl", "--user", "start"]:
            self.events.append("start")
            return 0, ""
        if argv[:3] == ["systemctl", "--user", "show"]:
            self.polls += 1
            self.events.append("drain")
            drained = self.drains_on_poll is not None and self.polls >= self.drains_on_poll
            return 0, "[not set]" if drained else "7"
        if argv[1:] == ["doctor", "--fix"]:
            self.events.append("doctor")
            if self.boom_at == "doctor":
                raise self.boom_exc
            return self.doctor_rc, self.doctor_out
        if argv[1:] == ["sessions", "cleanup", "--all-agents"]:
            self.events.append("cleanup")
            return 0, "cleaned 3 sessions"
        if argv[1:] == ["health"]:
            self.health_polls += 1
            self.events.append("health")
            ok = self.health_ok_on is not None and self.health_polls >= self.health_ok_on
            return (0 if ok else 1), ""
        raise AssertionError(f"unexpected command {argv}")

    boom_exc: BaseException = RuntimeError("boom")

    def run(self, **kw):
        lines: list[str] = []
        kw.setdefault("drain_timeout", 12)
        kw.setdefault("drain_interval", 3)
        kw.setdefault("health_timeout", 20)
        kw.setdefault("health_interval", 5)
        code = host.doctor(self, sleep=self.sleep, marker=self.marker, openclaw_bin="openclaw",
                           out=lines.append, **kw)
        return code, lines

    def ops(self) -> list[str]:
        """The events without sleeps, repeated polls collapsed."""
        out: list[str] = []
        for e in self.events:
            if e == "sleep" or (out and out[-1] == e and e in ("drain", "health")):
                continue
            out.append(e)
        return out


# --- AC-6.1 ------------------------------------------------------------------------------------------

def test_the_call_order_is_exactly_touch_stop_drain_doctor_rm_start_health():
    fake = Fake(drains_on_poll=1)
    code, _ = fake.run()
    assert code == 0
    assert fake.ops() == ["touch", "stop", "drain", "doctor", "rm", "start", "health"]


def test_doctor_runs_once_and_strictly_after_the_fourth_drain_poll():
    fake = Fake(drains_on_poll=4)
    code, _ = fake.run(drain_timeout=30)
    assert code == 0
    assert fake.polls == 4
    assert fake.events.count("doctor") == 1
    last_poll = max(i for i, e in enumerate(fake.events) if e == "drain")
    assert fake.events.index("doctor") > last_poll


def test_the_drain_is_polled_with_sleeps_never_a_single_fixed_wait():
    fake = Fake(drains_on_poll=4)
    fake.run(drain_timeout=30)
    before_doctor = fake.events[:fake.events.index("doctor")]
    assert before_doctor.count("sleep") == 3  # a sleep between polls, none after the successful one


def test_the_optional_session_cleanup_happens_inside_the_drained_window():
    fake = Fake()
    fake.run(cleanup_sessions=True)
    assert fake.ops() == ["touch", "stop", "drain", "doctor", "cleanup", "rm", "start", "health"]


# --- AC-6.2 ------------------------------------------------------------------------------------------

def test_a_cgroup_that_never_drains_skips_doctor_still_restarts_and_exits_non_zero():
    fake = Fake(drains_on_poll=None)
    code, lines = fake.run()
    assert code == host.EXIT_NOT_DRAINED
    assert "doctor" not in fake.events
    assert fake.ops()[-3:] == ["rm", "start", "health"]
    assert any("skipped" in ln for ln in lines)


def test_a_doctor_that_does_not_complete_is_reported_after_the_gateway_is_back():
    fake = Fake(doctor_out="aborted: ownership or manager identity changed", doctor_rc=1)
    code, _ = fake.run()
    assert code == host.EXIT_DOCTOR_FAILED
    assert fake.ops()[-3:] == ["rm", "start", "health"]


def test_a_gateway_that_does_not_come_back_is_exit_five():
    fake = Fake(health_ok_on=None)
    code, lines = fake.run()
    assert code == host.EXIT_NOT_HEALTHY
    assert fake.health_polls == 4  # 20s / 5s: bounded, not forever
    assert "healthy=no" in lines


def test_health_is_polled_until_it_answers_not_a_fixed_sleep():
    fake = Fake(health_ok_on=3)
    code, _ = fake.run()
    assert code == 0 and fake.health_polls == 3


# --- AC-6.3: every exit path restores the window ------------------------------------------------------------

@pytest.mark.parametrize("boom", [RuntimeError("boom"), KeyboardInterrupt()], ids=["exception", "ctrl-c"])
@pytest.mark.parametrize("where", ["stop", "doctor"])
def test_the_marker_is_removed_and_the_gateway_started_after_an_interruption(where, boom):
    fake = Fake(boom_at=where)
    fake.boom_exc = boom
    with pytest.raises(type(boom)):
        fake.run()
    assert fake._present is False
    assert fake.events.count("rm") == 1 and fake.events.count("start") == 1
    assert fake.events.index("rm") < fake.events.index("start")


# --- AC-6.4 ----------------------------------------------------------------------------------------------------

def test_an_open_window_is_refused_without_stopping_anything():
    fake = Fake(marker_present=True)
    code, lines = fake.run()
    assert code == host.EXIT_REFUSED
    assert fake.events == []
    assert any("--force" in ln for ln in lines)


def test_force_takes_over_a_stale_window():
    fake = Fake(marker_present=True)
    code, _ = fake.run(force=True)
    assert code == 0 and fake.ops()[0] == "touch"


def test_dry_run_prints_the_sequence_and_touches_nothing():
    fake = Fake()
    code, lines = fake.run(dry_run=True)
    assert code == 0 and fake.events == []
    text = "\n".join(lines)
    assert text.index("watchdog.off") < text.index("stop") < text.index("doctor --fix") < text.index("start")


# --- environment -------------------------------------------------------------------------------------------------

def test_systemctl_gets_the_runtime_dir_and_bus_that_a_timer_lacks(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    env = host.systemd_env()
    assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={env['XDG_RUNTIME_DIR']}/bus"


def test_the_cli_registers_doctor_with_its_flags():
    from ai_resources import cli
    args = cli.build_parser().parse_args(["openclaw", "doctor", "--force", "--drain-timeout", "5",
                                          "--cleanup-sessions"])
    assert args.func is host.cmd_doctor and args.force and args.drain_timeout == 5 and args.cleanup_sessions


def test_the_real_marker_is_created_and_removed(tmp_path):
    m = host.Marker(tmp_path / "sub" / "watchdog.off")
    assert not m.exists()
    m.touch()
    assert m.exists()
    m.remove()
    m.remove()  # removing twice is fine
    assert not m.exists()
