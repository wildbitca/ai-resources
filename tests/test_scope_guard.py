"""The collision guard of concurrent runs: scope claims under a lock, and the advisory scope verification (3.2.0 C1)."""
from __future__ import annotations

import threading

from ai_resources import scope_guard as sg

ALIVE: set[str] = set()


def live(run_id):
    return run_id in ALIVE


def claim(tmp_path, run, scopes, ws="/repo"):
    return sg.claim(ws, run, scopes, live, root=tmp_path)


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
