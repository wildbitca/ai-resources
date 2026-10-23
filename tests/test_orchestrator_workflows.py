"""Every shipped workflow runs under the orchestrator (ADR-0015 D3): it loads, starts and hands out a first step."""
from __future__ import annotations

import re

import pytest

from ai_resources import repo_root, roles_policy
from ai_resources.orchestrator import engine as eng
from ai_resources.orchestrator import workflow_loader as wl
from ai_resources.setup import role_render
from ai_resources.setup.cockpits import openclaw_roles
from orchestrator_fakes import Clock, Runner, dispatches
from ai_resources.orchestrator.state import RunStore

NAMES = [w["file"].removesuffix(".workflow.yaml") for w in wl.list_workflows()]


def test_every_workflow_file_is_listed():
    assert len(NAMES) == 19 and "feature-lanes" in NAMES


@pytest.mark.parametrize("name", NAMES)
def test_the_workflow_loads_starts_and_hands_out_its_first_step(name, tmp_path):
    wf = wl.load(name)
    e = eng.Engine(RunStore(tmp_path / "runs"), clock=Clock(), runner=Runner(tmp_path))
    run = e.start(wf, "a goal", "/work/repo", run_id="t1")
    assert run["status"] == "running" and run["failure"] == ""
    res = e.next("t1")
    first = [a for a in res["actions"] if a["type"] in ("dispatch", "ask")]
    assert first, res
    for a in dispatches(res):
        assert a["agent"].startswith("kit-role-") and a["timeout_s"] >= 30 and a["prompt"]
        assert "{{" not in a["prompt"], a["step"]            # workflow parameters are filled or point at the goal


@pytest.mark.parametrize("name", NAMES)
def test_every_role_a_workflow_uses_is_a_kit_role_with_an_agent(name):
    roles = {s.role for s in wl.load(name).steps}
    known = set(role_render.role_names(repo_root()))
    assert roles <= known, roles - known


def test_role_agent_ids_are_unique_valid_and_short_for_roles_and_personas():
    ids = [openclaw_roles.agent_id_for(s.base, s.name[len(s.base) + 1:] if s.is_persona else "")
           for s in role_render.load_specs(repo_root(), env={"AI_RESOURCES_SKILLS": "all"})]
    assert len(ids) == len(set(ids)) == 40
    assert all(re.fullmatch(r"[a-z][a-z0-9-]{0,30}", i) for i in ids)


def test_the_read_only_groups_of_the_shipped_workflows_run_in_parallel_and_the_rest_sequentially():
    feature = wl.load("feature-implementation")
    assert [(g[0].id, len(g)) for g in feature.groups() if len(g) > 1] == [("review", 2)]
    infra = wl.load("infra-triage")
    assert all(s.group == "" for s in infra.steps) and infra.notes
    lanes = wl.load("feature-lanes")
    assert [(g[0].id, len(g)) for g in lanes.groups() if len(g) > 1] == [("review", 2)]


def test_the_first_writer_gate_of_each_workflow_is_a_writer_role():
    for name in NAMES:
        wf = wl.load(name)
        gate = wf.first_writer()
        if gate:
            assert wf.step(gate).role in wl.WRITER_ROLES
        else:
            assert not any(s.role in wl.WRITER_ROLES for s in wf.steps)


def test_workflow_parameters_are_filled_from_params_or_point_at_the_goal(tmp_path):
    wf = wl.load("ci-debug")
    e = eng.Engine(RunStore(tmp_path / "runs"), clock=Clock(), runner=Runner(tmp_path))
    e.start(wf, "why is CI red", "/work/repo", run_id="p1", params={"ci_run_url": "https://ci.example/run/7"})
    prompt = dispatches(e.next("p1"))[0]["prompt"]
    assert "https://ci.example/run/7" in prompt and "{{ci_run_url}}" not in prompt
    e.start(wf, "why is CI red", "/work/repo", run_id="p2")
    assert "<ci_run_url: see the goal>" in dispatches(e.next("p2"))[0]["prompt"]
