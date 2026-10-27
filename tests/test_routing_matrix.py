"""The single routing matrix and the balancing rule (ADR-0018). Pure and offline."""
from __future__ import annotations

import pytest

from ai_resources.scoring import routing
from ai_resources.setup import lanes_render, profiles


def test_every_cell_is_valid_and_has_a_known_pool_and_class():
    merged = routing.merged()
    assert set(merged) == {"team", "chat"}
    for roles in merged.values():
        for cells in roles.values():
            for key in ("primary", "secondary"):
                if cells.get(key):
                    assert routing.validate_cell(cells[key]) is None
                    assert routing.pool_of(cells[key]) in routing.POOLS
            assert cells["class"] in routing.CLASS_POOLS


def test_antigravity_is_a_primary_route_for_read_roles_and_writers_stay_on_claude_code():
    team = routing.merged()["team"]
    assert team["explore"]["primary"].startswith("agy:")
    assert team["code-reviewer"]["primary"].startswith("agy:")
    for role in ("implementer", "tester", "doc-writer", "generalPurpose", "package-upgrade"):
        assert team[role]["primary"].startswith("claude-code")
    for role in ("verifier", "security-auditor"):
        assert team[role]["primary"] == "claude-code:opus"


def test_chat_plane_primary_is_anthropic_api_and_light_work_drains_gemini_credits():
    chat = routing.merged()["chat"]
    assert routing.pool_of(chat["main"]["primary"]) == routing.POOL_ANTHROPIC_API
    for role in ("utility", "summarization", "explore"):
        assert routing.pool_of(chat[role]["primary"]) == routing.POOL_GEMINI_API
    assert chat["heartbeat"]["gated"]            # stays on Haiku until Gemini tool-calling is proven


def test_matrix_agrees_with_the_profile_lane_candidates():
    """Where a profile declares lane candidates for a role, the matrix primary backend is one of them."""
    doc = profiles.load_profile("balanced", "single-model")
    team = routing.merged(doc)["team"]
    for role, cands in doc["lanes"]["roles"].items():
        backends = {c.split(":", 1)[0] for c in cands}
        assert team[role]["primary"].split(":", 1)[0] in backends, role


def test_profile_override_replaces_one_cell_and_rejects_bad_ones():
    doc = {"routing": {"team": {"explore": {"primary": "claude-cli:haiku"}}}}
    assert routing.merged(doc)["team"]["explore"]["primary"] == "claude-cli:haiku"
    assert routing.merged(doc)["team"]["planner"] == routing.TEAM_MATRIX["planner"]
    with pytest.raises(routing.RoutingError):
        routing.merged({"routing": {"team": {"explore": {"primary": "gpt:x"}}}})
    with pytest.raises(routing.RoutingError):
        routing.merged({"routing": {"bogus": {}}})


def test_choose_pool_spills_to_the_next_pool_and_unknown_is_available():
    assert routing.choose_pool("team-read", {})[0] == routing.POOL_AGY_GEMINI
    use = {routing.POOL_AGY_GEMINI: 0.9, routing.POOL_MAX: 0.3}
    pool, why = routing.choose_pool("team-read", use)
    assert pool == routing.POOL_MAX and "spill" in why
    hot = {routing.POOL_AGY_GEMINI: 0.95, routing.POOL_MAX: 0.9}
    pool, why = routing.choose_pool("team-read", hot)
    assert pool == routing.POOL_MAX and "least used" in why
    with pytest.raises(routing.RoutingError):
        routing.choose_pool("nope", {})


def test_balancing_distributes_work_across_all_four_pools():
    drained = {routing.choose_pool(c, {})[0] for c in routing.CLASS_POOLS}
    assert drained == {routing.POOL_MAX, routing.POOL_AGY_GEMINI, routing.POOL_ANTHROPIC_API, routing.POOL_GEMINI_API}


def test_lanes_yaml_carries_the_routing_section_and_it_moves_the_digest():
    lanes = {"agents": {}, "roles": {}, "second": {}}
    a = lanes_render.render_doc(lanes, {}, "single-model")
    assert a["routing"]["matrix"]["team"]["explore"]["primary"] == {"cell": "agy:gemini-3.8-flash-high",
                                                                    "pool": routing.POOL_AGY_GEMINI}
    assert a["routing"]["balancing"]["spill_at"] == routing.SPILL_AT
    b = lanes_render.render_doc({**lanes, "routing": {"team": {"explore": {"primary": "claude-cli:haiku"}}}},
                                {}, "single-model")
    assert a["digest"] != b["digest"]
    assert lanes_render.doc_digest({k: v for k, v in a.items()}) == a["digest"]
