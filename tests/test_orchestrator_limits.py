"""maxTeams, the FIFO queue and per-role timeouts (ADR-0015 D3, acceptance 14). No spend cap exists."""
from __future__ import annotations

import pytest

from ai_resources.orchestrator import limits
from orchestrator_fakes import OK, dispatches, drive_to, finish, make


def test_role_timeout_defaults_and_overrides():
    assert [limits.role_timeout(r) for r in ("planner", "implementer", "tester", "verifier", "explore")] == [
        1200, 9000, 2700, 1800, 1800]
    assert limits.role_timeout("planner", {"planner": 60}) == 60
    for bad in (5, 10 ** 9, "9000", None, True, -1):
        assert limits.role_timeout("planner", {"planner": bad}) == 1200


def test_max_teams_is_clamped_and_defaults_to_two():
    assert limits.DEFAULT_MAX_TEAMS == 2
    assert [limits.clamp_max_teams(v) for v in (0, -3, 1, 2, "3", "x", None, 99)] == [1, 1, 1, 2, 3, 2, 2, 16]


def test_admission_and_fifo_order():
    runs = [{"id": "a", "status": "running"}, {"id": "b", "status": "awaiting_approval"},
            {"id": "q2", "status": "queued", "queued_at": 20}, {"id": "q1", "status": "queued", "queued_at": 10},
            {"id": "done", "status": "completed"}, {"id": "x", "status": "cancelled"}]
    assert limits.active_count(runs) == 2 and limits.queue_order(runs) == ["q1", "q2"]
    assert limits.admit(runs, 2) == "queued" and limits.admit(runs, 3) == "running"
    assert limits.next_to_promote(runs, 3) == ["q1"] and limits.next_to_promote(runs, 4) == ["q1", "q2"]
    assert limits.next_to_promote(runs, 1) == []


def test_no_spend_cap_exists_in_the_limits():
    names = " ".join(dir(limits)).lower()
    assert "spend" not in names and "usd" not in names and "budget" not in names


def test_three_launches_with_two_teams_queue_the_third_with_a_notice(tmp_path):
    e, wf, _ = make(tmp_path)
    a = e.start(wf, "one", "/w/one", max_teams=2, run_id="a")
    b = e.start(wf, "two", "/w/two", max_teams=2, run_id="b")
    c = e.start(wf, "three", "/w/three", max_teams=2, run_id="c")
    assert (a["status"], b["status"], c["status"]) == ("running", "running", "queued")
    [note] = e.next("c")["actions"]
    assert note["type"] == "notice" and note["kind"] == "queued" and "2 team(s) are working" in note["text"]


def test_a_finished_team_releases_its_slot_and_the_third_starts_in_fifo_order(tmp_path):
    e, wf, _ = make(tmp_path)
    for rid in ("a", "b", "c", "d"):
        e.start(wf, rid, f"/w/{rid}", max_teams=2, run_id=rid)
    assert [e.status(r)["status"] for r in "abcd"] == ["running", "running", "queued", "queued"]
    res = e.cancel("a")
    assert res["promoted"] == ["c"] and e.status("c")["status"] == "running"
    assert e.status("d")["status"] == "queued"
    [d] = dispatches(e.next("c"))
    assert d["step"] == "plan" and d["cwd"] == "/w/c"


def test_a_run_that_completes_or_fails_promotes_the_queue(tmp_path):
    e, wf, _ = make(tmp_path)
    e.start(wf, "a", "/w/a", max_teams=1, run_id="a")
    e.start(wf, "b", "/w/b", max_teams=1, run_id="b")
    assert e.status("b")["status"] == "queued"
    e.next("a")
    res = e.report("a", "plan", error="x")                       # three errors in a row exhaust the retries of `plan`
    for _ in range(3):
        res = e.report("a", "plan", error="x")
    assert res["status"] == "failed" and res["promoted"] == ["b"] and e.status("b")["status"] == "running"


