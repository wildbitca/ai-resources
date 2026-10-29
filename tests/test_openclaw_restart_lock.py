"""One lock and one exclusive marker for every path that stops or restarts the gateway (2.2b section 2).

The graceful restart, the drained window (`doctor`, `apply-pending`) and setup's / `models update`'s restart
take the same non-blocking flock; `watchdog.off` is created with O_EXCL; and the restart path of setup refuses
inside the gateway cgroup and when the busy probe could not tell. Nothing here reaches a live unit.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402

# Integration: real processes / host behaviour; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

REAL_GATEWAY_BUSY = openclaw._gateway_busy      # conftest replaces the attribute; this is the real reader


class Runner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "show"]:
            return 0, "[not set]"
        return 0, ""

    def stops(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "stop"]]


def _window(tmp_path, runner, action=lambda: True, **kw):
    return host.drained_window(action, runner=runner, marker=host.Marker(tmp_path / "w.off"),
                               sleep=lambda s: None, openclaw_bin="openclaw", out=lambda m: None,
                               drain_timeout=3, drain_interval=1, health_timeout=5, health_interval=1,
                               self_cgroup_path=tmp_path / "no-cgroup", **kw)


# --- one lock for every path -------------------------------------------------------------------------------------

def test_a_drained_window_refuses_while_another_path_holds_the_lock(tmp_path):
    fd = host.acquire_restart_lock()
    assert fd is not None
    try:
        runner, lines = Runner(), []
        code = host.drained_window(lambda: True, runner=runner, marker=host.Marker(tmp_path / "w.off"),
                                   sleep=lambda s: None, openclaw_bin="openclaw", out=lines.append,
                                   self_cgroup_path=tmp_path / "no-cgroup")
        assert code == host.EXIT_REFUSED
        assert runner.stops() == [] and not (tmp_path / "w.off").exists()
        assert len([ln for ln in lines if ln.startswith("refusing")]) == 1
    finally:
        host.release_restart_lock(fd)
    assert _window(tmp_path, Runner()) == host.EXIT_OK, "released: the window opens again"


def test_the_lock_file_is_kept_and_reusable_after_a_window(tmp_path):
    assert _window(tmp_path, Runner()) == host.EXIT_OK
    assert host.LOCK_PATH.exists(), "never unlinked: an unlink while locked lets two holders in"
    fd = host.acquire_restart_lock()
    assert fd is not None
    host.release_restart_lock(fd)


def test_graceful_restart_setup_and_a_second_window_all_refuse_inside_a_drained_window(tmp_path, monkeypatch):
    seen: dict = {}
    runner = Runner()

    def action():
        # the first path is inside its window: every other path that stops or restarts the unit must refuse
        r = host.graceful_restart(reason="manual", runner=Runner(), openclaw_bin="openclaw", out=lambda m: None,
                                  self_cgroup_path=tmp_path / "no-cgroup", knobs=dict(host.RESTART_KNOB_DEFAULTS))
        seen["graceful"] = (r["exit_code"], r["result"])
        other = Runner()
        seen["window"] = _window(tmp_path / "second", other) if (tmp_path / "second").mkdir() is None else None
        seen["window_stops"] = other.stops()
        calls: list = []
        monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, "ok"))
        try:
            openclaw.restart_gateway(probe=lambda: (0, []), interactive=False)
            seen["setup"] = "restarted"
        except openclaw.RestartDeferred as e:
            seen["setup"] = e.reason
        seen["setup_calls"] = calls
        return True

    assert _window(tmp_path, runner, action) == host.EXIT_OK
    assert seen["graceful"] == (host.EXIT_LOCKED, "locked")
    assert seen["window"] == host.EXIT_REFUSED and seen["window_stops"] == []
    assert "lock is held" in seen["setup"] and seen["setup_calls"] == []
    assert len(runner.stops()) == 1


def test_the_lock_is_shared_across_processes(tmp_path):
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO / "scripts")!r})
        from ai_resources import openclaw_host as host
        from pathlib import Path
        fd = host.acquire_restart_lock(Path({str(host.LOCK_PATH)!r}))
        print("held" if fd is not None else "busy", flush=True)
        time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        runner = Runner()
        assert _window(tmp_path, runner) == host.EXIT_REFUSED and runner.stops() == []
        with pytest.raises(openclaw.RestartDeferred):
            openclaw.restart_gateway(probe=lambda: (0, []), interactive=False)
    finally:
        p.kill()
        p.wait()
    assert _window(tmp_path, Runner()) == host.EXIT_OK, "a dead holder releases the lock"


def test_two_concurrent_paths_never_both_proceed(tmp_path):
    started, release = threading.Event(), threading.Event()
    results: dict = {}
    r1 = Runner()

    def first():
        def action():
            started.set()
            release.wait(10)
            return True
        results["first"] = _window(tmp_path, r1, action)

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(10)
    try:
        r2 = Runner()
        results["second"] = _window(tmp_path / "x", r2) if (tmp_path / "x").mkdir() is None else None
    finally:
        release.set()
        t.join(10)
    assert results["first"] == host.EXIT_OK and results["second"] == host.EXIT_REFUSED
    assert len(r1.stops()) == 1 and r2.stops() == []


# --- the marker is created exclusively ---------------------------------------------------------------------------

def test_marker_acquire_has_exactly_one_winner(tmp_path):
    marker = host.Marker(tmp_path / "sub" / "watchdog.off")
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        wins.append(marker.acquire())

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1 and wins.count(False) == 7
    assert marker.exists()


def test_an_exclusive_guard_refuses_an_open_window_and_leaves_it_in_place(tmp_path):
    marker = host.Marker(tmp_path / "watchdog.off")
    marker.touch()
    with pytest.raises(host.WindowBusy):
        host.MarkerGuard(marker, exclusive=True).__enter__()
    assert marker.exists(), "somebody else's window is not removed"
    with host.MarkerGuard(marker):          # the non-exclusive guard (setup) proceeds and does not own it
        pass
    assert marker.exists()
    marker.remove()
    with host.MarkerGuard(marker, exclusive=True):
        assert marker.exists()
    assert not marker.exists()


def test_a_drained_window_does_not_take_a_marker_that_appeared_after_the_check(tmp_path):
    class Racy(host.Marker):
        """exists() says no, and then the operator's window is there by the time of the exclusive create."""
        def exists(self):
            if not getattr(self, "raced", False):
                self.raced = True
                super().touch()
                return False
            return super().exists()

    runner, lines = Runner(), []
    code = host.drained_window(lambda: True, runner=runner, marker=Racy(tmp_path / "w.off"), sleep=lambda s: None,
                               openclaw_bin="openclaw", out=lines.append, self_cgroup_path=tmp_path / "no-cgroup")
    assert code == host.EXIT_REFUSED and runner.stops() == []
    assert (tmp_path / "w.off").exists(), "the window that appeared is left alone"


