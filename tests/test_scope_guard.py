"""The collision guard of concurrent runs: scope claims under a lock, and the advisory scope verification (3.2.0 C1)."""
from __future__ import annotations

import subprocess
import sys
import threading

from ai_resources import scope_guard as sg
import pytest

# Integration: real processes / host behaviour; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

ALIVE: set[str] = set()


def live(run_id):
    return run_id in ALIVE


def claim(tmp_path, run, scopes, ws="/repo", holder=False):
    return sg.claim(ws, run, scopes, live, root=tmp_path, holder=holder)


def _kill_holder(tmp_path, run, ws="/repo"):
    """Make `run`'s claim belong to a process that is gone (a finished or crashed holder)."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with sg._locked(ws, tmp_path) as path:
        claims = sg._read(path)
        claims[run] = {**claims[run], "pid": dead.pid, "start": "1"}
        sg._write(path, claims)


def setup_function(_):
    ALIVE.clear()
    ALIVE.update({"a", "b", "c"})


def test_disjoint_scopes_are_both_taken_and_overlapping_ones_name_the_holder(tmp_path):
    assert claim(tmp_path, "a", ["src/api/"]) == []
    assert claim(tmp_path, "b", ["tests"]) == []
    assert claim(tmp_path, "c", ["src/api/v2"]) == ["a: src/api"]


def test_the_root_scope_overlaps_everything(tmp_path):
    assert claim(tmp_path, "a", ["."]) == []
    assert claim(tmp_path, "b", ["docs"]) == ["a: ."]
    ALIVE.discard("a")
    assert claim(tmp_path, "b", ["docs"]) == []                      # a finished run never keeps its claim


def test_claims_are_per_workspace_and_a_run_may_claim_again(tmp_path):
    assert claim(tmp_path, "a", ["."], ws="/one") == [] and claim(tmp_path, "b", ["."], ws="/two") == []
    assert claim(tmp_path, "a", ["src"], ws="/one") == []


def test_two_concurrent_claimants_never_both_win(tmp_path):
    results: dict[str, list[str]] = {}
    gate = threading.Barrier(8)

    def go(run):
        gate.wait()
        results[run] = claim(tmp_path, run, ["src"])
    ALIVE.update(f"r{i}" for i in range(8))
    threads = [threading.Thread(target=go, args=(f"r{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for c in results.values() if not c) == 1


def test_violations_log_paths_outside_the_claim_and_allow_the_runs_own_output(tmp_path):
    assert sg.violations(["tests/a.py", "src/x.py", ".agent-output/r1/tester/t.md", ".agent-output/r2/t.md"],
                         ["tests/"], "r1") == ["src/x.py", ".agent-output/r2/t.md"]
    assert sg.violations(["anything.py"], ["."]) == []


def test_two_ad_hoc_runs_overlapping_on_one_scope_are_refused(tmp_path):
    """An id the engine does not know (`agy run --run team-a`) keeps its claim while its process lives."""
    ALIVE.clear()
    assert claim(tmp_path, "team-a", ["src"], holder=True) == []
    assert claim(tmp_path, "team-b", ["src/deep"], holder=True) == ["team-a: src"]
    assert claim(tmp_path, "team-b", ["docs"], holder=True) == []


def test_a_dead_holders_claim_is_dropped_and_a_pid_reuse_does_not_keep_it(tmp_path):
    ALIVE.clear()
    assert claim(tmp_path, "team-a", ["src"], holder=True) == []
    _kill_holder(tmp_path, "team-a")
    assert claim(tmp_path, "team-b", ["src"], holder=True) == []     # holder gone: dropped
    assert claim(tmp_path, "team-c", ["src"]) == ["team-b: src"]
    with sg._locked("/repo", tmp_path) as path:                      # same pid, other start time = a reused pid
        claims = sg._read(path)
        claims["team-b"]["start"] = "not-this-process"
        sg._write(path, claims)
    assert claim(tmp_path, "team-c", ["src"]) == []


def test_an_engine_claim_lives_and_dies_with_the_run_not_with_the_process_and_old_claims_still_read(tmp_path):
    assert claim(tmp_path, "a", ["src"]) == []
    assert claim(tmp_path, "b", ["src"]) == ["a: src"]               # active run, no holder recorded
    ALIVE.discard("a")
    assert claim(tmp_path, "b", ["src"]) == []                       # finished run: freed although this process lives on
    with sg._locked("/repo", tmp_path) as path:
        path.write_text('{"old": ["lib"]}')                          # the first format: a bare list, no holder
    assert sg.held("/repo", "old", tmp_path) == ["lib"] and claim(tmp_path, "b", ["lib"]) == []
