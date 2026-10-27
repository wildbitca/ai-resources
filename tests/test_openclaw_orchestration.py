"""OpenClaw as the orchestrator (ADR-0019 D1, C1): allowAgents wiring, limits, bundle-mcp, idempotence, dry-run diff, teardown."""
from __future__ import annotations

import copy

import pytest

from ai_resources.setup.cockpits import openclaw_orchestration as O
from ai_resources.setup.cockpits import openclaw_roles as R

from orchestration_host import (CHANNEL_AGENTS, LANES, OLD_LIMITS, ORCHESTRATORS, Host, host_doc, kit_agents, run_setup)


@pytest.fixture
def agents():
    return kit_agents()


def entries(host):
    return host.doc["agents"]["entries"]


def allow(host, aid):
    return entries(host)[aid]["subagents"]["allowAgents"]


def test_after_auto_the_host_is_an_orchestrator_host(agents, tmp_path):
    doc, s = host_doc(agents)
    host = Host(doc)
    run_setup(s, host, tmp_path)
    ids = sorted(a.id for a in agents)
    # 0 unused kit-lane agents
    assert not [a for a in entries(host) if a.startswith("kit-lane-")] and sorted(host.deleted) == sorted(LANES)
    # every kit-role agent carries the matrix chain
    by_id = {a.id: a for a in agents}
    for aid in ids:
        model = entries(host)[aid]["model"]
        assert model["primary"] == by_id[aid]["model"] and model.get("fallbacks", []) == by_id[aid]["fallbacks"]
    assert any(entries(host)[aid]["model"].get("fallbacks") for aid in ids)
    # all 7 orchestrators may spawn the worker and every kit-role agent; no lane id is left in any list
    for orch in ORCHESTRATORS:
        got = set(allow(host, orch))
        assert set(ids) <= got, orch
        assert not [a for a in got if a.startswith("kit-lane-")], orch
        assert ("claude" in got) == (orch != "claude")
    # limits are at least the old values (C1: raised, never lowered, no cap imposed)
    sub = host.doc["agents"]["defaults"]["subagents"]
    assert all(sub[k] >= v for k, v in OLD_LIMITS.items()) and sub["maxSpawnDepth"] >= 4
    assert sub["maxChildrenPerAgent"] >= 16 and sub["maxConcurrent"] >= 32 and sub["runTimeoutSeconds"] == 7200
    # bundle-mcp denied on every kit-role agent, and the per-server denials were kept
    for aid in ids:
        deny = entries(host)[aid]["tools"]["deny"]
        assert "bundle-mcp" in deny and "engram__*" in deny and "supabase-admin__*" in deny


def test_a_second_run_is_a_no_op(agents, tmp_path):
    doc, s = host_doc(agents)
    host = Host(doc)
    run_setup(s, host, tmp_path)
    before_doc, before_state = copy.deepcopy(host.doc), copy.deepcopy(s.openclaw.role_agents_applied)
    writes = host.writes()
    assert run_setup(s, host, tmp_path) is False
    assert host.writes() == writes and host.doc == before_doc and s.openclaw.role_agents_applied == before_state


def test_explore_keeps_its_read_only_deny_because_it_is_a_report_role_and_agy_has_no_extra_deny(agents, tmp_path):
    doc, s = host_doc(agents)
    host = Host(doc)
    run_setup(s, host, tmp_path)
    explore = entries(host)["kit-role-explore"]
    assert explore["model"]["primary"].startswith("agy-cli-lane/")
    assert {"write", "edit", "exec"} <= set(explore["tools"]["deny"])
    for aid, ent in entries(host).items():
        if aid.startswith("kit-role-"):
            assert not {"message", "sessions_spawn", "cron"} & set(ent["tools"]["deny"]), aid
    assert not hasattr(__import__("ai_resources.setup.cockpits.openclaw_agy", fromlist=["x"]), "AGY_TOOL_DENY")


def test_operator_additions_to_allow_agents_are_kept_and_the_union_is_fill_only(agents, tmp_path):
    doc, s = host_doc(agents)
    doc["agents"]["entries"]["devops"]["subagents"]["allowAgents"].append("my-own-agent")
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert "my-own-agent" in allow(host, "devops")
    # the operator's entry is not in the record: teardown takes back only what setup added
    assert "my-own-agent" not in s.openclaw.role_agents_applied["allow_agents_added"]["devops"]


def test_a_wildcard_allow_list_is_left_alone(agents, tmp_path):
    doc, s = host_doc(agents)
    doc["agents"]["entries"]["ai"]["subagents"]["allowAgents"] = ["*"]
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert allow(host, "ai") == ["*"]


def test_limits_are_never_lowered(agents, tmp_path):
    doc, s = host_doc(agents)
    doc["agents"]["defaults"]["subagents"].update({"maxConcurrent": 64, "maxSpawnDepth": 9})
    host = Host(doc)
    run_setup(s, host, tmp_path)
    sub = host.doc["agents"]["defaults"]["subagents"]
    assert sub["maxConcurrent"] == 64 and sub["maxSpawnDepth"] == 9 and sub["maxChildrenPerAgent"] >= 16
    assert "maxConcurrent" not in s.openclaw.role_agents_applied["limits_prev"]