# --- setup's restart: inside the gateway cgroup, and a probe that cannot tell --------------------------------------

def test_the_setup_restart_refuses_inside_the_gateway_cgroup(tmp_path, monkeypatch):
    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/openclaw-gateway.service\n")
    calls: list = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, "ok"))
    with pytest.raises(openclaw.RestartDeferred) as e:
        openclaw.restart_gateway(probe=lambda: (0, []), interactive=False, self_cgroup_path=str(cg))
    assert "T29" in e.value.reason and calls == []
    assert not host.WATCHDOG_OFF.exists()


def test_the_default_busy_reader_fails_closed_when_it_has_something_to_report(monkeypatch):
    def noisy(runner=None, **kw):
        kw["out"]("restart guard: systemctl could not describe openclaw-gateway.service (rc 1); treating idle")
        return 0, []

    monkeypatch.setattr(host, "gateway_busy", noisy)
    count, workers = REAL_GATEWAY_BUSY()
    assert count == openclaw.PROBE_UNKNOWN and workers == []
    monkeypatch.setattr(host, "gateway_busy", lambda runner=None, **kw: (0, []))
    assert REAL_GATEWAY_BUSY() == (0, [])


def test_an_unknown_busy_state_is_asked_about_and_never_restarts_unattended(monkeypatch):
    calls: list = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, "ok"))
    monkeypatch.setattr(openclaw, "backend_registered", lambda b: True)
    with pytest.raises(openclaw.RestartDeferred) as e:
        openclaw.restart_gateway(probe=lambda: (openclaw.PROBE_UNKNOWN, []), interactive=False)
    assert "could not tell" in e.value.reason and calls == []
    asked: list = []
    out = openclaw.restart_gateway(probe=lambda: (openclaw.PROBE_UNKNOWN, []), interactive=True,
                                   ask=lambda q, opts, default=None: asked.append(q) or openclaw.RESTART_NOW)
    assert out is True and "could not tell" in asked[0] and len(calls) == 1


