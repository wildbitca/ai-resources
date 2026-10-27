"""The generated workflow skill's step table shows each step's backend ."""
from __future__ import annotations

from ai_resources import generate


def _doc(steps):
    return {"name": "t", "description": "d", "trigger": "when t", "steps": steps}


def _rows(md: str) -> list[str]:
    return [line for line in md.splitlines() if line.startswith("|")]


def test_header_has_backend_column():
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([{"id": "a", "subagent_type": "planner"}]))
    assert _rows(md)[0] == "| Step | Agent | Backend | Parallel group |"


def test_step_without_backend_renders_route():
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([{"id": "a", "subagent_type": "planner"}]))
    assert "| `a` | `planner` | route | — |" in _rows(md)


def test_openclaw_step_renders_agent_id():
    step = {"id": "a", "subagent_type": "code-reviewer", "backend": "openclaw-agent", "agentId": "agy-flash",
            "execution_hints": {"parallel_group": "post-test"}}
    md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
    assert "| `a` | `code-reviewer` | openclaw-agent (`agy-flash`) | post-test |" in _rows(md)


def test_explicit_overrides_render_as_written():
    for backend in ("claude-code", "agy"):
        step = {"id": "a", "subagent_type": "planner", "backend": backend}
        md = generate._workflow_skill_md("t.workflow.yaml", _doc([step]))
        assert f"| `a` | `planner` | {backend} | — |" in _rows(md)


def test_labels():
    assert generate._step_backend_label({}) == "route"
    assert generate._step_backend_label({"backend": "claude-code"}) == "claude-code"
    assert generate._step_backend_label({"backend": "openclaw-agent", "agentId": "a"}) == "openclaw-agent (`a`)"
