"""The graceful restart (ADR-0004): gates, action, retries, MarkerGuard on every exit path, snapshots.

A stub runner, the cgroup and proc fixtures and a fake clock drive the real code: nothing here can stop,
restart or signal a real gateway, and every path (state, lock, snapshots, watchdog.off) is under tmp_path.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import textwrap
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

FIX = REPO / "tests" / "fixtures"
JOURNAL = FIX / "journal" / "gateway-restart-recovery-2026-10-10.txt"
# 2026-10-11 08:00 UTC is 03:00 in America/Guayaquil: inside the 02:00-05:00 window.
import datetime as _dt
IN_WINDOW = _dt.datetime(2026, 10, 11, 8, 0, tzinfo=_dt.timezone.utc).timestamp()
OUT_OF_WINDOW = _dt.datetime(2026, 10, 11, 18, 0, tzinfo=_dt.timezone.utc).timestamp()   # 13:00 local
CG = "/openclaw-gateway.service"

KNOBS = {**host.RESTART_KNOB_DEFAULTS, "OPENCLAW_GRACEFUL_RESTART": "on", "OPENCLAW_RESTART_SETTLE_S": "0"}


class Stub:
    """Answers every command graceful_restart issues and records the ones that mutate."""

    def __init__(self, tmp_path, *, active="active", restart_results=None, health_ok=True, new_pid=True,
                 boom_on_restart=None, restart_hook=None):
        self.tmp = tmp_path
        self.calls: list[list[str]] = []
        self.active = active
        self.restart_results = list(restart_results if restart_results is not None else [(0, "restarted")])
        self.health_ok = health_ok
        self.pid = "1001"
        self.new_pid = new_pid
        self.boom_on_restart = boom_on_restart
        self.restart_hook = restart_hook
        self.marker_at_restart: list[bool] = []
        self.state_at_restart: list[str] = []
        self.marker_path = tmp_path / "watchdog.off"
        self.state_path = tmp_path / "state"

    def restarts(self):
        return [c for c in self.calls if c[-2:] == ["gateway", "restart"]]

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return (0 if self.active == "active" else 3), self.active
        if argv[:3] == ["systemctl", "--user", "show"]:
            if argv[-3:] == ["-p", "ControlGroup", "--value"]:
                return 0, CG
            if argv[-3:] == ["-p", "MainPID", "--value"]:
                return 0, self.pid
            return 0, "MainPID=%s\nMemoryCurrent=1\nActiveState=active" % self.pid
        if argv[0] == "timeout" and argv[-2:] == ["gateway", "restart"]:
            self.marker_at_restart.append(self.marker_path.exists())
            self.state_at_restart.append(self.state_path.read_text() if self.state_path.exists() else "")
            if self.restart_hook:
                self.restart_hook()
            if self.boom_on_restart:
                raise self.boom_on_restart
            rc, text = self.restart_results.pop(0) if self.restart_results else (0, "")
            if rc == 0 and self.new_pid:
                self.pid = "2002"
            return rc, text
        if argv[-1:] == ["health"]:
            return (0 if self.health_ok else 1), ""
        if argv[:2] == ["free", "-b"]:
            return 0, "Swap: 17179869184 16000000000 1179869184"
        if argv[0] == "ps":
            return 0, "  PID  PPID STAT ELAPSED   RSS COMMAND\n 1001     1 Ssl     7200 9000 node"
        if argv[0] == "timeout" and argv[2:5] == ["openclaw", "sessions", "list"]:
            return 0, json.dumps([{"key": "agent:main:slack:x", "status": "running",
                                   "prompt": "SECRET PROMPT TEXT", "updatedAt": 1}])
        if argv[0] == "journalctl":
            return 0, JOURNAL.read_text(encoding="utf-8")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def env(tmp_path):
    class E:
        pass
    e = E()
    e.tmp = tmp_path
    e.marker = host.Marker(tmp_path / "watchdog.off")
    e.state = tmp_path / "state"
    e.lock = tmp_path / "lock"
    e.snaps = tmp_path / "snaps"
    e.cgroup = tmp_path / "cg"
    import shutil
    shutil.copytree(FIX / "cgroup" / "normal", e.cgroup)
    e.self_cg = tmp_path / "self_cgroup"
    e.self_cg.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    e.sleeps: list = []
    e.now = IN_WINDOW

    def run(stub, **kw):
        args = dict(reason="memory", classification="pressure", runner=stub, knobs=dict(KNOBS),
                    marker=e.marker, state_path=e.state, lock_path=e.lock, snapshot_root=e.snaps,
                    cgroup_root=e.cgroup, proc_root=FIX / "proc", self_cgroup_path=e.self_cg,
                    now=lambda: e.now, sleep=e.sleeps.append, openclaw_bin="openclaw", out=lambda m: None)
        args.update(kw)
        return host.graceful_restart(**args)
    e.run = run
    return e


def stub_for(env, **kw):
    s = Stub(env.tmp, **kw)
    s.marker_path, s.state_path = env.marker.path, env.state
    return s


# --- gates ---------------------------------------------------------------------------------------------------

def test_success_calls_restart_once_through_timeout_never_force_and_writes_state_first(env):
    stub = stub_for(env)
    res = env.run(stub)
    assert res["exit_code"] == 0 and res["result"] == "ok" and res["old_pid"] == "1001" and res["new_pid"] == "2002"
    [call] = stub.restarts()
    assert call[:2] == ["timeout", "600"] and call[2:] == ["openclaw", "gateway", "restart"]
    assert stub.marker_at_restart == [True], "watchdog.off exists during the call"
    assert not env.marker.exists(), "and is gone afterwards"
    assert "last_restart=" in stub.state_at_restart[0], "state is written BEFORE the restart"
    assert host.state_get("last_restart_result", env.state) == "ok"
    assert host.state_get("restarts_day", env.state).endswith(":1")


@pytest.mark.parametrize("name,setup", [
    ("watchdog_off", lambda e, kw: e.marker.touch()),
    ("unit_active", lambda e, kw: kw.update(active="inactive")),
    ("unit_active", lambda e, kw: kw.update(active="deactivating")),
    ("cooldown", lambda e, kw: host.state_set("last_restart", str(int(e.now - 600)), e.state)),
    ("daily_cap", lambda e, kw: host.state_set("restarts_day", host._local_date(e.now, "America/Guayaquil") + ":2", e.state)),
    ("window", lambda e, kw: setattr(e, "now", OUT_OF_WINDOW)),
    ("mode", lambda e, kw: kw.update(mode="notify")),
    ("pressure", lambda e, kw: kw.update(classification="healthy")),
])
def test_each_gate_refuses_with_its_name_and_changes_nothing(env, name, setup):
    kw: dict = {}
    setup(env, kw)
    stub = stub_for(env, active=kw.pop("active", "active"))
    run_kw = {}
    if "mode" in kw:
        run_kw["knobs"] = {**KNOBS, "OPENCLAW_GRACEFUL_RESTART": kw.pop("mode")}
    if "classification" in kw:
        run_kw["classification"] = kw.pop("classification")
    before_state = env.state.read_text() if env.state.exists() else ""
    res = env.run(stub, **run_kw)
    assert res["exit_code"] == host.EXIT_REFUSED_GATE and res["result"] == "refused-gate" and res["gate"] == name
    assert stub.restarts() == [] and not env.snaps.exists()
    assert (env.state.read_text() if env.state.exists() else "") == before_state


def test_outside_the_window_above_the_ceiling_acts_and_inside_the_window_acts(env):
    env.now = OUT_OF_WINDOW
    assert env.run(stub_for(env), classification="hard")["result"] == "ok"
    env2 = env
    env2.now = IN_WINDOW + 86400
    host.state_set("last_restart", "0", env.state)
    host.state_set("restarts_day", "1970-01-01:1", env.state)
    assert env2.run(stub_for(env2), classification="pressure")["result"] == "ok"


def test_a_manual_run_skips_the_mode_and_pressure_gates_but_not_the_window(env):
    knobs = {**KNOBS, "OPENCLAW_GRACEFUL_RESTART": "notify"}
    res = env.run(stub_for(env), reason="manual", classification=None, knobs=knobs)
    assert res["result"] == "ok"
    env.now = OUT_OF_WINDOW + 86400
    host.state_set("last_restart", "0", env.state)
    host.state_set("restarts_day", "1970-01-01:0", env.state)
    res = env.run(stub_for(env), reason="manual", classification=None, knobs=knobs)
    assert res["gate"] == "window"


def test_the_self_cgroup_check_refuses_before_anything_else(env):
    env.self_cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/openclaw-gateway.service\n")
    stub = stub_for(env)
    res = env.run(stub)
    assert res["exit_code"] == host.EXIT_REFUSED_SELF and res["result"] == "refused-self"
    assert stub.calls == [] and not env.lock.exists()


def test_a_held_lock_exits_without_acting(env):
    env.lock.touch()
    fd = os.open(env.lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        stub = stub_for(env)
        res = env.run(stub)
    finally:
        os.close(fd)
    assert res["exit_code"] == host.EXIT_LOCKED and res["result"] == "locked" and stub.restarts() == []
    # and a dry run reports the contention the same way, without creating the file
    fd = os.open(env.lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert env.run(stub_for(env), dry_run=True)["exit_code"] == host.EXIT_LOCKED
    finally:
        os.close(fd)


# --- retries and failures --------------------------------------------------------------------------------------

REFUSED = (1, "GATEWAY_RESTART_PREPARATION_REFUSED: Cannot record restart intent ... database is locked. "
              "Gateway was not signaled.")


def test_a_refused_preparation_is_retried_and_then_succeeds(env):
    stub = stub_for(env, restart_results=[REFUSED, REFUSED, (0, "ok")])
    res = env.run(stub)
    assert res["result"] == "ok" and res["attempts"] == 3 and len(stub.restarts()) == 3
    assert [s for s in env.sleeps if s in (20, 40, 80)] == [20, 40]


def test_refused_every_time_is_refused_never_frozen_and_backs_off_20_40_80(env):
    stub = stub_for(env, restart_results=[REFUSED] * 4)
    res = env.run(stub)
    assert res["result"] == "refused" and res["exit_code"] == host.EXIT_RESTART_REFUSED and res["attempts"] == 4
    assert [s for s in env.sleeps if s in (20, 40, 80)] == [20, 40, 80]
    assert not env.marker.exists() and host.state_get("last_restart_result", env.state) == "refused"


def test_a_non_zero_restart_is_failed_with_the_marker_gone(env):
    res = env.run(stub_for(env, restart_results=[(1, "boom")]))
    assert res["result"] == "failed" and res["exit_code"] == host.EXIT_RESTART_FAILED and not env.marker.exists()
    assert host.state_get("last_restart_result", env.state) == "failed"


def test_no_new_pid_is_failed(env):
    res = env.run(stub_for(env, new_pid=False))
    assert res["exit_code"] == host.EXIT_NO_NEW_PID and res["result"] == "failed"


def test_a_health_timeout_is_failed(env):
    res = env.run(stub_for(env, health_ok=False))
    assert res["exit_code"] == host.EXIT_HEALTH_TIMEOUT and not env.marker.exists()


def test_an_exception_mid_restart_removes_the_marker_and_is_reported(env):
    stub = stub_for(env, boom_on_restart=RuntimeError("boom"))
    res = env.run(stub)
    assert res["exit_code"] == host.EXIT_RESTART_FAILED and res["error"] == "RuntimeError"
    assert not env.marker.exists()
    assert host.state_get("last_restart_result", env.state) == "failed"


def test_sigterm_during_the_restart_removes_the_marker(env, tmp_path):
    """The CLI path in a real subprocess; the stub blocks inside the restart call and gets a SIGTERM."""
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    ready = tmp_path / "ready"
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(f"""
        import sys, time, pathlib
        sys.path.insert(0, {str(REPO / 'scripts')!r})
        from ai_resources import openclaw_host as host
        marker = host.Marker(pathlib.Path({str(home / '.openclaw' / 'watchdog.off')!r}))
        def runner(argv, **kw):
            if argv[:3] == ["systemctl", "--user", "is-active"]: return 0, "active"
            if argv[-3:] == ["-p", "ControlGroup", "--value"]: return 0, "/gw"
            if argv[-3:] == ["-p", "MainPID", "--value"]: return 0, "1"
            if argv[-2:] == ["gateway", "restart"]:
                pathlib.Path({str(ready)!r}).write_text("x")
                time.sleep(60)
            return 0, ""
        # a fixed instant (2026-10-11 03:00 UTC) inside 02:00-05:00 UTC: the test never reads the wall clock
        host.graceful_restart(reason="memory", classification="pressure", runner=runner, now=lambda: 1791687600.0,
                              knobs={{**host.RESTART_KNOB_DEFAULTS, "OPENCLAW_GRACEFUL_RESTART": "on",
                                      "OPENCLAW_RESTART_WINDOW": "02:00-05:00", "OPENCLAW_RESTART_TZ": "UTC"}},
                              marker=marker, state_path=pathlib.Path({str(home / 'state')!r}),
                              lock_path=pathlib.Path({str(home / 'lock')!r}),
                              snapshot_root=pathlib.Path({str(home / 'snaps')!r}),
                              cgroup_root=pathlib.Path({str(tmp_path / 'nocg')!r}),
                              proc_root=pathlib.Path({str(tmp_path / 'noproc')!r}),
                              self_cgroup_path={str(env.self_cg)!r}, openclaw_bin="openclaw",
                              out=lambda m: None)
    """))
    proc = subprocess.Popen([sys.executable, "-I", str(driver)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), proc.stderr.read().decode() if proc.poll() is not None else "driver never reached the restart"
        assert (home / ".openclaw" / "watchdog.off").exists(), "the marker is held during the restart"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (home / ".openclaw" / "watchdog.off").exists(), "SIGTERM must remove watchdog.off"
    assert proc.returncode == 128 + signal.SIGTERM


def test_marker_guard_restores_signal_handlers_and_removes_on_exception(tmp_path):
    marker = host.Marker(tmp_path / "off")
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(ValueError):
        with host.MarkerGuard(marker):
            assert marker.exists() and signal.getsignal(signal.SIGTERM) is not before
            raise ValueError("x")
    assert not marker.exists() and signal.getsignal(signal.SIGTERM) is before


# --- dry run -----------------------------------------------------------------------------------------------------

def test_dry_run_evaluates_the_gates_and_mutates_nothing(env):
    stub = stub_for(env)
    res = env.run(stub, dry_run=True)
    assert res["exit_code"] == 0 and res["result"] == "dry-run" and res["planned"]
    assert {g["name"] for g in res["gates"]} >= {"watchdog_off", "unit_active", "mode", "pressure", "cooldown",
                                                  "daily_cap", "window"}
    assert stub.restarts() == []
    assert not env.state.exists() and not env.lock.exists() and not env.snaps.exists() and not env.marker.exists()
    mutating = [c for c in stub.calls if c[:3] in (["systemctl", "--user", "stop"], ["systemctl", "--user", "start"])
                or "restart" in c]
    assert mutating == []


def test_dry_run_reports_a_failing_gate_with_its_name_and_still_mutates_nothing(env):
    env.now = OUT_OF_WINDOW
    res = env.run(stub_for(env), dry_run=True)
    assert res["exit_code"] == host.EXIT_REFUSED_GATE and res["gate"] == "window"
    assert not env.state.exists() and not env.snaps.exists()


# --- snapshots and the report ------------------------------------------------------------------------------------

def test_snapshot_modes_contents_and_no_argv_or_prompt_text(env):
    stub = stub_for(env)
    res = env.run(stub)
    snap = pathlib.Path(res["snapshot_dir"])
    modes = {}
    for p in [snap, *snap.rglob("*")]:
        modes[p] = stat.S_IMODE(p.stat().st_mode)
    assert all(m == (0o700 if p.is_dir() else 0o600) for p, m in modes.items()), modes
    names = {p.name for p in snap.rglob("*") if p.is_file()}
    assert {"busy.txt", "unit.txt", "free.txt", "cgroup.txt", "ps.txt", "sessions.json", "report.txt"} <= names
    blob = "".join(p.read_text() for p in snap.rglob("*") if p.is_file())
    assert "SECRET PROMPT TEXT" not in blob and "agent:main:slack:x" in blob
    ps_calls = [c for c in stub.calls if c[0] == "ps"]
    assert ps_calls and all("args" not in c[2] and "cmd" not in c[2] for c in ps_calls)


def test_snapshots_are_pruned_to_the_bound(env):
    env.snaps.mkdir(parents=True)
    for i in range(12):
        (env.snaps / f"2026010{i % 10}T0000{i:02d}Z").mkdir()
    knobs = {**KNOBS, "OPENCLAW_RESTART_SNAPSHOTS_KEEP": "3"}
    env.run(stub_for(env), knobs=knobs)
    left = sorted(p.name for p in env.snaps.iterdir())
    assert len(left) == 3 and any(n.startswith("20261011") for n in left)


def test_the_report_has_the_downtime_memory_and_recovery_fields(env):
    res = env.run(stub_for(env))
    assert res["memory_before"] == 6442450944 and res["memory_after"] == 6442450944
    assert res["recovery"]["available"] is True and res["recovery"]["aborted_runs"] == 4
    text = pathlib.Path(res["report"]).read_text()
    for needle in ("reason=memory", "result=ok", "memory_before=", "marked_interrupted=3", "aborted_runs=4",
                   "restart_command_seconds="):
        assert needle in text, needle
    assert host.state_get("last_report", env.state) == res["report"]


def test_no_runner_call_ever_contains_force_or_a_kill(env):
    stub = stub_for(env, restart_results=[REFUSED, (0, "ok")])
    env.run(stub)
    for call in stub.calls:
        assert "--force" not in call and not any(w in ("kill", "pkill", "killall", "-9") for w in call), call
        assert call[:3] not in (["systemctl", "--user", "stop"], ["systemctl", "--user", "start"])
        assert call[:3] != ["systemctl", "--user", "restart"]


def test_the_cli_prints_json_and_returns_the_exit_code(env, capsys):
    import argparse
    args = argparse.Namespace(reason="memory", dry_run=True, classification="pressure", json=True)
    rc = host.cmd_graceful_restart(args, runner=stub_for(env), knobs=dict(KNOBS), marker=env.marker,
                                   state_path=env.state, lock_path=env.lock, snapshot_root=env.snaps,
                                   cgroup_root=env.cgroup, proc_root=FIX / "proc", self_cgroup_path=env.self_cg,
                                   now=lambda: env.now, sleep=lambda s: None, openclaw_bin="openclaw")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["result"] == "dry-run"


def test_poll_health_sleeps_before_every_probe():
    seq = []

    def runner(argv, **kw):
        seq.append("probe")
        return (0 if seq.count("probe") >= 3 else 1), ""
    ok = host.poll_health(runner, "oc", timeout=20, interval=5, sleep=lambda s: seq.append("sleep"))
    assert ok and seq == ["sleep", "probe", "sleep", "probe", "sleep", "probe"]


# --- D11: setup's own restart goes through MarkerGuard ---------------------------------------------------------------

def test_the_setup_restart_idle_path_holds_and_removes_the_marker(monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    seen: list[bool] = []

    def fake_openclaw(args, stdin=None, timeout=120):
        if args == ["gateway", "restart"]:
            seen.append(host.WATCHDOG_OFF.exists())
        return 0, "ok"

    monkeypatch.setattr(openclaw, "_openclaw", fake_openclaw)
    monkeypatch.setattr(openclaw, "backend_registered", lambda _b: True)
    assert not host.WATCHDOG_OFF.exists()
    assert openclaw.restart_gateway(probe=lambda: (0, []), interactive=False) is True
    assert seen == [True], "watchdog.off exists during `gateway restart`"
    assert not host.WATCHDOG_OFF.exists(), "and is removed afterwards"


def test_the_setup_restart_removes_the_marker_when_the_restart_raises_or_fails(monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: (1, "boom"))
    assert openclaw.restart_gateway(probe=lambda: (0, []), interactive=False) is False
    assert not host.WATCHDOG_OFF.exists()

    def explode(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(openclaw, "_openclaw", explode)
    with pytest.raises(RuntimeError):
        openclaw.restart_gateway(probe=lambda: (0, []), interactive=False)
    assert not host.WATCHDOG_OFF.exists()


def test_a_window_somebody_else_opened_is_not_closed_by_the_setup_restart(monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    host.WATCHDOG_OFF.parent.mkdir(parents=True, exist_ok=True)
    host.WATCHDOG_OFF.touch()
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: (0, "ok"))
    monkeypatch.setattr(openclaw, "backend_registered", lambda _b: True)
    assert openclaw.restart_gateway(probe=lambda: (0, []), interactive=False) is True
    assert host.WATCHDOG_OFF.exists(), "an existing maintenance window stays open"
    host.WATCHDOG_OFF.unlink()


def test_the_cli_is_registered_with_manual_as_the_default_reason():
    import argparse
    ap = argparse.ArgumentParser()
    host.add_subparser(ap.add_subparsers(dest="cmd"))
    a = ap.parse_args(["openclaw", "graceful-restart", "--dry-run"])
    assert a.reason == "manual" and a.dry_run is True and a.classification is None
    assert ap.parse_args(["openclaw", "graceful-restart", "--reason", "memory", "--classification", "hard"]).classification == "hard"


# --- the time budget (ADR-0004, limit 9) ---------------------------------------------------------------------------

class FakeClock:
    """A monotonic clock that only moves when a fake sleep or a fake probe spends time."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_poll_health_is_bounded_by_a_wall_clock_deadline_with_hanging_probes():
    clock = FakeClock()

    def runner(argv, **kw):
        clock.t += kw["timeout"]          # a hung `openclaw health`: it burns its whole timeout
        return 124, "timed out"
    ok = host.poll_health(runner, "oc", timeout=180, interval=5, sleep=clock.sleep, clock=clock)
    assert ok is False
    assert clock.t <= 180 + host.HEALTH_FINAL_PROBE_MIN_S, f"poll ran {clock.t}s past a 180 s budget"