# --- 2.2c, 6e ----------------------------------------------------------------------------------------------------

def test_the_strict_busy_reader_says_cannot_tell_not_idle_when_the_restart_then_defers(monkeypatch):
    from ai_resources.setup import ui
    said: list[str] = []
    monkeypatch.setattr(ui, "detail", lambda msg, *a, **k: said.append(str(msg)))

    def noisy(runner=None, **kw):
        kw["out"]("restart guard: systemctl could not describe openclaw-gateway.service (rc 1); "
                  "treating the gateway as idle")
        kw["out"]("restart guard: cannot read /sys/fs/cgroup/x (Permission denied); treating the gateway as idle")
        return 0, []

    monkeypatch.setattr(host, "gateway_busy", noisy)
    count, _workers = REAL_GATEWAY_BUSY()
    assert count == openclaw.PROBE_UNKNOWN
    assert len(said) == 2 and all("cannot tell whether agent turns are in flight" in m for m in said)
    assert not any("treating the gateway as idle" in m for m in said)


def test_a_symlinked_restart_lock_says_it_is_a_symlink(tmp_path):
    real = tmp_path / "elsewhere"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "health-restart.lock"
    link.symlink_to(real)
    with pytest.raises(host.RestartLockUnusable) as e:
        host.acquire_restart_lock(link)
    assert "is a symlink" in str(e.value) and "OSError" not in str(e.value)
    plain = tmp_path / "dir.lock"
    plain.mkdir()
    with pytest.raises(host.RestartLockUnusable) as e2:
        host.acquire_restart_lock(plain)
    assert "is a symlink" not in str(e2.value)


def test_the_graceful_restart_lock_loser_prints_the_same_one_line_as_doctor_and_apply_pending(tmp_path):
    fd = host.acquire_restart_lock()
    assert fd is not None
    try:
        lines: list[str] = []
        res = host.graceful_restart(reason="manual", runner=Runner(), knobs=dict(host.RESTART_KNOB_DEFAULTS),
                                    marker=host.Marker(tmp_path / "off"), state_path=tmp_path / "state",
                                    lock_path=host.LOCK_PATH, snapshot_root=tmp_path / "snap",
                                    self_cgroup_path=tmp_path / "no-cgroup", openclaw_bin="openclaw",
                                    out=lines.append)
        assert res["exit_code"] == host.EXIT_LOCKED
        drained: list[str] = []
        host.drained_window(lambda: True, runner=Runner(), marker=host.Marker(tmp_path / "w.off"),
                            sleep=lambda s: None, openclaw_bin="openclaw", out=drained.append,
                            self_cgroup_path=tmp_path / "no-cgroup")
    finally:
        host.release_restart_lock(fd)
    assert lines == drained and len(lines) == 1
    assert "another restart or maintenance window is in progress" in lines[0] and "nothing was stopped" in lines[0]


def test_the_lock_messages_are_exactly_the_sentences_the_operator_is_told(tmp_path):
    """2.2d (4): `<path> is a symlink` (no colon) for a link, `<path>: <ErrorName>` for anything else."""
    real = tmp_path / "elsewhere"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "health-restart.lock"
    link.symlink_to(real)
    with pytest.raises(host.RestartLockUnusable) as e:
        host.acquire_restart_lock(link)
    assert str(e.value) == f"{link} is a symlink"
    plain = tmp_path / "dir.lock"
    plain.mkdir()
    with pytest.raises(host.RestartLockUnusable) as e2:
        host.acquire_restart_lock(plain)
    assert str(e2.value) == f"{plain}: IsADirectoryError"
