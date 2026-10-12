"""`watchdog.off` and a killed `models` run (kit 2.2e, 4c).

`TimeoutStartSec=900` sends SIGTERM to a daily `models update` that overran, and nothing trapped it: the marker
stayed, and since 2.2c a leftover marker is a maintenance window (the daily job stands down, the guard hook
allows everything). The run now holds the marker through `MarkerGuard`, removes only the file it created, and
signs it so a leftover of a dead kit run is told from a window an operator opened.
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

from ai_resources import models, openclaw_host as host  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402
from test_models_update import Env  # noqa: E402


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# --- the marker's signature and the identity of the file ------------------------------------------------------------

def test_acquire_signs_the_marker_with_this_process(tmp_path):
    m = host.Marker(tmp_path / "watchdog.off")
    assert m.acquire() is True
    text = m.path.read_text(encoding="ascii")
    ticks = host.process_start_ticks(os.getpid())
    assert ticks and text.startswith(f"kit-run pid={os.getpid()} start={ticks} at=")
    assert m.stale_kit_marker() is False and m.reclaim_stale() is False and m.path.exists()
    assert m.acquire() is False, "the exclusive create still has one winner"


def test_a_signed_marker_of_a_dead_run_is_stale_and_reclaimed(tmp_path):
    path = tmp_path / "watchdog.off"
    path.write_text(f"kit-run pid={_dead_pid()} start=4242 at=1\n", encoding="ascii")
    m = host.Marker(path)
    assert m.stale_kit_marker() is True
    assert m.reclaim_stale() is True and not path.exists()
    assert m.reclaim_stale() is False


def test_a_reused_pid_is_not_the_run_that_signed_the_marker(tmp_path):
    path = tmp_path / "watchdog.off"
    ticks = host.process_start_ticks(os.getpid())
    path.write_text(f"kit-run pid={os.getpid()} start={ticks + 99} at=1\n", encoding="ascii")
    assert host.Marker(path).stale_kit_marker() is True


@pytest.mark.parametrize("content", ["", "maintenance\n", "kit-run pid=x start=1\n", " kit-run pid=1 start=1\n",
                                     "touch\nkit-run pid=1 start=1\n"])
def test_an_operators_marker_is_never_stale(tmp_path, content):
    path = tmp_path / "watchdog.off"
    path.write_text(content, encoding="ascii")
    m = host.Marker(path)
    assert m.stale_kit_marker() is False and m.reclaim_stale() is False and path.exists()


def test_a_marker_that_is_a_link_or_a_directory_is_left_alone(tmp_path):
    target = tmp_path / "t"
    target.write_text(f"kit-run pid={_dead_pid()} start=1 at=1\n", encoding="ascii")
    link = tmp_path / "watchdog.off"
    link.symlink_to(target)
    assert host.Marker(link).reclaim_stale() is False and link.is_symlink()
    d = tmp_path / "dir"
    d.mkdir()
    assert host.Marker(d).reclaim_stale() is False and d.is_dir()


def test_remove_leaves_a_file_that_is_no_longer_the_one_this_object_created(tmp_path):
    path = tmp_path / "watchdog.off"
    m = host.Marker(path)
    assert m.acquire()
    os.link(path, tmp_path / "old-inode")                 # keeps the inode number from being handed out again
    path.unlink()
    path.write_text("", encoding="ascii")                 # the operator's, opened in its place
    m.remove()
    assert path.exists(), "an operator's window is not closed by a run that did not open it"
    m2 = host.Marker(path)
    m2.touch()
    m2.remove()
    assert not path.exists(), "a marker that was not acquired is removed as before"


def test_remove_removes_the_file_it_created_and_tolerates_its_absence(tmp_path):
    m = host.Marker(tmp_path / "watchdog.off")
    assert m.acquire()
    m.remove()
    assert not m.path.exists()
    m.remove()


# --- SIGTERM in the middle of a models run --------------------------------------------------------------------------

@pytest.fixture
def sigterm_sentinel():
    """A SIGTERM handler that only records: where the run installs no handler of its own (the unfixed code), the
    signal must not end the test process."""
    seen: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: seen.append(signum))
    yield seen
    signal.signal(signal.SIGTERM, previous)


def test_sigterm_during_the_restart_removes_the_marker_and_restores_the_handler(tmp_path, monkeypatch,
                                                                                  sigterm_sentinel):
    env = Env(tmp_path, monkeypatch)
    before = signal.getsignal(signal.SIGTERM)
    seen = []

    def killed_restart():
        seen.append(env.marker.exists())
        os.kill(os.getpid(), signal.SIGTERM)             # what systemd does at TimeoutStartSec
        raise AssertionError("unreachable: the handler raises first")

    deps = env.deps()
    import dataclasses
    deps = dataclasses.replace(deps, restart=killed_restart)
    with pytest.raises(SystemExit) as exc:
        models.run_update(models.Options(classes=["sonnet"]), deps)
    assert exc.value.code == 128 + signal.SIGTERM
    assert seen == [True]
    assert not env.marker.path.exists(), "the marker does not outlive a killed run"
    assert signal.getsignal(signal.SIGTERM) == before


def test_sigterm_does_not_remove_an_operators_marker_that_took_its_place(tmp_path, monkeypatch, sigterm_sentinel):
    env = Env(tmp_path, monkeypatch)

    def killed_restart():
        os.link(env.marker.path, tmp_path / "old-inode")  # keeps the inode number from being handed out again
        env.marker.path.unlink()                         # the operator replaces the file mid-run ...
        env.marker.path.write_text("", encoding="ascii")
        os.kill(os.getpid(), signal.SIGTERM)             # ... and the run is killed

    import dataclasses
    deps = dataclasses.replace(env.deps(), restart=killed_restart)
    with pytest.raises(SystemExit):
        models.run_update(models.Options(classes=["sonnet"]), deps)
    assert env.marker.path.exists() and env.marker.path.read_text(encoding="ascii") == ""


def test_rollback_holds_the_marker_through_the_guard_too(tmp_path, monkeypatch, sigterm_sentinel):
    env = Env(tmp_path, monkeypatch)
    assert env.run(classes=["sonnet"]).rc == 0
    import dataclasses

    def killed_restart():
        os.kill(os.getpid(), signal.SIGTERM)

    deps = dataclasses.replace(env.deps(), restart=killed_restart)
    with pytest.raises(SystemExit):
        models.run_rollback(deps)
    assert not env.marker.path.exists()


# --- a stale kit marker does not block the run; an operator's still does --------------------------------------------

def test_a_stale_kit_marker_does_not_block_the_restart(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.marker.path.write_text(f"kit-run pid={_dead_pid()} start=77 at=1\n", encoding="ascii")
    result = env.run(classes=["sonnet"])
    assert (result.rc, result.outcome) == (0, "switched")
    assert env.restarts == 1 and env.marker_during_restart == [True]
    assert not env.marker.path.exists()


def test_an_operators_marker_still_blocks_the_restart(tmp_path, monkeypatch):
    marker = host.Marker()                                # the path conftest.py points away from the real host
    marker.path.parent.mkdir(parents=True, exist_ok=True)
    marker.path.write_text("", encoding="ascii")
    monkeypatch.setattr(host, "gateway_cgroup_state", lambda path=None: "outside")
    monkeypatch.setattr(openclaw, "_openclaw", lambda argv, **kw: (0, "ok"))
    monkeypatch.setattr(openclaw, "backend_registered", lambda backend: True)
    real = models.default_deps().restart
    with pytest.raises(models.RestartDeferred):
        real()
    assert marker.path.exists()


def test_the_real_restart_reclaims_a_stale_kit_marker_and_refuses_inside_a_live_one(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "WATCHDOG_OFF", tmp_path / "watchdog.off")
    monkeypatch.setattr(host, "gateway_cgroup_state", lambda path=None: "outside")
    calls = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda argv, **kw: calls.append(argv) or (0, "ok"))
    monkeypatch.setattr(openclaw, "backend_registered", lambda backend: True)
    restart = models.default_deps().restart
    marker = host.Marker()
    marker.path.parent.mkdir(parents=True, exist_ok=True)
    marker.path.write_text(f"kit-run pid={_dead_pid()} start=1 at=1\n", encoding="ascii")
    restart()
    assert calls == [["gateway", "restart"]] and not marker.path.exists()
    ticks = host.process_start_ticks(os.getpid())
    marker.path.write_text(f"kit-run pid={os.getpid()} start={ticks} at=1\n", encoding="ascii")
    with pytest.raises(models.RestartDeferred):
        restart()
    assert marker.path.exists()
