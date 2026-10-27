"""Workflow steps take the default backend `route`: every role resolves through the matrix, never a hard-coded claude-code."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from ai_resources import cli
from ai_resources.orchestrator import workflow_loader
from ai_resources.orchestrator.workflow_loader import WorkflowError
from ai_resources.scoring import routing
from ai_resources.setup import detection

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((REPO / "workflows").glob("*.workflow.yaml"))
EXPLICIT = {"claude-code", "openclaw-agent", "agy"}


@pytest.fixture(autouse=True)
def _agy_signed_in(monkeypatch):
    monkeypatch.setattr(detection, "agy_active", lambda: True)
    monkeypatch.setattr(detection, "codex_active", lambda: False)


def _steps(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("steps") or []


def test_there_are_workflows():
    assert len(WORKFLOWS) >= 15


def test_every_workflow_validates():
    spec = importlib.util.spec_from_file_location("validate_kit_route", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.check_workflows(mod.check_skills(), mod.check_roles())
    assert mod.failures == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_step_defaults_to_claude_code(path):
    wf = workflow_loader.load(str(path))
    for raw, step in zip(_steps(path), wf.steps):
        assert step.backend == (raw.get("backend") or "route")
        assert step.backend == "route" or raw.get("backend") in EXPLICIT


def test_the_bundled_workflows_pin_no_backend():
    pinned = [(p.name, s.get("id")) for p in WORKFLOWS for s in _steps(p) if s.get("backend")]
    assert pinned == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_step_role_resolves_through_the_matrix(path):
    for step in workflow_loader.load(str(path)).steps:
        if step.role not in routing.ROLES:
            continue            # `generalPurpose`-style aliases are checked by validate_kit against agents/roles
        route = routing.resolve(step.role, {})
        assert route.cell and not route.blocked, (path.name, step.id, step.role)


def test_a_dry_route_prints_for_every_role_of_every_step(capsys):
    roles = {s.get("subagent_type") for p in WORKFLOWS for s in _steps(p)} & set(routing.ROLES)
    assert roles
    for role in sorted(roles):
        assert cli.main(["route", role, "--json"]) == 0
    assert capsys.readouterr().out.count('"cell"') == len(roles)


def test_backend_lane_is_rejected(tmp_path):
    wf = tmp_path / "t.workflow.yaml"
    wf.write_text("name: t\ndescription: d\ntrigger: t\nsteps:\n  - id: a\n    subagent_type: planner\n"
                  "    backend: lane\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="unknown backend 'lane'"):
        workflow_loader.load(str(wf))