def test_status_lists_the_queue_and_the_active_count(tmp_path):
    e, wf, _ = make(tmp_path)
    for rid in ("a", "b", "c"):
        e.start(wf, rid, f"/w/{rid}", max_teams=2, run_id=rid)
    s = e.status()
    assert s["active"] == 2 and s["queued"] == ["c"] and [r["id"] for r in s["runs"]] == ["a", "b", "c"]
    assert s["max_teams"] == 2


def test_a_run_that_writes_holds_the_whole_checkout_until_it_finishes(tmp_path):
    e, wf, _ = make(tmp_path)
    a = e.start(wf, "a", "/w", run_id="a")
    b = e.start(wf, "b", "/w", run_id="b")
    assert (a["status"], b["status"]) == ("running", "queued")
    assert any("write scope held by a: ." in ev["text"] for ev in b["events"])
    assert e.cancel("a")["promoted"] == ["b"] and e.status("b")["status"] == "running"


def test_disjoint_write_scopes_of_one_workspace_run_together_and_overlapping_ones_queue(tmp_path):
    e, wf, _ = make(tmp_path)
    a = e.start(wf, "a", "/w", run_id="a", params={"write_scope": "src/api, docs/"})
    b = e.start(wf, "b", "/w", run_id="b", params={"write_scope": "tests/"})
    c = e.start(wf, "c", "/w", run_id="c", params={"write_scope": "src/api/v2"})
    assert (a["status"], b["status"], c["status"]) == ("running", "running", "queued")
    assert dispatches(e.next("a"))[0]["cwd"] == dispatches(e.next("b"))[0]["cwd"] == "/w"


def test_a_read_only_team_claims_nothing(tmp_path):
    flow = """name: ro
description: reads only
domain: x
steps:
  - id: look
    subagent_type: explore
    prompt_template: |
      Look.
"""
    e, wf, _ = make(tmp_path, text=flow)
    assert [e.start(wf, rid, "/w", run_id=rid)["status"] for rid in ("a", "b")] == ["running", "running"]



def test_the_timeout_of_each_role_is_carried_by_its_dispatch(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "g", "/w/g", run_id="t")
    seen = {}
    res = e.next("t")
    for _ in range(30):
        for a in dispatches(res):
            seen[a["role"]] = a["timeout_s"]
        if res["status"] in ("completed", "failed"):
            break
        if any(a["type"] == "ask" for a in res["actions"]):
            res = e.approve("t")
            continue
        ds = dispatches(res)
        if not ds:
            break
        for a in ds:
            res = finish(e, "t", a, OK + ("Verdict: PASS\n" if a["role"] in ("verifier", "code-reviewer", "security-auditor",
                                                                              "tester") else ""))
    assert seen["planner"] == 1200 and seen["implementer"] == 9000 and seen["tester"] == 2700 and seen["verifier"] == 1800
    assert seen["code-reviewer"] == 1800


def test_two_concurrent_teams_on_one_repo_keep_their_own_handoff_and_never_deadlock(tmp_path):
    import threading
    e, wf, _ = make(tmp_path)
    got: dict[str, dict] = {}

    def go(rid, scope):
        got[rid] = e.start(wf, f"goal {rid}", "/w", run_id=rid, max_teams=4, params={"write_scope": scope})
    threads = [threading.Thread(target=go, args=(rid, scope)) for rid, scope in (("a", "src/"), ("b", "tests/"), ("c", "src/"))]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    assert not any(t.is_alive() for t in threads)
    statuses = {r: got[r]["status"] for r in "abc"}
    assert statuses["b"] == "running" and sorted([statuses["a"], statuses["c"]]) == ["queued", "running"]
    first = e.next("a")["run"], e.next("b")["run"]
    handoffs = {r: e.store.handoff_path(r) for r in first}
    assert handoffs["a"] != handoffs["b"] and "goal a" in handoffs["a"].read_text() and "goal b" in handoffs["b"].read_text()