def test_poll_health_still_succeeds_when_a_probe_answers_after_slow_ones():
    clock = FakeClock()
    n = []

    def runner(argv, **kw):
        n.append(1)
        clock.t += 40
        return (0 if len(n) == 3 else 124), ""
    assert host.poll_health(runner, "oc", timeout=180, interval=5, sleep=clock.sleep, clock=clock) is True


def test_a_restart_whose_every_step_hangs_ends_inside_the_unit_budget(env):
    """Restart attempts that refuse only after their full timeout, then a health probe that hangs."""
    clock = FakeClock()
    stub = stub_for(env)
    refusal = (1, host.REFUSAL_CODE)
    stub.restart_results = [refusal] * 4

    def runner(argv, **kw):
        if argv[-2:] == ["gateway", "restart"]:
            stub.calls.append(list(argv))
            clock.t += kw["timeout"]
            return stub.restart_results.pop(0)
        if argv[-1:] == ["health"]:
            clock.t += kw["timeout"]
            return 124, "timed out"
        return stub(argv, **kw)
    res = env.run(runner, sleep=clock.sleep, clock=clock)
    assert res["exit_code"] == host.EXIT_RESTART_REFUSED
    assert clock.t <= host.RESTART_PHASE_BUDGET_S, f"restart phase ran {clock.t}s"
    assert res["attempts"] == 1 and res.get("budget_exhausted") is True, "no retry can finish inside the budget"
    assert not env.marker.exists()


