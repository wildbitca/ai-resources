"""Setup-side admission of agy lane candidates (ADR-0007 section 11, plan 5.3a, AC3.8): one predicate."""
from __future__ import annotations

import copy
import dataclasses

import pytest

from ai_resources import agy_lane_gates as gates_mod, model_pins
from ai_resources.scoring import lanes as scoring_lanes
from ai_resources.setup import lanes_render as lr

IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
BY_ROLE = {"implementer": {"model": "sonnet"}, "tester": {"model": "sonnet"}, "doc-writer": {"model": "sonnet"},
           "verifier": {"model": "opus"}}
BLOCK = {"roles": {"tester": ["agy:claude-sonnet-5-5"], "implementer": ["agy:claude-sonnet-5-5"],
                   "verifier": ["claude-code", "agy:claude-opus-5-5"]}}
SONNET_ID = "kit-lane-agy-sonnet55"
ACCT = "operator@example.test"


@pytest.fixture
def proven(monkeypatch):
    """The constant True and every phase 3 gate proven: the only way the agy lane can open."""
    monkeypatch.setattr(lr, "AGY_LANE_ADR_ACCEPTED", True)
    monkeypatch.setattr(gates_mod, "EVIDENCE",
                        {g.id: dataclasses.replace(g, status=gates_mod.PROVEN, evidence="test-fixture")
                         for g in gates_mod.GATES})


def ack(roles, account=ACCT):
    a = lr.make_ack("google", roles, "2026-10-16T00:00:00+00:00", account=account)
    return {"google": a}


def propose(block=None, *, flag=True, acks=None):
    doc = {"lanes": copy.deepcopy(block or BLOCK)}
    return lr.propose(doc, BY_ROLE, "single-model", orchestrators=["main"], acks=acks, claude_ids=IDS,
                      gates={"antigravity": flag})


def has_agy(obj) -> bool:
    return "kit-lane-agy" in repr(obj)


# --- the data shape ---------------------------------------------------------------------------------------

def test_an_agy_candidate_becomes_an_agy_cli_lane_agent_with_a_lane_ref(proven):
    frag = lr.state_from_block(BLOCK, claude_ids=IDS, gates={"antigravity": True}, acks=ack(["tester", "implementer"]))
    assert frag["agents"][SONNET_ID] == {"backend": "agy-cli-lane", "model": "agy-cli-lane/claude-sonnet-5-5",
                                         "pool": "agy-claude-gpt"}
    assert "held" not in frag or all(h["candidate"] != "agy:claude-sonnet-5-5" for h in frag["held"])


def test_a_slug_outside_the_lane_list_is_a_block_error():
    with pytest.raises(lr.LanesBlockError):
        lr.state_from_block({"roles": {"tester": ["agy:made-up-slug"]}}, claude_ids=IDS)


# --- AC3.8: each gate closed in turn ----------------------------------------------------------------------

CLOSED = {
    "flag-off": dict(flag=False, acks=ack(["tester", "implementer", "verifier"]), why="--lanes-antigravity is off"),
    "no-ack": dict(flag=True, acks={}, why="no valid Google acknowledgement"),
    "stale-text": dict(flag=True, acks={"google": {**ack(["tester", "implementer", "verifier"])["google"],
                                                   "text_sha256": "0" * 64}}, why="no valid Google acknowledgement"),
    "no-account": dict(flag=True, acks=ack(["tester", "implementer", "verifier"], account=""),
                       why="no valid Google acknowledgement"),
    "role-not-listed": dict(flag=True, acks=ack(["doc-writer"]), why="does not list"),
}


