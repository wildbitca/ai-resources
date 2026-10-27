"""Two concurrent teams on one repository never collide (3.2.0 C1): own run id, handoff, output directory, session names,
write scope; shared state is appended or replaced atomically; nothing deadlocks."""
from __future__ import annotations

import json
import os
import re
import threading

import pytest

from ai_resources import run_ledger, scope_guard
from orchestrator_fakes import OK, dispatches, finish, make

VERDICT_ROLES = ("verifier", "code-reviewer", "security-auditor", "tester")


def drive(e, rid, seen, errors, timeout=30):
    """Run one team to its end, recording every dispatch it was handed and writing its report file like a child would."""
    try:
        res = e.next(rid)
        for _ in range(120):
            if res["status"] in ("completed", "failed", "cancelled"):
                return
            if any(a["type"] == "ask" for a in res["actions"]):
                res = e.approve(rid)
                continue
            ds = dispatches(res)
            if not ds:
                return
            for a in ds:
                seen.setdefault(rid, []).append(a)
                out = a["cwd"] and os.path.join(a["cwd"], a["run_dir"], a["role"])
                os.makedirs(out, exist_ok=True)
                with open(os.path.join(out, f"{a['step']}.md"), "w", encoding="utf-8") as fh:
                    fh.write(f"report of {rid}\n")
                res = finish(e, rid, a, OK + ("Verdict: PASS\n" if a["role"] in VERDICT_ROLES else ""))
    except BaseException as exc:                                       # surfaced by the test, never swallowed
        errors.append(exc)


def test_two_teams_on_one_repo_finish_side_by_side_without_sharing_anything(tmp_path):
    e, wf, _ = make(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    a = e.start(wf, "goal alpha", str(repo), run_id="alpha", params={"write_scope": "src/"})
    b = e.start(wf, "goal beta", str(repo), run_id="beta", params={"write_scope": "tests/"})
    assert (a["status"], b["status"]) == ("running", "running")
    seen: dict[str, list] = {}
    errors: list = []
    threads = [threading.Thread(target=drive, args=(e, rid, seen, errors)) for rid in ("alpha", "beta")]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert not errors, errors
    assert e.status("alpha")["status"] == e.status("beta")["status"] == "completed"
    # own handoff file, with its own goal and nothing of the other team's
    hand = {r: e.store.handoff_path(r) for r in ("alpha", "beta")}
    assert hand["alpha"] != hand["beta"]
    assert "goal alpha" in hand["alpha"].read_text() and "goal beta" not in hand["alpha"].read_text()
    assert "goal beta" in hand["beta"].read_text() and "goal alpha" not in hand["beta"].read_text()
    # own output directory .agent-output/<run>/<role>/, on disk, never overwritten by the other team
    for rid, other in (("alpha", "beta"), ("beta", "alpha")):
        files = sorted((repo / ".agent-output" / rid).rglob("*.md"))
        assert files and all(f.read_text() == f"report of {rid}\n" for f in files)
        for d in seen[rid]:
            assert d["run_dir"] == f".agent-output/{rid}"
            assert f".agent-output/{rid}/{d['role']}/" in d["prompt"] and f".agent-output/{other}" not in d["prompt"]
            assert not re.search(r"\.agent-output/(planner|implementer|tester|verifier)/", d["prompt"])
    # unique session/task names: no two dispatches of the two teams (or of one team) share a label
    labels = [d["label"] for ds in seen.values() for d in ds]
    assert len(labels) == len(set(labels)) and all(l.startswith(("alpha-", "beta-")) for l in labels)


def test_two_teams_that_want_the_same_scope_do_not_overwrite_each_other_the_second_waits_then_runs(tmp_path):
    e, wf, _ = make(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    first = e.start(wf, "one", str(repo), run_id="one")
    second = e.start(wf, "two", str(repo), run_id="two")
    assert (first["status"], second["status"]) == ("running", "queued")
    seen: dict[str, list] = {}
    errors: list = []
    drive(e, "one", seen, errors)                                          # finishing releases the claim and promotes
    assert not errors and e.status("two")["status"] == "running"
    drive(e, "two", seen, errors)
    assert not errors and e.status("two")["status"] == "completed"
    assert sorted(p.name for p in (repo / ".agent-output").iterdir()) == ["one", "two"]


def test_run_ids_are_unique_even_when_minted_at_the_same_instant():
    from ai_resources.orchestrator.state import new_run_id
    ids = {new_run_id("feature", now=1_000_000.0) for _ in range(300)}
    assert len(ids) == 300


def test_the_prompt_names_the_run_directory_in_workflows_that_wrote_a_shared_path():
    import glob
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = []
    for f in glob.glob(str(root / "workflows" / "*.workflow.yaml")):
        for n, line in enumerate(open(f, encoding="utf-8"), 1):
            if re.search(r"\.agent-output/(?!handoff|readonly|parallel-group|\{\{)[A-Za-z<]", line):
                bad.append(f"{os.path.basename(f)}:{n}")
    assert not bad, f"workflow prompts that still write a shared .agent-output/<role>/ path: {bad}"


def test_the_ledger_appends_are_atomic_under_concurrent_writers(tmp_path):
    path = tmp_path / "dispatch.jsonl"
    n_threads, per = 8, 40
    gate = threading.Barrier(n_threads)

    def go(i):
        gate.wait()
        for k in range(per):
            run_ledger.append({"mode": "full", "outcome": "pass", "agents": 3, "steps": k, "rework": 0, "run": f"r{i}",
                               "pad": "x" * 600}, path)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(n_threads)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert not any(t.is_alive() for t in threads)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == n_threads * per
    assert {(r["run"], r["steps"]) for r in rows} == {(f"r{i}", k) for i in range(n_threads) for k in range(per)}


@pytest.mark.parametrize("module,writer", [("setup.lanes_render", "_atomic_write"), ("model_rerender", "_atomic_write")])
def test_shared_file_writers_use_a_unique_temp_file(module, writer, tmp_path):
    """Two processes replacing one shared file never collide on a fixed `<name>.tmp` (the O_EXCL / unlink race)."""
    import importlib
    mod = importlib.import_module(f"ai_resources.{module}")
    target = tmp_path / "shared.yaml"
    gate = threading.Barrier(8)
    errors: list = []

    def go(i):
        gate.wait()
        try:
            for k in range(25):
                getattr(mod, writer)(target, f"writer {i} write {k}\n" + "y" * 2000)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not errors, errors
    text = target.read_text()
    assert text.startswith("writer ") and text.count("writer ") == 1                  # one whole write, never a mix
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shared.yaml"]


def test_scope_claims_of_two_teams_cannot_both_win_and_a_finished_run_frees_its_claim(tmp_path):
    live = {"a": True, "b": True}
    assert scope_guard.claim("/repo", "a", ["src/"], lambda r: live.get(r, False), root=tmp_path) == []
    assert scope_guard.claim("/repo", "b", ["src/api"], lambda r: live.get(r, False), root=tmp_path) == ["a: src"]
    live["a"] = False
    assert scope_guard.claim("/repo", "b", ["src/api"], lambda r: live.get(r, False), root=tmp_path) == []
