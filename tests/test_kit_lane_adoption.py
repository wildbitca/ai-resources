"""Adoption of the by-model kit-lane agents a lost record left behind (3.2.8, ADR-0022).

Real-shaped host (2026-10-28): six `kit-lane-*` agents with the old writer's shape (workspace under `kit-lanes/`, a
`claude-cli` pin per Anthropic ref, `bundle-mcp` denied), no `created_agent_ids` record, and `allowAgents` lists listing
them. Setup adopts exactly those: it fills what a spawn needs to land on the lane's own model and never touches an
operator field, and a second run is a no-op with no `kept: not created by setup` line.
"""
from __future__ import annotations

import copy

from ai_resources.setup.cockpits import openclaw_orchestration as O
from ai_resources.setup.cockpits import openclaw_roles as R

from orchestration_host import LANES, ORCHESTRATORS, Host, host_doc, run_setup

PINS = {ref: {"agentRuntime": {"id": "claude-cli"}} for ref in
        ("anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5")}
FALLBACKS = ["anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5"]
PRIMARY = {"kit-lane-agy-gemini38flashhigh": "agy-cli-lane/gemini-3.8-flash-high",
           "kit-lane-agy-gemini4argonhigh": "agy-cli-lane/gemini-4-argon-high",
           "kit-lane-agy-opus55": "agy-cli-lane/claude-opus-5-5",
           "kit-lane-cli-haiku55": "anthropic/claude-haiku-5-5",
           "kit-lane-cli-opus55": "anthropic/claude-opus-5-5",
           "kit-lane-cli-sonnet55": "anthropic/claude-sonnet-5-5"}


def lane_entry(aid):
    return {"name": aid, "workspace": f"/home/u/.openclaw/kit-lanes/{aid}",
            "agentDir": f"/home/u/.openclaw/agents/{aid}/agent",
            "model": {"primary": PRIMARY[aid], "fallbacks": list(FALLBACKS)},
            "models": copy.deepcopy(PINS), "identity": {"name": aid},
            "tools": {"deny": ["bundle-mcp"]}}


def lost_record_host():
    doc, s = host_doc()
    for aid in LANES:
        doc["agents"]["entries"][aid] = lane_entry(aid)
    s.openclaw.lanes_applied = {}                              # the record is gone: this is the live shape
    doc["plugins"] = {"entries": {"ai-resources": {"config": {"agyBin": "/home/u/.local/bin/agy"}}}}
    return doc, s


def test_a_lane_in_the_old_writers_shape_is_adopted_not_reported_as_kept(tmp_path):
    doc, s = lost_record_host()
    host, infos = Host(doc), []
    run_setup(s, host, tmp_path, infos=infos)
    assert sorted(s.openclaw.lanes_applied["adopted_agent_ids"]) == sorted(LANES)
    assert host.deleted == [] and all(a in host.doc["agents"]["entries"] for a in LANES)
    assert not [m for m in infos if "not created by setup" in m]


def test_adoption_pins_each_lanes_own_model_as_its_spawn_model(tmp_path):
    doc, s = lost_record_host()
    host = Host(doc)
    run_setup(s, host, tmp_path)
    for aid in LANES:
        ent = host.doc["agents"]["entries"][aid]
        assert ent["subagents"]["model"] == {"primary": PRIMARY[aid], "fallbacks": FALLBACKS}


def test_adoption_never_overwrites_an_operator_field(tmp_path):
    doc, s = lost_record_host()
    doc["agents"]["entries"]["kit-lane-cli-opus55"]["subagents"] = {"model": "anthropic/claude-haiku-5-5"}
    doc["agents"]["entries"]["kit-lane-cli-opus55"]["thinkingDefault"] = "high"
    doc["agents"]["entries"]["kit-lane-cli-haiku55"]["tools"]["deny"].append("cron")
    host, infos = Host(doc), []
    run_setup(s, host, tmp_path, infos=infos)
    ent = host.doc["agents"]["entries"]["kit-lane-cli-opus55"]
    assert ent["subagents"]["model"] == "anthropic/claude-haiku-5-5" and ent["thinkingDefault"] == "high"
    assert host.doc["agents"]["entries"]["kit-lane-cli-haiku55"]["tools"]["deny"] == ["bundle-mcp", "cron"]
    # the operator's override is reported once, named, with the model it makes the spawn run on
    hits = [m for m in infos if "kit-lane-cli-opus55" in m and "subagents.model" in m]
    assert len(hits) == 1 and "anthropic/claude-haiku-5-5" in hits[0]


def test_a_missing_runtime_pin_is_filled_only_where_a_sibling_pin_shows_the_cli_route(tmp_path):
    doc, s = lost_record_host()
    del doc["agents"]["entries"]["kit-lane-cli-sonnet55"]["models"]["anthropic/claude-haiku-5-5"]
    doc["agents"]["entries"]["kit-lane-cli-haiku55"]["models"] = {}        # no evidence the lane runs on the CLI
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert host.doc["agents"]["entries"]["kit-lane-cli-sonnet55"]["models"]["anthropic/claude-haiku-5-5"] == \
        {"agentRuntime": {"id": "claude-cli"}}
    assert host.doc["agents"]["entries"]["kit-lane-cli-haiku55"]["models"] == {}


def test_an_orchestrator_missing_an_adopted_lane_in_its_allow_list_gets_it_back(tmp_path):
    doc, s = lost_record_host()
    allow = doc["agents"]["entries"]["devops"]["subagents"]["allowAgents"]
    allow.remove("kit-lane-agy-opus55")
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert "kit-lane-agy-opus55" in host.doc["agents"]["entries"]["devops"]["subagents"]["allowAgents"]