@pytest.mark.parametrize("case", sorted(CLOSED))
def test_each_closed_gate_keeps_every_agy_agent_out_of_state_and_lanes_yaml(proven, case):
    c = CLOSED[case]
    lanes = propose(flag=c["flag"], acks=c["acks"])
    assert lanes["agents"] == {}
    assert not has_agy(lanes["agents"]) and not has_agy(lanes["roles"])
    reasons = {h["candidate"]: h["reason"] for h in lanes["held"]}
    assert reasons and all(c["why"] in r for r in reasons.values()), reasons
    doc = lr.render_doc({**lanes, "gates": {"antigravity": c["flag"]}}, BY_ROLE, "single-model", c["acks"])
    assert not has_agy(doc["agents"]) and not has_agy(doc["resolved"]["roles"]) \
        and not has_agy(doc["resolved"]["second"])


def test_with_the_gates_open_the_agent_is_in_state_lanes_yaml_and_resolved(proven):
    acks = ack(["tester", "implementer", "verifier"])
    lanes = propose(acks=acks)
    assert SONNET_ID in lanes["agents"] and "kit-lane-agy-opus55" in lanes["agents"]
    doc = lr.render_doc({**lanes, "gates": {"antigravity": True}}, BY_ROLE, "single-model", acks)
    assert SONNET_ID in doc["agents"]
    assert doc["resolved"]["roles"]["tester"]["lane"] == f"openclaw-agent:{SONNET_ID}"
    assert doc["resolved"]["roles"]["tester"]["model"] == "agy-cli-lane/claude-sonnet-5-5"
    assert doc["gates"] == {"antigravity": True} and doc["acks"]["google"]["roles"] == \
        ["implementer", "tester", "verifier"]


def test_a_partial_ack_keeps_the_agent_for_the_listed_role_and_never_routes_the_unlisted_one(proven):
    acks = ack(["tester"])
    lanes = propose(acks=acks)
    assert SONNET_ID in lanes["agents"]                                    # one listed role names it
    held = {(h["role"], h["candidate"]) for h in lanes["held"]}
    assert ("implementer", "agy:claude-sonnet-5-5") in held
    doc = lr.render_doc({**lanes, "gates": {"antigravity": True}}, BY_ROLE, "single-model", acks)
    assert doc["resolved"]["roles"]["tester"]["lane"] == f"openclaw-agent:{SONNET_ID}"
    assert doc["resolved"]["roles"]["implementer"]["lane"] == "claude-code"
    assert not any(f["lane"].endswith(SONNET_ID) for f in doc["resolved"]["roles"]["implementer"]["fallbacks"])


def test_an_unproven_gate_holds_everything_with_a_message_naming_it(monkeypatch):
    monkeypatch.setattr(lr, "AGY_LANE_ADR_ACCEPTED", True)            # constant True, evidence untouched: G1.. unproven
    lanes = propose(acks=ack(["tester", "implementer", "verifier"]))
    assert lanes["agents"] == {}
    assert all("G1" in h["reason"] and "not proven" in h["reason"] for h in lanes["held"])


def test_the_constant_false_keeps_the_historic_reason(monkeypatch):
    monkeypatch.setattr(lr, "AGY_LANE_ADR_ACCEPTED", False)
    frag = lr.state_from_block(BLOCK, claude_ids=IDS, gates={"antigravity": True}, acks=ack(["tester"]))
    assert {h["reason"] for h in frag["held"]} == {lr.HELD_AGY_REASON}


def test_the_admission_predicate_holds_an_agent_a_state_from_an_older_kit_still_carries(proven):
    lanes = {"agents": {SONNET_ID: {"backend": "agy-cli-lane", "model": "agy-cli-lane/claude-sonnet-5-5",
                                     "pool": "agy-claude-gpt"},
                        "kit-lane-cli-sonnet55": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                                  "pool": "claude-subscription"}},
             "roles": {"tester": {"candidates": [f"openclaw-agent:{SONNET_ID}"]}}, "second": {}}
    stale = {"google": {**ack(["tester"])["google"], "text_sha256": "1" * 64}}
    held = lr.agy_admission(lanes, {"antigravity": True}, stale)
    assert set(held) == {SONNET_ID} and "no valid Google acknowledgement" in held[SONNET_ID]
    assert lr.agy_admission(lanes, {"antigravity": True}, ack(["tester"])) == {}
    assert SONNET_ID in lr.agy_admission(lanes, {"antigravity": False}, ack(["tester"]))


