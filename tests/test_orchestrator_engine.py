"""The orchestrator engine over a fake dispatcher (ADR-0015 D3, acceptance 13): order, groups, returns, sign-off, approval."""
from __future__ import annotations

import pytest

from ai_resources.orchestrator import engine as eng
from ai_resources.orchestrator import limits, workflow_loader as wl
from orchestrator_fakes import (FLOW, MODELS, OK, Clock, Runner, dispatches, drive_to, finish, make, reply, write_flow)


def start(e, wf, **kw):
    kw.setdefault("models", MODELS)
    return e.start(wf, "add a thing", "/work/repo", **kw)


def test_the_first_step_is_dispatched_with_everything_the_child_needs(tmp_path):
    e, wf, runner = make(tmp_path)
    run = start(e, wf, domain="")
    assert run["status"] == "running" and run["worktree"]["branch"].startswith("openclaw/team-")
    [d] = dispatches(e.next(run["id"]))
    assert (d["step"], d["role"], d["agent"], d["attempt"]) == ("plan", "planner", "kit-role-planner", 1)
    assert d["cwd"] == run["worktree"]["path"] and d["timeout_s"] == 1200 and not d["parallel"]
    assert d["handoff_file"].endswith("handoff.md") and run["id"] in d["handoff_file"]
    p = d["prompt"]
    assert "agents/roles/planner.md" in p and "skills/common-system-design/SKILL.md" in p
    assert "do NOT edit it" in p and "Blocked: true | false" in p and "add a thing" in p
    assert "{{" not in p and "$AGENT_KIT" not in p


