"""The lanes example workflow (ADR-0007 phase 3): `workflows/feature-lanes.workflow.yaml`.

The explicit `openclaw-agent` sample stays in `feature-cross-provider.workflow.yaml` (ADR-0005; its pinned tests
are untouched). This file pins the shape of the all-lane example: scope writers verified in sequence, the
integrating writer, a review with a second member, the security audit alone and the kit's landing path.
"""
from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
PATH = REPO / "workflows" / "feature-lanes.workflow.yaml"


def _doc() -> dict:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))


def test_the_lanes_example_is_a_lane_workflow_with_a_verified_landing_chain():
    doc = _doc()
    steps = {s["id"]: s for s in doc["steps"]}
    assert not [s for s in doc["steps"] if s.get("backend") == "openclaw-agent"]
    assert not any("agentId" in s for s in doc["steps"]), "a lane step never names an agent"
    assert not any("model" in s for s in doc["steps"]), "there is no model field"
    assert doc["integrating_writer"] == "implementer"
    assert steps["implement"]["integrates"] == ["tests", "docs"]
    assert steps["tests"]["write_scope"] == ["tests/"] and steps["docs"]["write_scope"] == ["docs/"]
    assert steps["verify-tests"]["verifies"] == ["tests"] and steps["verify-docs"]["verifies"] == ["docs"]
    order = [s["id"] for s in doc["steps"]]
    assert order.index("verify-tests") < order.index("verify-docs") < order.index("implement")   # sequential
    groups: dict = {}
    for s in doc["steps"]:
        g = (s.get("execution_hints") or {}).get("parallel_group")
        if g:
            groups.setdefault(g, set()).add(s["id"])
    assert groups == {"scope-writers": {"tests", "docs"}, "review": {"review", "review-second"}}
    assert steps["review-second"]["execution_hints"]["lane_member"] == "second"
    for sid in ("verify-tests", "verify-docs", "verify-implement", "verify-final", "security"):
        assert not (steps[sid].get("execution_hints") or {}).get("parallel_group")
        assert steps[sid]["backend"] == "lane"
    assert "Update {{handoff_file}}" not in "".join(str(s.get("prompt_template", "")) for s in doc["steps"]
                                                    if s.get("backend") == "lane")


def test_the_generated_skill_lists_every_lane_step_and_no_agent_id():
    from ai_resources import generate

    doc = _doc()
    table = generate._workflow_skill_md(PATH.name, doc)
    lane_steps = [s for s in doc["steps"] if s.get("backend") == "lane"]
    assert table.count("lane (resolved at dispatch from the setup lanes table)") == len(lane_steps)
    assert "| `implement` | `implementer` | lane (resolved at dispatch from the setup lanes table), integrating writer |" in table
    assert "openclaw-agent (`" not in table


def test_the_generated_skill_is_in_sync():
    skill = REPO / "skills" / "workflow-feature-lanes"
    assert (skill / "SKILL.md").is_file() and (skill / ".generated-workflow-skill").is_file()