# --- valid_acks and the ack text --------------------------------------------------------------------------

def test_valid_acks_drops_a_stale_text_and_an_ack_without_an_account():
    good = ack(["tester"])
    assert set(lr.valid_acks(good)) == {"google"}
    assert lr.valid_acks({"google": {**good["google"], "text_sha256": "2" * 64}}) == {}
    assert lr.valid_acks(ack(["tester"], account="")) == {}
    a = lr.make_ack("anthropic", ["tester"], "2026-10-16")
    assert set(lr.valid_acks({"anthropic": a})) == {"anthropic"}


def test_the_google_text_carries_the_stated_residuals_and_names_the_account():
    text = lr.ACK_TEXT["google"]
    for needle in ("openclaw message", "crontab", "systemd-run", "curl", "static weights", "role count",
                   "spawn itself", "HOME", "Google account", "third-party"):
        assert needle in text, needle
    assert "spawn itself" in lr.ACK_TEXT["anthropic"]                     # the partial-ack residual is symmetric
    assert ack(["tester"])["google"]["account"] == ACCT


# --- the render path --------------------------------------------------------------------------------------

def test_the_render_path_passes_effective_ids_valid_acks_and_never_the_global_pin(monkeypatch, proven):
    seen = []
    real = scoring_lanes.resolve_lanes

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)
    monkeypatch.setattr(scoring_lanes, "resolve_lanes", spy)
    acks = {**ack(["tester"]), "google": {**ack(["tester"])["google"], "text_sha256": "3" * 64}}
    lanes = {"agents": {}, "roles": {"tester": {"candidates": ["claude-code"]}}, "second": {}}
    lr.render_doc(lanes, BY_ROLE, "single-model", acks)
    lr.propose({"lanes": {"roles": {"tester": ["claude-code"]}}}, BY_ROLE, "single-model")
    assert seen
    for kw in seen:
        assert kw.get("effective_ids") == model_pins.effective()
        assert "operator_pin" not in (kw.get("gates") or {})
        assert not (kw.get("acks") or {}).get("google")                 # the stale ack never reaches the resolver


def test_a_phase_two_state_renders_the_same_keys_without_gate_or_ack_fields():
    lanes = {"agents": {"kit-lane-cli-sonnet55": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                                  "pool": "claude-subscription"}},
             "roles": {"tester": {"candidates": ["openclaw-agent:kit-lane-cli-sonnet55", "claude-code"]}},
             "second": {}}
    doc = lr.render_doc(lanes, BY_ROLE, "single-model", {"anthropic": lr.make_ack("anthropic", ["tester"], "d")})
    # ADR-0018: every lanes.yaml carries the routing matrix; the gate and ack fields still appear only when they exist
    assert list(doc) == ["mode", "agents", "roles", "second", "resolved", "routing", "digest"]


# --- version drift (plan 5.7) ---------------------------------------------------------------------------------

def test_a_lane_agent_that_is_now_tier_two_is_named_in_the_drift_warnings():
    lanes = {"agents": {"kit-lane-cli-sonnet46": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-4-6",
                                                  "pool": "claude-subscription"},
                        "kit-lane-cli-sonnet55": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                                  "pool": "claude-subscription"}},
             "roles": {"implementer": {"candidates": ["openclaw-agent:kit-lane-cli-sonnet46",
                                                      "openclaw-agent:kit-lane-cli-sonnet55"]}}, "second": {}}
    got = lr.version_drift(lanes, BY_ROLE, IDS)
    assert [d["agent"] for d in got] == ["kit-lane-cli-sonnet46"]
    text = lr.drift_warnings(lanes, BY_ROLE)
    assert any("kit-lane-cli-sonnet46 runs claude-sonnet-4-6" in w and "never for a checker" in w for w in text)
    assert not any("kit-lane-cli-sonnet55" in w for w in text)
