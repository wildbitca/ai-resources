"""MCP scoping for kit lane agents (ADR-0007 OQ-5, V7): default-deny of bundle MCP, explicit per-role opt-in."""
from __future__ import annotations

import copy
import json

import pytest

from ai_resources.setup import lanes_render
from ai_resources.setup.cockpits import openclaw_lanes
from test_openclaw_lanes_setup import BASE, LANES, OPUS, SONNET, FakeOpenClaw, _run, _s, host  # noqa: F401

SONNET_ID = "kit-lane-cli-sonnet55"
OPUS_ID = "kit-lane-cli-opus55"


def _with_servers(host, servers):
    doc = host.doc()
    doc["mcp"] = {"servers": {n: {"command": "x"} for n in servers}}
    host.path.write_text(json.dumps(doc))


def _deny(host, aid):
    return (host.doc()["agents"]["entries"][aid].get("tools") or {}).get("deny")


def test_default_denies_every_bundle_mcp_server_on_each_kit_lane_agent(host):
    _with_servers(host, ["supabase", "engram"])
    assert _run(_s(), host) is True
    assert _deny(host, SONNET_ID) == ["bundle-mcp"]
    assert _deny(host, OPUS_ID) == ["bundle-mcp"]


def test_non_kit_agents_never_get_the_deny(host):
    _run(_s(), host)
    after = host.doc()["agents"]["entries"]
    assert "tools" not in after["other"] and "tools" not in after["claude"]
    assert after["main"]["tools"] == {"exec": {"mode": "full"}}


def test_opt_in_servers_are_kept_and_the_rest_are_denied_by_name(host):
    _with_servers(host, ["supabase", "engram", "my.server"])
    lanes = copy.deepcopy(LANES)
    lanes["agents"][SONNET_ID]["mcp_servers"] = ["engram"]
    _run(_s(lanes), host)
    assert _deny(host, SONNET_ID) == ["my-server__*", "supabase__*"]
    assert _deny(host, OPUS_ID) == ["bundle-mcp"]


def test_existing_operator_deny_entries_are_kept_and_second_run_is_idempotent(host):
    doc = host.doc()
    doc["agents"]["entries"][SONNET_ID] = {"name": SONNET_ID, "workspace": str(host.path.parent / SONNET_ID),
                                           "tools": {"deny": ["browser"]}}
    host.path.write_text(json.dumps(doc))
    s = _s()
    _run(s, host)
    assert _deny(host, SONNET_ID) == ["browser", "bundle-mcp"]
    snap = host.path.read_text()
    _run(s, host)
    assert host.path.read_text() == snap


def test_teardown_removes_only_the_kit_tokens_from_a_surviving_agent(host):
    doc = host.doc()
    doc["agents"]["entries"][SONNET_ID] = {"name": SONNET_ID, "workspace": str(host.path.parent / SONNET_ID),
                                           "tools": {"deny": ["browser"]}}
    host.path.write_text(json.dumps(doc))
    s = _s()
    _run(s, host)
    assert s.openclaw.lanes_applied["mcp_deny_added"] == {SONNET_ID: ["bundle-mcp"], OPUS_ID: ["bundle-mcp"]}
    frag, _ = openclaw_lanes.restore_fragment(s.openclaw.lanes_applied, host.doc())
    assert frag["agents"]["entries"][SONNET_ID]["tools"]["deny"] == ["browser"]
    assert frag["agents"]["entries"][OPUS_ID]["tools"]["deny"] is None


def test_removing_an_opt_in_later_tightens_the_deny(host):
    _with_servers(host, ["supabase", "engram"])
    lanes = copy.deepcopy(LANES)
    lanes["agents"][SONNET_ID]["mcp_servers"] = ["engram", "supabase"]
    s = _s(lanes)
    _run(s, host)
    assert _deny(host, SONNET_ID) is None or _deny(host, SONNET_ID) == []
    s.profile.lanes["agents"][SONNET_ID]["mcp_servers"] = ["engram"]
    _run(s, host)
    assert _deny(host, SONNET_ID) == ["supabase__*"]


def test_scope_findings_flag_a_lane_agent_that_is_not_scoped(host):
    _run(_s(), host)
    assert openclaw_lanes.mcp_scope_findings(LANES, host.doc()) == []
    doc = host.doc()
    doc["agents"]["entries"][SONNET_ID]["tools"]["deny"] = []
    msgs = openclaw_lanes.mcp_scope_findings(LANES, doc)
    assert len(msgs) == 1 and SONNET_ID in msgs[0]


def test_profile_block_mcp_key_validates_and_maps_to_agents():
    block = {"roles": {"implementer": ["claude-cli:sonnet"]}, "mcp": {"implementer": ["engram"]}}
    assert lanes_render.validate_block(block) == []
    frag = lanes_render.state_from_block(block, claude_ids={"sonnet": "claude-sonnet-5-5"})
    (aid,) = frag["agents"]
    assert frag["agents"][aid]["mcp_servers"] == ["engram"]
    assert frag["mcp"] == {"implementer": ["engram"]}
    assert "mcp: engram" in lanes_render.explain_lanes(frag)["implementer"]


@pytest.mark.parametrize("bad", [{"implementer": "engram"}, {"nope-role": ["x"]}, ["engram"]])
def test_profile_block_mcp_rejects_bad_shapes(bad):
    block = {"roles": {"implementer": ["claude-cli:sonnet"]}, "mcp": bad}
    assert lanes_render.validate_block(block)


# --- loosening / bookkeeping across runs --------------------------------------------------------------

def test_loosening_to_an_opt_in_drops_bundle_mcp_and_denies_by_name(host):
    _with_servers(host, ["supabase", "engram"])
    s = _s()
    _run(s, host)
    assert _deny(host, SONNET_ID) == ["bundle-mcp"]
    s.profile.lanes["agents"][SONNET_ID]["mcp_servers"] = ["engram"]
    _run(s, host)
    assert _deny(host, SONNET_ID) == ["supabase__*"]
    assert "bundle-mcp" not in _deny(host, SONNET_ID)
    assert s.openclaw.lanes_applied["mcp_deny_added"][SONNET_ID] == ["supabase__*"]
    assert s.openclaw.lanes_applied["mcp_deny_added"][OPUS_ID] == ["bundle-mcp"]


def test_full_opt_in_keeps_operator_tokens_and_clears_the_deny_when_none(host):
    _with_servers(host, ["supabase", "engram"])
    doc = host.doc()
    doc["agents"]["entries"][SONNET_ID] = {"name": SONNET_ID, "workspace": str(host.path.parent / SONNET_ID),
                                           "tools": {"deny": ["browser"]}}
    host.path.write_text(json.dumps(doc))
    s = _s()
    _run(s, host)
    assert _deny(host, SONNET_ID) == ["browser", "bundle-mcp"]
    assert _deny(host, OPUS_ID) == ["bundle-mcp"]
    for aid in (SONNET_ID, OPUS_ID):
        s.profile.lanes["agents"][aid]["mcp_servers"] = ["supabase", "engram"]
    _run(s, host)
    assert _deny(host, SONNET_ID) == ["browser"]
    assert _deny(host, OPUS_ID) is None
    applied = s.openclaw.lanes_applied.get("mcp_deny_added") or {}
    assert not applied.get(SONNET_ID) and not applied.get(OPUS_ID)
