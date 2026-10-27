"""Fakes for the orchestrator tests: a clock, a workflow file and canned replies."""
from __future__ import annotations

from pathlib import Path

from ai_resources.orchestrator import engine as eng
from ai_resources.orchestrator import workflow_loader as wl
from ai_resources.orchestrator.state import RunStore

FLOW = """\
name: demo-flow
description: A small workflow for the engine tests.
domain: "{{domain}}"
steps:
  - id: plan
    subagent_type: planner
    skills: [common-system-design]
    handoff_to: implement
    prompt_template: |
      Workspace: {{workspace}}. Goal: {{user_goal}}. Update {{handoff_file}} when done. See $AGENT_KIT/x.
  - id: implement
    subagent_type: implementer
    skills: [common-best-practices]
    on_concern_return_to: plan
    handoff_to: test
    prompt_template: |
      Implement in {{workspace}}.
  - id: test
    subagent_type: tester
    on_concern_return_to: implement
    handoff_to: review
    prompt_template: |
      Test.
  - id: review
    subagent_type: code-reviewer
    on_concern_return_to: implement
    handoff_to: verify
    execution_hints: {parallel_group: post}
    prompt_template: |
      Review.
  - id: security
    subagent_type: security-auditor
    on_concern_return_to: implement
    handoff_to: verify
    execution_hints: {parallel_group: post}
    prompt_template: |
      Audit.
  - id: verify
    subagent_type: verifier
    on_concern_return_to: implement
    prompt_template: |
      Verify.
"""

OK = "Status: success\nBlocked: false\n"


class Clock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        self.t += 1.0
        return self.t


def write_flow(tmp_path: Path, text: str = FLOW) -> Path:
    p = tmp_path / "demo.workflow.yaml"
    p.write_text(text)
    return p


def make(tmp_path, *, text: str = FLOW):
    """The engine, the demo workflow and the claims directory (write-scope claims never touch the host state)."""
    claims = tmp_path / "claims"
    e = eng.Engine(RunStore(tmp_path / "runs"), clock=Clock(), claims_root=claims)
    return e, wl.load(str(write_flow(tmp_path, text))), claims


MODELS = {"planner": "sonnet", "implementer": "sonnet", "tester": "haiku", "code-reviewer": "sonnet",
          "security-auditor": "opus", "verifier": "opus"}


def reply(**f) -> str:
    return "\n".join(f"{k}: {v}" for k, v in f.items()) + "\n"


def finish(e, run_id, action, text=OK, **kw):
    return e.report(run_id, action["step"], text=text, session=kw.pop("session", f"s-{action['step']}-{action['attempt']}"),
                    agent=action["agent"], **kw)


def dispatches(result):
    return [a for a in result["actions"] if a["type"] == "dispatch"]


def drive_to(e, run_id, upto: str, wf=None, models=None):
    """Report OK for every step before `upto` (approving the plan on the way); returns the result that offers `upto`."""
    res = e.next(run_id)
    for _ in range(80):
        acts = res["actions"]
        disp = {a["step"]: a for a in acts if a["type"] == "dispatch"}
        if upto in disp:
            return res
        if any(a["type"] == "ask" for a in acts):
            res = e.approve(run_id)
            continue
        if disp:
            for a in disp.values():
                text = OK + ("Verdict: PASS\n" if a["role"] in ("verifier", "code-reviewer", "security-auditor", "tester") else "")
                res = finish(e, run_id, a, text)
            continue
        raise AssertionError(f"drive_to({upto}) is stuck on {acts}")
    raise AssertionError("drive_to did not converge")