def test_second_run_is_a_noop_without_warnings(tmp_path):
    doc, s = lost_record_host()
    host = Host(doc)
    run_setup(s, host, tmp_path)
    before, writes = copy.deepcopy(host.doc), host.writes()
    infos: list[str] = []
    changed = run_setup(s, host, tmp_path, infos=infos)
    assert not changed and host.writes() == writes and host.doc == before
    assert not [m for m in infos if "kit-lane" in m]


def test_a_lane_that_does_not_match_the_old_writers_shape_is_still_kept(tmp_path):
    doc, s = lost_record_host()
    doc["agents"]["entries"]["kit-lane-cli-haiku55"]["workspace"] = "/srv/mine/haiku"
    host, infos = Host(doc), []
    run_setup(s, host, tmp_path, infos=infos)
    assert "kit-lane-cli-haiku55" not in s.openclaw.lanes_applied["adopted_agent_ids"]
    assert any("kit-lane-cli-haiku55" in m and "not created by setup" in m for m in infos)
    assert "subagents" not in host.doc["agents"]["entries"]["kit-lane-cli-haiku55"]


def test_only_kit_lane_ids_are_adopted(tmp_path):
    doc, s = lost_record_host()
    doc["agents"]["entries"]["mine"] = lane_entry("kit-lane-cli-haiku55") | {
        "name": "mine", "workspace": "/home/u/.openclaw/kit-lanes/mine", "identity": {"name": "mine"}}
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert "mine" not in s.openclaw.lanes_applied["adopted_agent_ids"]
    assert "model" not in (host.doc["agents"]["entries"]["mine"].get("subagents") or {})


def test_a_lane_setup_created_still_follows_the_retirement_path(tmp_path):
    doc, s = host_doc()                                        # the record names the lanes: created by setup
    host = Host(doc)
    run_setup(s, host, tmp_path)
    assert sorted(host.deleted) == sorted(LANES)
    assert not s.openclaw.lanes_applied.get("adopted_agent_ids")


def test_teardown_removes_only_what_adoption_added(tmp_path):
    doc, s = lost_record_host()
    doc["agents"]["entries"]["kit-lane-cli-opus55"]["subagents"] = {"model": "anthropic/claude-haiku-5-5"}
    del doc["agents"]["entries"]["kit-lane-cli-sonnet55"]["models"]["anthropic/claude-haiku-5-5"]
    host = Host(doc)
    run_setup(s, host, tmp_path)
    frag = O.restore_fragment(s.openclaw.role_agents_applied, host.doc)
    ents = frag["agents"]["entries"]
    assert ents["kit-lane-cli-haiku55"]["subagents"]["model"] is None
    assert "kit-lane-cli-opus55" not in ents                    # the operator's override was never ours
    assert ents["kit-lane-cli-sonnet55"]["models"] == {"anthropic/claude-haiku-5-5": None}
    host.apply(frag, replace_paths=O.allow_paths(frag))
    assert not host.doc["agents"]["entries"]["kit-lane-cli-haiku55"].get("subagents")
    assert host.doc["agents"]["entries"]["kit-lane-cli-opus55"]["subagents"]["model"] == "anthropic/claude-haiku-5-5"


def test_the_agy_stub_gains_a_missing_slug_for_an_adopted_agy_lane(tmp_path):
    doc, s = lost_record_host()
    doc["models"] = {"providers": {"agy-cli-lane": {"baseUrl": "http://127.0.0.1:9/", "api": "openai-completions",
                                                    "models": [{"id": "claude-opus-5-5", "name": "claude-opus-5-5"}]}}}
    host = Host(doc)
    run_setup(s, host, tmp_path)
    ids = {m["id"] for m in host.doc["models"]["providers"]["agy-cli-lane"]["models"]}
    assert {"claude-opus-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high"} <= ids


def test_spawn_model_audit_names_every_remaining_override():
    doc, _ = lost_record_host()
    doc["agents"]["defaults"]["subagents"]["model"] = "anthropic/claude-sonnet-5-5"
    doc["agents"]["entries"]["kit-lane-agy-opus55"]["subagents"] = {"model": "anthropic/claude-haiku-5-5"}
    audit = O.audit_spawn_models(doc, ["kit-lane-agy-opus55", "kit-lane-cli-haiku55"])
    assert audit["kit-lane-agy-opus55"] == ("agent subagents.model", "anthropic/claude-haiku-5-5")
    assert audit["kit-lane-cli-haiku55"] == ("agents.defaults.subagents.model", "anthropic/claude-sonnet-5-5")
    del doc["agents"]["defaults"]["subagents"]["model"]
    assert O.audit_spawn_models(doc, ["kit-lane-cli-haiku55"])["kit-lane-cli-haiku55"] == \
        ("own model", "anthropic/claude-haiku-5-5")
    assert R is not None


def test_an_agy_lane_without_an_absolute_agy_bin_is_reported_once_with_the_cause(tmp_path):
    doc, s = lost_record_host()
    doc["plugins"] = {"entries": {"ai-resources": {"config": {"agyBin": "agy"}}}}
    host, msgs = Host(doc), []
    run_setup(s, host, tmp_path, infos=msgs)
    hits = [m for m in msgs if "agyBin is not an absolute path" in m]
    assert len(hits) == 1 and "kit-lane-agy-opus55" in hits[0]
    doc2, s2 = lost_record_host()
    doc2["plugins"] = {"entries": {"ai-resources": {"config": {"agyBin": "/home/u/.local/bin/agy"}}}}
    msgs2: list[str] = []
    run_setup(s2, Host(doc2), tmp_path, infos=msgs2)
    assert not [m for m in msgs2 if "agyBin" in m]
