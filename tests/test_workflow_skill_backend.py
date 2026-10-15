"""The generated workflow skill's step table shows each step's backend (ADR 0005)."""
from __future__ import annotations

from ai_resources import generate


def _doc(steps):
    return {"name": "t", "description": "d", "trigger": "when t", "steps": steps}


def _rows(md: str) -> list[str]:
    return [line for line in md.splitlines() if line.startswith("|")]


def test_header_has_backend_column():
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([{"id": "a", "subagent_type": "planner"}]))
    assert _rows(md)[0] == "| Step | Agent | Backend | Parallel group |"


def test_step_without_backend_renders_claude_code():
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([{"id": "a", "subagent_type": "planner"}]))
    assert "| `a` | `planner` | claude-code | — |" in _rows(md)


def test_openclaw_step_renders_agent_id():
    step = {"id": "a", "subagent_type": "code-reviewer", "backend": "openclaw-agent", "agentId": "agy-flash",
            "execution_hints": {"parallel_group": "post-test"}}
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
    assert "| `a` | `code-reviewer` | openclaw-agent (`agy-flash`) | post-test |" in _rows(md)


def test_explicit_claude_code_renders_claude_code():
    step = {"id": "a", "subagent_type": "planner", "backend": "claude-code"}
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
    assert "| `a` | `planner` | claude-code | — |" in _rows(md)


# -- `backend: lane` (ADR-0007) ----------------------------------------------------------------------------

LANE = "lane (resolved at dispatch from the setup lanes table)"


def test_lane_step_renders_the_resolved_at_dispatch_label():
    step = {"id": "review", "subagent_type": "code-reviewer", "backend": "lane"}
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
    assert f"| `review` | `code-reviewer` | {LANE} | — |" in _rows(md)


def test_scope_writer_label_carries_its_write_scope():
    step = {"id": "tests", "subagent_type": "tester", "backend": "lane", "write_scope": ["tests/", "docs/x.md"],
            "execution_hints": {"parallel_group": "g"}}
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
    assert f"| `tests` | `tester` | {LANE}, write scope: tests/, docs/x.md | g |" in _rows(md)


def test_integrating_writer_label_follows_the_workflow_level_role():
    steps = [{"id": "build", "subagent_type": "implementer", "backend": "lane"}]
    md = generate._workflow_skill_md("t.workflow.yaml", _doc(steps))
    assert f"| `build` | `implementer` | {LANE}, integrating writer | — |" in _rows(md)
    other = {**_doc([{"id": "build", "subagent_type": "generalPurpose", "backend": "lane"}]),
             "integrating_writer": "generalPurpose"}
    md = generate._workflow_skill_md("t.workflow.yaml", other)
    assert f"| `build` | `generalPurpose` | {LANE}, integrating writer | — |" in _rows(md)


def test_second_member_label_and_no_agent_id_ever():
    step = {"id": "r2", "subagent_type": "code-reviewer", "backend": "lane",
            "execution_hints": {"parallel_group": "g", "lane_member": "second"}}
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
    assert f"| `r2` | `code-reviewer` | {LANE}, second member | g |" in _rows(md)
    assert "kit-lane" not in md and "agentId" not in md


def test_existing_labels_are_unchanged_by_the_lane_label():
    assert generate._step_backend_label({"backend": "claude-code"}) == "claude-code"
    assert generate._step_backend_label({}) == "claude-code"
    assert generate._step_backend_label({"backend": "openclaw-agent", "agentId": "a"}) == "openclaw-agent (`a`)"
