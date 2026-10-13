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