def test_steps_run_in_workflow_order_and_next_is_idempotent(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    first = e.next(run["id"])
    again = e.next(run["id"])
    assert [a["type"] for a in again["actions"]] == ["wait"] and len(dispatches(first)) == 1
    res = finish(e, run["id"], dispatches(first)[0])
    # the plan must be approved before the first writer
    [ask] = res["actions"]
    assert ask["type"] == "ask" and ask["kind"] == "approval" and ask["step"] == "implement"
    assert res["status"] == "awaiting_approval"
    res = e.approve(run["id"])
    assert [(a["step"], a["role"]) for a in dispatches(res)] == [("implement", "implementer")]
    assert dispatches(res)[0]["timeout_s"] == 9000


def test_the_plan_approval_pauses_until_approve_and_cancel_ends_it(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    finish(e, run["id"], dispatches(e.next(run["id"]))[0])
    assert e.next(run["id"])["actions"][0]["type"] == "ask"
    assert e.status(run["id"])["approval"]["state"] == "pending"
    e.approve(run["id"])
    with pytest.raises(eng.EngineError):
        e.approve(run["id"])
    other = start(e, wf, run_id="r-2")
    finish(e, "r-2", dispatches(e.next("r-2"))[0])
    assert e.cancel("r-2")["status"] == "cancelled"
    assert e.next("r-2")["actions"][0]["type"] == "cancelled"


def test_a_workflow_that_never_writes_has_no_approval_pause(tmp_path):
    text = """\
name: read-only
description: x
steps:
  - id: look
    subagent_type: explore
    prompt_template: look
  - id: write-up
    subagent_type: planner
    prompt_template: plan
"""
    e, wf, _ = make(tmp_path, text=text)
    assert wf.first_writer() == ""
    run = start(e, wf)
    res = e.next(run["id"])
    res = finish(e, run["id"], dispatches(res)[0])
    assert dispatches(res)[0]["step"] == "write-up"


def test_read_only_group_members_are_dispatched_together_and_joined_before_the_next_step(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "review", wf)
    both = dispatches(res)
    assert sorted(d["step"] for d in both) == ["review", "security"] and all(d["parallel"] for d in both)
    res = finish(e, run["id"], both[0], OK + "Verdict: PASS\n")
    assert [a["type"] for a in res["actions"]] == ["wait"]                       # the other member is still running
    res = finish(e, run["id"], both[1], OK + "Verdict: PASS\n")
    assert [d["step"] for d in dispatches(res)] == ["verify"]


def test_a_group_with_a_writer_runs_sequentially():
    text = FLOW.replace("  - id: test\n    subagent_type: tester\n", "  - id: test\n    subagent_type: tester\n"
                        "    execution_hints: {parallel_group: post}\n")
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        wf = wl.load(str(write_flow(pathlib.Path(d), text)))
    assert all(s.group == "" for s in wf.steps) and wf.notes


def test_a_blocked_member_returns_to_the_named_step_after_the_join(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    both = dispatches(drive_to(e, run["id"], "review", wf))
    res = finish(e, run["id"], next(d for d in both if d["step"] == "security"),
                 reply(Blocked="true", Return_to_step="implement", Block_reason="secret in logs"))
    assert [a["type"] for a in res["actions"]] == ["wait"]
    res = finish(e, run["id"], next(d for d in both if d["step"] == "review"), OK + "Verdict: PASS\n")
    [d] = dispatches(res)
    assert (d["step"], d["attempt"]) == ("implement", 2)
    st = e.status(run["id"])
    assert st["returns"] == {"implement": 1} and st["steps"]["test"]["state"] == "pending"
    assert st["steps"]["plan"]["state"] == "done"


def test_blocked_returns_three_times_and_the_fourth_fails_the_run(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "implement", wf)
    for n in range(1, 4):
        [d] = dispatches(res)
        assert d["step"] == "implement" and d["attempt"] == n
        res = finish(e, run["id"], d, reply(Blocked="true", Return_to_step="implement", Block_reason=f"try {n}"))
    [d] = dispatches(res)
    assert d["attempt"] == 4
    res = finish(e, run["id"], d, reply(Blocked="true", Return_to_step="implement", Block_reason="again"))
    assert res["status"] == "failed" and res["actions"][0]["type"] == "failed"
    assert "retry cap" in res["actions"][0]["text"]
    assert e.next(run["id"])["actions"][0]["type"] == "failed"


def test_a_blocked_step_defaults_to_the_steps_own_concern_return(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "test", wf)
    [d] = dispatches(res)
    res = finish(e, run["id"], d, reply(Blocked="true", Block_reason="red suite"))
    assert dispatches(res)[0]["step"] == "implement"                           # on_concern_return_to of `test`


def test_a_ready_from_a_non_verifier_is_rejected_and_the_step_is_run_again(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "implement", wf)
    [d] = dispatches(res)
    res = finish(e, run["id"], d, OK + "Verdict: READY\n")
    [again] = dispatches(res)
    assert (again["step"], again["attempt"]) == ("implement", 2)
    st = e.status(run["id"])
    assert "only the verifier signs a run off" in st["steps"]["implement"]["reason"]
    assert st["handoff"]


def test_the_verifier_may_sign_off_and_the_run_completes(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "verify", wf)
    [d] = dispatches(res)
    assert d["role"] == "verifier" and d["agent"] == "kit-role-verifier" and d["timeout_s"] == 1800
    res = finish(e, run["id"], d, OK + "Verdict: READY\n")
    assert res["status"] == "completed" and res["actions"][0]["type"] == "done"
    assert "verification: READY" in open(e.store.handoff_path(run["id"])).read()


def test_the_verifier_in_a_writers_session_is_not_independent(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "verify", wf)
    [d] = dispatches(res)
    res = e.report(run["id"], "verify", text=OK + "Verdict: PASS\n", session="s-implement-1", agent=d["agent"])
    assert dispatches(res)[0]["step"] == "verify" and dispatches(res)[0]["attempt"] == 2
    assert "writer's session" in e.status(run["id"])["steps"]["verify"]["reason"]


def test_the_verifier_on_another_agent_is_refused(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(drive_to(e, run["id"], "verify", wf))
    e.report(run["id"], "verify", text=OK + "Verdict: PASS\n", session="s-v", agent="kit-role-implementer")
    assert "not on kit-role-verifier" in e.status(run["id"])["steps"]["verify"]["reason"]


def test_a_verifier_reply_without_a_verdict_is_not_accepted(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(drive_to(e, run["id"], "verify", wf))
    res = finish(e, run["id"], d, OK)
    assert dispatches(res)[0]["attempt"] == 2                                  # not accepted: the verifier runs again
    assert "no verdict" in e.status(run["id"])["steps"]["verify"]["reason"]


def test_a_missing_session_id_is_recorded_as_a_limitation_not_a_failure(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(drive_to(e, run["id"], "verify", wf))
    res = e.report(run["id"], "verify", text=OK + "Verdict: PASS\n", agent=d["agent"])
    assert res["status"] == "completed"
    notes = e.status(run["id"])["independence"]["notes"]
    assert any("no session id" in n for n in notes)


def test_a_checker_family_equal_to_a_writer_fails_the_run_at_start(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "g", "/w", models={**MODELS, "verifier": "sonnet"})
    assert run["status"] == "failed" and "not independent of writer" in run["failure"]
    run2 = e.start(wf, "g", "/w", models={**MODELS, "verifier": "mystery-model"}, run_id="r2")
    assert run2["status"] == "failed" and "unknown family" in run2["failure"]


def test_unknown_models_are_allowed_with_the_limitation_recorded(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "g", "/w")
    assert run["status"] == "running" and any("model families unknown" in n for n in run["independence"]["notes"])


def test_a_timeout_is_a_blocked_outcome_and_retries_the_same_step(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(e.next(run["id"]))
    res = e.report(run["id"], "plan", timeout=True)
    [again] = dispatches(res)
    assert (again["step"], again["attempt"]) == ("plan", 2)
    st = e.status(run["id"])
    assert st["steps"]["plan"]["result"] == "timeout" and "timed out after 1200" in st["steps"]["plan"]["reason"]
    assert st["returns"] == {"plan": 1}


def test_custom_role_timeouts_reach_the_dispatch(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf, role_timeouts={"planner": 600})
    assert dispatches(e.next(run["id"]))[0]["timeout_s"] == 600


def test_a_dispatch_error_counts_like_a_blocked_attempt(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    e.next(run["id"])
    res = e.report(run["id"], "plan", error="agent kit-role-planner is not allowed")
    assert dispatches(res)[0]["attempt"] == 2


def test_a_process_killed_mid_run_resumes_at_the_same_step_with_the_same_attempt(tmp_path):
    e, wf, runner = make(tmp_path)
    run = start(e, wf)
    res = drive_to(e, run["id"], "implement", wf)
    [d] = dispatches(res)
    # a new process: a new engine over the same store
    e2 = eng.Engine(e.store, clock=Clock(), runner=runner)
    assert e2.next(run["id"])["actions"][0]["type"] == "wait"                    # not handed out twice by accident
    res2 = e2.resume(run["id"])
    [d2] = dispatches(res2)
    assert (d2["step"], d2["attempt"]) == ("implement", 1) and res2["resumed"] == ["implement"]
    assert d2["prompt"] == d["prompt"]
    done = finish(e2, run["id"], d2)
    assert done["status"] == "running" and dispatches(done)[0]["step"] == "test"


def test_resume_all_resumes_running_runs_and_starts_promotions(tmp_path):
    e, wf, runner = make(tmp_path)
    a = start(e, wf, run_id="a-1")
    e.next("a-1")
    out = eng.Engine(e.store, clock=Clock(), runner=runner).resume_all()
    assert [r["run"] for r in out] == ["a-1"] and dispatches(out[0])[0]["step"] == "plan"


def test_returning_before_the_approval_gate_asks_for_the_plan_again(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(drive_to(e, run["id"], "implement", wf))
    res = finish(e, run["id"], d, reply(Blocked="true", Return_to_step="plan", Block_reason="plan is wrong"))
    assert e.status(run["id"])["approval"]["state"] == "pending"
    assert dispatches(res)[0]["step"] == "plan"
    res = finish(e, run["id"], dispatches(res)[0])
    assert res["actions"][0]["type"] == "ask"


def test_the_handoff_front_matter_follows_the_run(tmp_path):
    import yaml
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(e.next(run["id"]))
    finish(e, run["id"], d, OK + "Plan_ref: .agent-output/planner/p.md\nRequires_tests: yes\n")
    text = open(e.store.handoff_path(run["id"])).read()
    front = yaml.safe_load(text.split("---")[1])
    assert front["refs"]["plan"] == ".agent-output/planner/p.md" and front["requires_tests"] == "yes"
    assert front["step"] == "plan" and front["workflow"] == "demo-flow"
    assert "| plan | planner | done |" in text


def test_a_report_on_a_cancelled_or_unknown_run_or_step_is_handled(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    e.next(run["id"])
    with pytest.raises(eng.EngineError):
        e.report(run["id"], "nope", text=OK)
    with pytest.raises(eng.EngineError):
        e.report(run["id"], "implement", text=OK)               # pending, not running
    e.cancel(run["id"])
    res = e.report(run["id"], "plan", text=OK)
    assert res["ignored"] and res["status"] == "cancelled"
    with pytest.raises(eng.EngineError):
        e.next("no-such-run")


def test_a_duplicate_report_is_ignored(tmp_path):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    [d] = dispatches(e.next(run["id"]))
    finish(e, run["id"], d)
    again = e.report(run["id"], "plan", text=OK)
    assert again.get("ignored") == "already reported"


def test_a_non_git_workspace_gets_no_worktree_and_says_so(tmp_path):
    e, wf, runner = make(tmp_path, git=False)
    run = start(e, wf)
    assert run["worktree"] is None and run["cwd"] == "/work/repo"
    assert any("not a git checkout" in n for n in run["independence"]["notes"])
    assert not any(c[:3] == ["openclaw", "worktrees", "create"] for c in runner.calls)


def test_the_real_feature_workflow_runs_end_to_end_with_a_fake_dispatcher(tmp_path):
    from ai_resources.orchestrator.state import RunStore
    store = RunStore(tmp_path / "runs")
    runner = Runner(tmp_path)
    e = eng.Engine(store, clock=Clock(), runner=runner)
    wf = wl.load("feature-implementation")
    run = e.start(wf, "ship it", "/work/repo", models=MODELS)
    res = e.next(run["id"])
    seen = []
    for _ in range(40):
        acts = res["actions"]
        if acts and acts[0]["type"] in ("done", "failed"):
            break
        if acts and acts[0]["type"] == "ask":
            res = e.approve(run["id"])
            continue
        todo = [a for a in acts if a["type"] == "dispatch"]
        assert todo, acts
        for a in todo:
            seen.append(a["step"])
            verdict = "Verdict: PASS\n" if a["role"] in ("verifier", "code-reviewer", "security-auditor", "tester") else ""
            res = finish(e, run["id"], a, OK + verdict)
    assert res["status"] == "completed"
    assert seen == ["research", "plan", "architect", "implement", "test", "review", "security", "verify", "document"] \
        or sorted(seen[5:7]) == ["review", "security"]
    assert seen[:5] == ["research", "plan", "architect", "implement", "test"] and seen[-2:] == ["verify", "document"]


@pytest.mark.parametrize("verdict", ["request changes", "block", "`request changes`"])
def test_a_code_reviewer_asking_for_changes_is_a_blocked_return(tmp_path, verdict):
    e, wf, _ = make(tmp_path)
    run = start(e, wf)
    both = dispatches(drive_to(e, run["id"], "review", wf))
    finish(e, run["id"], next(d for d in both if d["step"] == "review"), OK + f"Code review verdict: {verdict}\n")
    res = finish(e, run["id"], next(d for d in both if d["step"] == "security"), OK + "Verdict: PASS\n")
    [d] = dispatches(res)
    assert (d["step"], d["attempt"]) == ("implement", 2)
    assert e.status(run["id"])["returns"] == {"implement": 1}


def test_a_workflow_whose_only_writer_is_general_purpose_needs_approval(tmp_path):
    text = """\
name: gp
description: x
steps:
  - id: look
    subagent_type: explore
    prompt_template: look
  - id: act
    subagent_type: generalPurpose
    prompt_template: act
"""
    e, wf, _ = make(tmp_path, text=text)
    assert wf.first_writer() == "act"
    run = start(e, wf)
    res = finish(e, run["id"], dispatches(e.next(run["id"]))[0])
    assert res["status"] == "awaiting_approval"
