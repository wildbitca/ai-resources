"""Fail-closed hold of every agy lane candidate while ADR-0007 §11 is not Accepted (architect B2, B3, gap 4)."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from ai_resources import roles_policy
from ai_resources.scoring import lanes as scoring_lanes
from ai_resources.setup import lanes_render
from ai_resources.setup.cockpits import openclaw_lanes
from test_openclaw_lanes_setup import LANES, _run, _s, host  # noqa: F401

ADR = Path(__file__).resolve().parents[1] / "docs" / "decisions" / "0007-subscription-lanes.md"
IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
BLOCK = {"roles": {"implementer": ["claude-cli:sonnet", "agy:gemini-3.8-flash-low"],
                   "explore": ["agy:gemini-3.8-flash-low"]},
         "second": {"code-reviewer": ["agy:gemini-3.1-pro-low"]}}


def _section_status() -> str:
    """The status of ADR-0007 section 11, parsed from the section's own `**Status**:` line (no other line counts)."""
    text = ADR.read_text()
    start = re.search(r"^### \d+\. Antigravity lane.*$", text, re.M)
    assert start, "ADR-0007 section 11 not found"
    body = text[start.end():]
    nxt = re.search(r"^#{2,3} ", body, re.M)
    section = body[: nxt.start()] if nxt else body
    m = re.search(r"^\*\*Status\*\*: (.+)$", section, re.M)
    assert m, "section 11 has no `**Status**:` line"
    return m.group(1).strip()


def test_the_agy_constant_is_true_iff_the_adr_section_status_line_reads_exactly_accepted():
    """Both directions, on the exact status (plan C2): 'Proposed', 'Proposed-deferred' and 'Accepted with
    conditions' do not satisfy it."""
    assert lanes_render.AGY_LANE_ADR_ACCEPTED is (_section_status() == "Accepted")


def test_accepted_is_necessary_and_not_sufficient_the_lane_is_still_closed_by_default():
    """The operator accepted section 11 on 2026-10-16; the lane stays opt-in, off by default, and the unproven
    gates keep every agy agent out of state and the writer."""
    from ai_resources import agy_lane_gates
    assert lanes_render.AGY_LANE_ADR_ACCEPTED is True
    assert not agy_lane_gates.all_proven()
    frag = lanes_render.state_from_block(BLOCK, claude_ids=IDS)
    assert all(a["backend"] == "claude-cli" for a in frag["agents"].values())
    assert openclaw_lanes.writable_backends() == {"claude-cli"}


def test_agy_candidates_are_held_with_a_recorded_reason_and_never_become_agents(monkeypatch):
    # the original B3 contract: while section 11 is not Accepted (constant False), every candidate is held
    monkeypatch.setattr(lanes_render, "AGY_LANE_ADR_ACCEPTED", False)
    frag = lanes_render.state_from_block(BLOCK, claude_ids=IDS)
    assert all(a["backend"] == "claude-cli" for a in frag["agents"].values())
    assert frag["roles"]["implementer"]["candidates"] == ["openclaw-agent:kit-lane-cli-sonnet55"]
    assert frag["roles"]["explore"]["candidates"] == []
    assert "code-reviewer" not in frag["second"]
    reasons = {(h["role"], h["candidate"]): h["reason"] for h in frag["held"]}
    assert reasons[("implementer", "agy:gemini-3.8-flash-low")] == "held: Antigravity lane not accepted"
    assert len(frag["held"]) == 3


def test_held_candidates_are_visible_in_explain(monkeypatch):
    monkeypatch.setattr(lanes_render, "AGY_LANE_ADR_ACCEPTED", False)
    frag = lanes_render.state_from_block(BLOCK, claude_ids=IDS)
    text = lanes_render.explain_lanes(frag)["implementer"]
    assert "held: Antigravity lane not accepted" in text and "agy:gemini-3.8-flash-low" in text


def test_with_section_11_accepted_the_flag_off_reason_is_recorded_and_still_never_an_agent():
    frag = lanes_render.state_from_block(BLOCK, claude_ids=IDS)
    assert all(a["backend"] == "claude-cli" for a in frag["agents"].values())
    reasons = {(h["role"], h["candidate"]): h["reason"] for h in frag["held"]}
    assert reasons[("implementer", "agy:gemini-3.8-flash-low")] == "held: --lanes-antigravity is off"
    assert len(frag["held"]) == 3
    assert "held: --lanes-antigravity is off" in lanes_render.explain_lanes(frag)["implementer"]


def test_a_block_without_agy_has_no_held_key():
    frag = lanes_render.state_from_block({"roles": {"implementer": ["claude-cli:sonnet"]}}, claude_ids=IDS)
    assert "held" not in frag


def test_the_writer_never_writes_an_agy_agent_even_when_the_state_carries_one(host):
    lanes = copy.deepcopy(LANES)
    lanes["agents"]["kit-lane-agy-gemini38flashlow"] = {"backend": "agy-cli", "model": "gemini-3.8-flash-low",
                                                        "pool": "agy-gemini"}
    lanes["agents"]["kit-lane-agy-lane"] = {"backend": "agy-cli-lane", "model": "gemini-3.8-flash-low",
                                            "pool": "agy-gemini"}
    assert _run(_s(lanes), host) is True
    doc = host.doc()
    assert not [a for a in doc["agents"]["entries"] if "agy" in a]
    assert not any("agy" in str(i) for i in doc["agents"]["entries"]["main"]["subagents"]["allowAgents"])
    held = openclaw_lanes.held_agents(lanes)
    assert set(held) == {"kit-lane-agy-gemini38flashlow", "kit-lane-agy-lane"}
    assert all("not writable" in r for r in held.values())
    assert openclaw_lanes.WRITABLE_BACKENDS == {"claude-cli"}


@pytest.mark.parametrize("ref,family,pool", [
    ("agy-cli-lane/gemini-3.8-flash-low", "gemini-flash", "agy-gemini"),
    ("agy-cli-lane/gemini-3.1-pro-low", "gemini-pro", "agy-gemini"),
    ("agy-cli-lane/claude-sonnet-5-5", "sonnet", "agy-claude-gpt"),
])
def test_agy_cli_lane_refs_have_a_family_and_a_pool(ref, family, pool):
    assert roles_policy.model_family(ref) == family
    agent = scoring_lanes.LaneAgent("kit-lane-agy-x", "agy-cli-lane", ref)
    assert scoring_lanes.pool_of_agent(agent) == pool
