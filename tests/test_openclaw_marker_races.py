"""The `watchdog.off` marker's races (kit 2.2f, item 4).

4a  `reclaim_stale` compared device and inode only: an operator who overwrote the stale file IN PLACE while the
    liveness of its run was being asked lost the window he had just opened.
4b  a SIGTERM between the exclusive create and `acquire()` returning left a marker nobody owned (an unsigned,
    empty one is an operator's maintenance window).
4c  `Marker.remove()` deleted whatever was there when the identity of the file it made could not be read.
4d  `_common.sh` read the marker through a redirect: a FIFO swapped in after the `[ -f ]` check blocked the
    models script for ever.
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

from ai_resources import openclaw_host as host  # noqa: E402


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# --- 4a --------------------------------------------------------------------------------------------------------------

def test_reclaim_leaves_a_file_overwritten_in_place_while_the_run_was_checked(tmp_path, monkeypatch):
    path = tmp_path / "watchdog.off"
    path.write_text(f"kit-run pid={_dead_pid()} start=1 at=1\n", encoding="ascii")
    m = host.Marker(path)

    def liveness(pid, start):
        before = path.stat()
        path.write_text("", encoding="ascii")                       # the operator opens a window in place ...
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))   # ... a later mtime
        assert path.stat().st_ino == before.st_ino
        return False                                                # the run is gone

    monkeypatch.setattr(host, "_kit_run_alive", liveness)
    assert m.reclaim_stale() is False
    assert path.exists() and path.read_text(encoding="ascii") == "", "the operator's window stays"


def test_reclaim_still_removes_an_untouched_stale_marker(tmp_path):
    path = tmp_path / "watchdog.off"
    path.write_text(f"kit-run pid={_dead_pid()} start=1 at=1\n", encoding="ascii")
    assert host.Marker(path).reclaim_stale() is True and not path.exists()


# --- 4b --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def sigterm_sentinel():
    seen: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: seen.append(signum))
    yield seen
    signal.signal(signal.SIGTERM, previous)


@pytest.mark.timing
def test_a_sigterm_between_the_create_and_the_return_does_not_leave_the_marker(tmp_path, monkeypatch,
                                                                            sigterm_sentinel):
    path = tmp_path / "watchdog.off"
    real = host.process_start_ticks

    def ticks_then_killed(pid):                                     # runs after the file exists, before it is signed
        assert path.exists()
        os.kill(os.getpid(), signal.SIGTERM)
        return real(pid)

    monkeypatch.setattr(host, "process_start_ticks", ticks_then_killed)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        with host.MarkerGuard(host.Marker(path)):
            raise AssertionError("unreachable: the signal ends the enter")
    assert exc.value.code == 128 + signal.SIGTERM
    assert not path.exists(), "no marker without an owner"
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_sigterm_while_the_marker_exists_already_is_not_ours_to_remove(tmp_path, sigterm_sentinel):
    path = tmp_path / "watchdog.off"
    path.write_text("", encoding="ascii")                           # an operator's window
    with pytest.raises(host.WindowBusy):
        with host.MarkerGuard(host.Marker(path), exclusive=True):
            pass
    assert path.exists()


# --- 4c --------------------------------------------------------------------------------------------------------------

def test_remove_leaves_a_file_whose_identity_was_not_recorded(tmp_path, monkeypatch, capsys):
    path = tmp_path / "watchdog.off"
    m = host.Marker(path)
    with monkeypatch.context() as mp:
        def failing(fd):
            raise OSError("no stat")
        mp.setattr(host.os, "fstat", failing)
        assert m.acquire() is True
    os.link(path, tmp_path / "old")                                 # an operator replaces it ...
    path.unlink()
    path.write_text("", encoding="ascii")                           # ... with his window
    m.remove()
    assert path.exists(), "what could not be identified is not deleted"
    assert "identity" in capsys.readouterr().err


def test_remove_of_an_identified_marker_is_unchanged(tmp_path):
    m = host.Marker(tmp_path / "watchdog.off")
    assert m.acquire() is True
    m.remove()
    assert not m.path.exists()
    touched = host.Marker(tmp_path / "other")
    touched.touch()
    touched.remove()
    assert not touched.path.exists()


# --- 4d --------------------------------------------------------------------------------------------------------------

def _stale_check(tmp_path, marker_setup, preamble=""):
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True, exist_ok=True)
    script = f'''
set -u
export HOME={home}
source {REPO}/scripts/openclaw/_common.sh
{preamble}
{marker_setup}
watchdog_marker_is_stale
echo "rc=$?"
'''
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                          env={"PATH": os.environ["PATH"], "HOME": str(home)})


def test_a_fifo_swapped_in_after_the_file_check_does_not_block(tmp_path):
    marker = tmp_path / "home" / ".openclaw" / "watchdog.off"
    # `[` is a function here: the first `-f` test sees the regular file, then the path becomes a FIFO
    preamble = f'''
[() {{
  if [[ "$1" == "-f" && -z "${{SWAPPED:-}}" && "$2" == "{marker}" ]]; then
    SWAPPED=1; builtin [ "$@"; local rc=$?
    rm -f "{marker}"; mkfifo "{marker}"
    return $rc
  fi
  builtin [ "$@"
}}
'''
    setup = f'echo "kit-run pid=1 start=1 at=1" > "{marker}"'
    # the shell function above must be defined AFTER the marker file exists; the check is the first `[ -f ]`
    r = _stale_check(tmp_path, setup + "\n" + preamble)
    assert r.returncode == 0 and "rc=1" in r.stdout, r.stderr


def test_a_signed_marker_of_a_dead_run_is_still_stale_and_an_operators_is_not(tmp_path):
    marker = tmp_path / "home" / ".openclaw" / "watchdog.off"
    dead = _dead_pid()
    r = _stale_check(tmp_path, f'echo "kit-run pid={dead} start=4242 at=1" > "{marker}"')
    assert "rc=0" in r.stdout, r.stderr
    r = _stale_check(tmp_path, f': > "{marker}"')
    assert "rc=1" in r.stdout
    r = _stale_check(tmp_path, f'printf "maintenance" > "{marker}"')
    assert "rc=1" in r.stdout