def test_unset_limits_are_filled_and_absent_orchestrators_are_not_invented(agents, tmp_path):
    doc, s = host_doc(agents)
    doc["agents"]["defaults"].pop("subagents")
    del doc["agents"]["entries"]["security"]
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert host.doc["agents"]["defaults"]["subagents"] == O.LIMIT_FLOORS
    assert "security" not in entries(host)


def test_the_orchestrators_are_main_and_every_channel_agent_but_no_kit_agent(agents):
    doc, _ = host_doc(agents)
    assert O.orchestrator_ids(doc) == sorted(ORCHESTRATORS)
    assert O.orchestrator_ids(doc, ["ghost"]) == sorted(ORCHESTRATORS)          # an id the config lacks is ignored


def test_a_kit_role_agent_the_operator_edited_still_gets_the_resource_guard(agents, tmp_path):
    doc, s = host_doc(agents)
    doc["agents"]["entries"]["kit-role-planner"]["model"] = {"primary": "anthropic/claude-opus-5-5"}
    host = Host(doc)
    run_setup(s, host, tmp_path)
    ent = entries(host)["kit-role-planner"]
    assert ent["model"]["primary"] == "anthropic/claude-opus-5-5" and "bundle-mcp" in ent["tools"]["deny"]


def test_the_dry_run_diff_lists_the_planned_set_and_writes_nothing(agents, tmp_path):
    doc, s = host_doc(agents)
    host = Host(doc)
    infos: list[str] = []
    assert run_setup(s, host, tmp_path, dry_run=True, infos=infos) is False
    assert host.writes() == 0 and host.doc == host_doc(agents)[0]
    text = "\n".join(infos)
    assert "agents added 0" in text and "changed" in text
    for orch in ORCHESTRATORS:
        assert f"allowAgents {orch}: +" in text
    assert "agents.defaults.subagents.maxConcurrent: 3 -> 32" in text
    assert "kit-lane agents removed" in text and all(lane in text for lane in LANES)


def test_a_restart_class_key_is_deferred_not_written(agents, tmp_path):
    doc, s = host_doc(agents)
    host = Host(doc)
    warns: list[str] = []
    changed = O.configure(
        s, host.doc, kit_ids=[a.id for a in agents], referenced=lambda _a: "", apply_patch=host.apply,
        classify=lambda p: ["agents.defaults.subagents.maxConcurrent"], delete_agent=host.delete,
        change_records=lambda *a, **k: [], warn=warns.append)
    assert changed is False and host.patches == [] and host.deleted == [] and "restart" in warns[0]


def test_teardown_restores_what_setup_added_and_keeps_the_operator_values(agents, tmp_path):
    doc, s = host_doc(agents)
    doc["agents"]["entries"]["devops"]["subagents"]["allowAgents"].append("my-own-agent")
    host = Host(doc)
    run_setup(s, host, tmp_path)
    host.doc["agents"]["defaults"]["subagents"]["maxChildrenPerAgent"] = 99       # the operator raised it later
    frag = O.restore_fragment(s.openclaw.role_agents_applied, host.doc)
    host.apply(frag, replace_paths=O.allow_paths(frag))
    ids = {a.id for a in agents}
    assert not ids & set(allow(host, "main")) and "my-own-agent" in allow(host, "devops")
    sub = host.doc["agents"]["defaults"]["subagents"]
    assert sub["maxChildrenPerAgent"] == 99 and sub["maxConcurrent"] == 3 and sub["maxSpawnDepth"] == 2


MAX_PRIMARY_ROLES = ("planner", "software-architect", "implementer", "crashlytics-fixer", "sentry-fixer",
                     "terraform-maintainer", "crossplane-upjet-maintainer")


def test_max_primary_roles_fall_back_to_agy_cells_never_to_api_credits_and_run_through_claude_cli(agents, tmp_path):
    """O4 + the runtime pin: a Claude (Max) primary is pinned to claude-cli, and its recorded fallbacks are agy cells."""
    doc, s = host_doc(agents)
    host = Host(doc)
    run_setup(s, host, tmp_path)
    seen = 0
    for a in agents:
        if a["role"] not in MAX_PRIMARY_ROLES or a.id != R.agent_id_for(a["role"]):
            continue
        ent = entries(host)[a.id]
        assert ent["model"]["primary"].startswith("anthropic/"), a.id
        fb = ent["model"]["fallbacks"]
        assert fb and all(ref.startswith("agy-cli-lane/") for ref in fb), (a.id, fb)
        assert ent["models"][ent["model"]["primary"]] == {"agentRuntime": {"id": "claude-cli"}}, a.id
        seen += 1
    assert seen == len(MAX_PRIMARY_ROLES)
    stub = host.doc["models"]["providers"]["agy-cli-lane"]["models"]
    assert {m["id"] for m in stub} >= {ref.split("/", 1)[1] for a in agents for ref in a["fallbacks"]
                                       if ref.startswith("agy-cli-lane/")}
