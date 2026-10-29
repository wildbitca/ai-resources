"""R5 fault injection: `watchdog.off` is removed and the gateway started again on EVERY exit path of the drained
window (an exception, SIGTERM, SIGINT, at any phase), and the refusals (lock, busy, T29) stop nothing."""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

# Integration: real processes / host behaviour; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration


class Runner:
    def __init__(self, on_show=None, on_health=None):
        self.calls: list[list[str]] = []
        self.on_show, self.on_health = on_show, on_health

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "show"]:
            if self.on_show:
                self.on_show()
            return 0, "[not set]"
        if argv[:2] == ["openclaw", "health"] and self.on_health:
            self.on_health()
        return 0, ""

    def verbs(self, verb):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", verb]]


def _window(tmp_path, runner, action=lambda: True, **kw):
    kw.setdefault("lock_path", tmp_path / "lock")
    return host.drained_window(action, runner=runner, marker=host.Marker(tmp_path / "w.off"),
                               sleep=lambda s: None, openclaw_bin="openclaw", out=lambda m: None,
                               drain_timeout=3, drain_interval=1, health_timeout=5, health_interval=1,
                               self_cgroup_path=tmp_path / "no-cgroup", **kw)


def _clean(tmp_path, runner):
    assert not (tmp_path / "w.off").exists(), "watchdog.off was left behind"
    assert len(runner.verbs("start")) == 1, "the gateway was not started again exactly once"


def test_an_exception_in_the_action_still_removes_the_marker_and_starts_the_unit(tmp_path):
    runner = Runner()

    def boom():
        assert (tmp_path / "w.off").exists()
        raise RuntimeError("action blew up")

    with pytest.raises(RuntimeError):
        _window(tmp_path, runner, boom)
    _clean(tmp_path, runner)


@pytest.mark.parametrize("sig,exc", [(signal.SIGTERM, SystemExit), (signal.SIGINT, KeyboardInterrupt)])
def test_a_signal_during_the_action_is_an_exit_path(tmp_path, sig, exc):
    runner = Runner()
    before = signal.getsignal(sig)

    def killed():
        os.kill(os.getpid(), sig)
        time.sleep(1)          # the handler raises before this ends
        return True

    with pytest.raises(exc):
        _window(tmp_path, runner, killed)
    _clean(tmp_path, runner)
    assert signal.getsignal(sig) == before, "the previous handler was not restored"


@pytest.mark.parametrize("sig,exc", [(signal.SIGTERM, SystemExit), (signal.SIGINT, KeyboardInterrupt)])
def test_a_signal_while_the_cgroup_drains_is_an_exit_path(tmp_path, sig, exc):
    runner = Runner(on_show=lambda: os.kill(os.getpid(), sig))
    with pytest.raises(exc):
        _window(tmp_path, runner)
    _clean(tmp_path, runner)


def test_a_signal_in_the_health_poll_after_the_start_leaves_nothing_behind(tmp_path):
    runner = Runner(on_health=lambda: os.kill(os.getpid(), signal.SIGTERM))
    with pytest.raises(SystemExit):
        _window(tmp_path, runner)
    _clean(tmp_path, runner)


def test_a_real_sigterm_to_a_child_process_mid_action_removes_the_marker(tmp_path):
    """No in-process trick: a separate interpreter holds the window open, the parent sends the real signal."""
    marker, log, ready = tmp_path / "w.off", tmp_path / "calls.log", tmp_path / "ready"
    script = textwrap.dedent(f"""
        import pathlib, sys, time
        sys.path.insert(0, {str(REPO / "scripts")!r})
        from ai_resources import openclaw_host as host
        def runner(argv, **kw):
            open({str(log)!r}, "a").write(" ".join(argv) + "\\n")
            return (0, "[not set]") if argv[:3] == ["systemctl", "--user", "show"] else (0, "")
        def action():
            open({str(ready)!r}, "w").write("x")
            time.sleep(60)
            return True
        host.drained_window(action, runner=runner, marker=host.Marker(pathlib.Path({str(marker)!r})),
                            sleep=lambda s: None, openclaw_bin="openclaw", out=lambda m: None,
                            drain_timeout=3, drain_interval=1, health_timeout=5, health_interval=1,
                            self_cgroup_path={str(tmp_path / "no-cgroup")!r},
                            lock_path=pathlib.Path({str(tmp_path / "lock")!r}))
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 20
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "window never opened"
        assert marker.exists(), "the window did not hold watchdog.off"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not marker.exists(), "SIGTERM left watchdog.off behind"
    calls = log.read_text().splitlines()
    assert any(c.startswith("systemctl --user start openclaw-gateway.service") for c in calls), calls


def test_an_operators_window_is_never_taken_or_removed(tmp_path):
    (tmp_path / "w.off").write_text("")
    runner = Runner()
    code = _window(tmp_path, runner)
    assert code == host.EXIT_REFUSED and runner.verbs("stop") == []
    assert (tmp_path / "w.off").exists()


# --- refusals stop nothing --------------------------------------------------------------------------------------

def test_inside_the_gateway_cgroup_nothing_is_stopped(tmp_path):
    cg = tmp_path / "cg"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/openclaw-gateway.service\n")
    runner = Runner()
    code = host.drained_window(lambda: True, runner=runner, marker=host.Marker(tmp_path / "w.off"),
                               sleep=lambda s: None, openclaw_bin="openclaw", out=lambda m: None,
                               self_cgroup_path=cg, lock_path=tmp_path / "lock")
    assert code == host.EXIT_REFUSED_SELF and runner.calls == [] and not (tmp_path / "w.off").exists()


def test_busy_gateway_defers_before_anything_is_stopped(tmp_path):
    runner = Runner()
    code = _window(tmp_path, runner, require_idle=True, busy_probe=lambda: 3)
    assert code == host.EXIT_REFUSED_BUSY and runner.verbs("stop") == [] and not (tmp_path / "w.off").exists()
    code = _window(tmp_path, runner, require_idle=True, busy_probe=lambda: None)
    assert code == host.EXIT_REFUSED_BUSY and runner.verbs("stop") == []


def test_restart_lock_contention_defers(tmp_path):
    fd = host.acquire_restart_lock(tmp_path / "lock")
    try:
        runner = Runner()
        code = _window(tmp_path, runner)
        assert code == host.EXIT_REFUSED and runner.calls == []
    finally:
        host.release_restart_lock(fd)


def test_a_stale_kit_marker_of_a_dead_run_is_reclaimed_not_mistaken_for_a_window(tmp_path):
    path = tmp_path / "w.off"
    path.write_text(f"{host.KIT_MARKER_WORD} pid=999999 start=1 at=1\n")
    assert host.Marker(path).stale_kit_marker() and host.Marker(path).reclaim_stale()
    assert not path.exists()
