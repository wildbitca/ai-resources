"""The retirement of the by-model kit-lane agents (ADR-0019 O1/O2): only an agent setup created AND nothing uses is deleted."""
from __future__ import annotations

import json

from ai_resources.setup.cockpits import openclaw_orchestration as O

from orchestration_host import LANES, ORCHESTRATORS, Host, host_doc, kit_agents, run_setup


def jobs(*agent_ids):
    return 0, json.dumps({"jobs": [{"id": f"j{i}", "agentId": a} for i, a in enumerate(agent_ids)]})


def live(doc, s, runner, state_dir=None):
    return O.live_referenced(doc, s.openclaw.lanes_applied, state_dir=state_dir, runner=runner)


def test_unused_lanes_setup_created_are_removed_from_every_allow_list_then_deleted(tmp_path):
    doc, s = host_doc()
    host = Host(doc)
    run_setup(s, host, tmp_path, referenced=live(doc, s, lambda argv: jobs("devops"), tmp_path / "agents"))
    assert sorted(host.deleted) == sorted(LANES)
    assert not [a for o in ORCHESTRATORS for a in host.doc["agents"]["entries"][o]["subagents"]["allowAgents"]
                if a.startswith("kit-lane-")]
    assert not s.openclaw.lanes_applied                                  # nothing left to track


def test_a_lane_a_cron_job_runs_on_is_kept_and_reported(tmp_path):
    doc, s = host_doc()
    host = Host(doc)
    infos: list[str] = []
    run_setup(s, host, tmp_path, infos=infos,
              referenced=live(doc, s, lambda argv: jobs("kit-lane-cli-haiku55"), tmp_path / "agents"))
    assert "kit-lane-cli-haiku55" not in host.deleted and "kit-lane-cli-haiku55" in host.doc["agents"]["entries"]
    assert sorted(host.deleted) == sorted(set(LANES) - {"kit-lane-cli-haiku55"})
    assert any("kit-lane-cli-haiku55" in m and "kept: a cron job runs on it" in m for m in infos)
    # the record keeps the kept id so a later run can retire it once nothing uses it
    assert s.openclaw.lanes_applied["created_agent_ids"] == ["kit-lane-cli-haiku55"]


def test_a_binding_a_session_and_an_allow_list_setup_did_not_write_each_keep_a_lane(tmp_path):
    doc, s = host_doc()
    doc["bindings"] = [{"agentId": "kit-lane-cli-opus55", "match": {"channel": "slack"}}]
    sessions = tmp_path / "agents" / "kit-lane-cli-sonnet55" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "sessions.json").write_text('{"s1": {"updatedAt": 1}}')
    doc["agents"]["entries"]["devops"]["subagents"]["allowAgents"].append("kit-lane-agy-opus55")
    s.openclaw.lanes_applied["allow_agents_added"]["devops"].remove("kit-lane-agy-opus55")     # the operator's entry
    host = Host(doc)
    infos: list[str] = []
    run_setup(s, host, tmp_path, infos=infos, referenced=live(doc, s, lambda argv: jobs(), tmp_path / "agents"))
    kept = {"kit-lane-cli-opus55", "kit-lane-cli-sonnet55", "kit-lane-agy-opus55"}
    assert set(host.deleted) == set(LANES) - kept
    text = "\n".join(infos)
    assert "a channel binding routes to it" in text and "it has sessions" in text and "devops lists it in allowAgents" in text


def test_a_lane_setup_did_not_create_is_never_deleted(tmp_path):
    doc, s = host_doc()
    s.openclaw.lanes_applied["created_agent_ids"].remove("kit-lane-cli-haiku55")
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert "kit-lane-cli-haiku55" not in host.deleted and "kit-lane-cli-haiku55" in host.doc["agents"]["entries"]
    assert sorted(host.deleted) == sorted(set(LANES) - {"kit-lane-cli-haiku55"})
    removable, kept = O.plan_kit_lanes(host.doc, s.openclaw.lanes_applied, lambda _a: "")
    assert removable == [] and kept == {"kit-lane-cli-haiku55": "not created by setup"}


def test_nothing_is_deleted_when_the_cron_list_cannot_be_read(tmp_path):
    doc, s = host_doc()
    host = Host(doc)
    run_setup(s, host, tmp_path, referenced=live(doc, s, lambda argv: (1, "gateway down"), tmp_path / "agents"))
    assert host.deleted == [] and all(lane in host.doc["agents"]["entries"] for lane in LANES)
    assert O.plan_kit_lanes(host.doc, s.openclaw.lanes_applied,
                            live(doc, s, lambda argv: (0, "not json"), None))[0] == []


def test_setup_never_deletes_a_kit_role_agent_or_an_operator_agent(tmp_path):
    doc, s = host_doc()
    doc["agents"]["entries"]["my-lane-notes"] = {"name": "x"}
    s.openclaw.lanes_applied["created_agent_ids"] += ["my-lane-notes", "kit-role-planner"]
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert set(host.deleted) <= set(LANES)
    assert "my-lane-notes" in host.doc["agents"]["entries"] and "kit-role-planner" in host.doc["agents"]["entries"]
    removable, _ = O.plan_kit_lanes(doc, s.openclaw.lanes_applied, lambda _a: "")
    assert all(a.startswith("kit-lane-") for a in removable)


def test_the_old_writer_is_gone_so_no_path_prunes_on_its_own():
    import importlib
    import pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ai_resources.setup.cockpits.openclaw_lanes")


def test_a_rejected_allow_list_cleanup_deletes_nothing(tmp_path):
    doc, s = host_doc()
    host = Host(doc)
    errors: list[str] = []
    ok = lambda patch, dry_run=False, replace_paths=None, **k: (False, "rejected") if replace_paths else host.apply(patch, dry_run, replace_paths)  # noqa: E731
    removed, kept = O.migrate_kit_lanes(s, doc, referenced=lambda _a: "", apply_patch=ok, delete_agent=host.delete,
                                        error=errors.append, info=lambda m: None)
    assert removed == [] and host.deleted == [] and set(kept) == set(LANES) and errors


def test_the_workspace_of_a_removed_lane_goes_only_when_it_lies_under_the_recorded_root(tmp_path):
    doc, s = host_doc()
    root = tmp_path / "kit-lanes"
    inside, outside = root / LANES[0], tmp_path / "elsewhere" / LANES[1]
    for d in (inside, outside):
        d.mkdir(parents=True)
    s.openclaw.lanes_applied.update({"workspace_root": str(root), "created_workspaces": [str(inside), str(outside)]})
    host = Host(doc)
    gone: list = []
    O.migrate_kit_lanes(s, doc, referenced=lambda _a: "", apply_patch=host.apply, delete_agent=host.delete,
                        remove_dir=gone.append, info=lambda m: None)
    assert [str(p) for p in gone] == [str(inside)]


def test_roles_are_written_before_the_lanes_go(tmp_path):
    doc, s = host_doc()
    host = Host(doc)
    agents = kit_agents()
    run_setup(s, host, tmp_path)
    assert all(a.id in host.doc["agents"]["entries"] for a in agents)