def test_fast_refusals_still_get_every_retry_inside_the_budget(env):
    clock = FakeClock()
    stub = stub_for(env, restart_results=[(1, host.REFUSAL_CODE)] * 3 + [(0, "ok")])

    def runner(argv, **kw):
        if argv[-2:] == ["gateway", "restart"]:
            clock.t += 5
        return stub(argv, **kw)
    res = env.run(runner, sleep=clock.sleep, clock=clock)
    assert res["exit_code"] == 0 and res["attempts"] == 4 and "budget_exhausted" not in res


def test_the_worst_case_sum_fits_the_timer_units_timeout_start_sec():
    unit = (REPO / "templates" / "systemd" / "openclaw-health-restart.service.template").read_text()
    limit = int(next(l for l in unit.splitlines() if l.startswith("TimeoutStartSec=")).split("=")[1].removesuffix("min")) * 60
    script_probes = 30 + 30 + 300
    health = host.RESTART_HEALTH_TIMEOUT_S + host.HEALTH_FINAL_PROBE_MIN_S
    bookkeeping = 18 * host.PROBE_CAP_S + 60 + 6 * host.PROBE_CAP_S      # probes + the confirm sample
    notices = 3 * (30 + 20) + 30
    total = script_probes + host.RESTART_PHASE_BUDGET_S + health + host.RESTART_SETTLE_MAX_S + bookkeeping + notices
    assert total == 2585
    assert limit - total >= 300, f"margin {limit - total}s under TimeoutStartSec={limit}s"
    assert host.RESTART_PHASE_BUDGET_S >= host.RESTART_COMMAND_TIMEOUT_S + host.RESTART_COMMAND_GRACE_S
