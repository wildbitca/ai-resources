"""Antigravity lane resolution (ADR-0007 section 11, phase 3 P3-S1/S2): gate, tier rule, pins, symmetry.

Pure resolver, fakes only. Agy refs are the real `agy-cli-lane/<slug>` form with candidate V5 slugs; V5 has not
confirmed them (agy_lane_gates G1), so these tests exercise the rule, not the evidence.
"""
from __future__ import annotations

import copy

import pytest

import scoring_fakes as fk
from ai_resources import roles_policy as rp
from ai_resources.scoring import lanes as L

EFFECTIVE = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
CLI_SONNET = L.LaneAgent("kit-lane-cli-sonnet55", "claude-cli", "anthropic/claude-sonnet-5-5", "claude-subscription")
CLI_OPUS = L.LaneAgent("kit-lane-cli-opus55", "claude-cli", "anthropic/claude-opus-5-5", "claude-subscription")
AGY_SONNET = L.LaneAgent("kit-lane-agy-sonnet55", "agy-cli-lane", "agy-cli-lane/claude-sonnet-5-5", "agy-claude-gpt")
AGY_OPUS = L.LaneAgent("kit-lane-agy-opus55", "agy-cli-lane", "agy-cli-lane/claude-opus-5-5", "agy-claude-gpt")
AGY_SONNET46 = L.LaneAgent("kit-lane-agy-sonnet46", "agy-cli-lane", "agy-cli-lane/claude-sonnet-4-6", "agy-claude-gpt")
AGY_GEMINI = L.LaneAgent("kit-lane-agy-gemini38flashlow", "agy-cli-lane",
                         "agy-cli-lane/gemini-3.8-flash-low", "agy-gemini")


def ref(a):
    return f"openclaw-agent:{a.id}"


def resolve(by_role, agents=(), candidates=None, second=None, **kw):
    kw.setdefault("acks", copy.deepcopy(fk.OPEN_ACKS))
    kw.setdefault("gates", dict(fk.OPEN_GATES))
    return L.resolve_lanes(by_role, {a.id: a for a in agents}, candidates or {}, second or {}, **kw)


# --- AC3.1: closed unless flag + ack + ADR ---------------------------------------------------------------

@pytest.mark.parametrize("gates,acks,missing", [
    ({}, {}, "--lanes-antigravity flag"),
    ({"antigravity_flag": True}, {}, "a Google acknowledgement listing verifier"),
    ({}, {"google": {"roles": ["verifier"], "text_sha256": "x"}}, "--lanes-antigravity flag"),
    ({"antigravity_flag": True}, {"google": {"roles": ["verifier"], "text_sha256": "x"}},
     "ADR-0007 Antigravity section Accepted"),
    ({"antigravity_flag": True, "adr_0007_agy_accepted": True},
     {"anthropic": {"roles": ["verifier"], "text_sha256": "x"}}, "a Google acknowledgement listing verifier"),
])
def test_a_checker_on_agy_is_blocked_with_the_adr_citation_unless_every_gate_is_open(gates, acks, missing):
    t = resolve({"verifier": {"model": "opus"}}, [AGY_OPUS], {"verifier": [ref(AGY_OPUS)]},
                gates=gates, acks=acks, effective_ids=EFFECTIVE)
    assert "verifier" in t.blocked
    assert "ADR-0006 section 4 / ADR-0007" in t.blocked["verifier"] and missing in t.blocked["verifier"]


def test_with_every_gate_open_listed_roles_are_admitted_and_unlisted_roles_refused():
    acks = {"google": {"roles": ["tester"], "text_sha256": "x"}}
    by_role = {"tester": {"model": "sonnet"}, "doc-writer": {"model": "sonnet"}}
    t = resolve(by_role, [AGY_SONNET], {"tester": [ref(AGY_SONNET)], "doc-writer": [ref(AGY_SONNET)]},
                acks=acks, effective_ids=EFFECTIVE)
    assert t.roles["tester"].ref == ref(AGY_SONNET)
    assert t.roles["doc-writer"].ref == "claude-code"           # not listed: refused, falls back
    # neither ack opens the other account
    t = resolve({"tester": {"model": "sonnet"}}, [AGY_SONNET, CLI_SONNET],
                {"tester": [ref(AGY_SONNET), ref(CLI_SONNET)]},
                acks={"anthropic": {"roles": ["tester"], "text_sha256": "x"}}, effective_ids=EFFECTIVE)
    assert t.roles["tester"].ref == ref(CLI_SONNET)
    t = resolve({"tester": {"model": "sonnet"}}, [CLI_SONNET], {"tester": [ref(CLI_SONNET)]},
                acks={"google": {"roles": ["tester"], "text_sha256": "x"}}, effective_ids=EFFECTIVE)
    assert t.roles["tester"].ref == "claude-code"


# --- AC3.5: independence across lanes --------------------------------------------------------------------

def test_implementer_on_agy_sonnet_and_verifier_on_agy_opus_is_valid():
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    t = resolve(by_role, [AGY_SONNET, AGY_OPUS], {"implementer": [ref(AGY_SONNET)], "verifier": [ref(AGY_OPUS)]},
                effective_ids=EFFECTIVE)
    assert t.roles["implementer"].ref == ref(AGY_SONNET) and t.roles["verifier"].ref == ref(AGY_OPUS)


def test_a_verifier_on_a_sonnet_family_lane_is_a_conflict():
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "sonnet"}}
    with pytest.raises(L.LaneConflict):
        resolve(by_role, [AGY_SONNET], {"implementer": [ref(AGY_SONNET)], "verifier": [ref(AGY_SONNET)]},
                effective_ids=EFFECTIVE)


def test_a_pinned_gemini_writer_with_an_opus_verifier_is_valid():
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    t = resolve(by_role, [AGY_GEMINI], {"implementer": [ref(AGY_GEMINI)]},
                pins={"implementer": [ref(AGY_GEMINI)]}, effective_ids=EFFECTIVE)
    assert t.roles["implementer"].ref == ref(AGY_GEMINI)
    assert t.roles["verifier"].ref == "claude-code"


# --- AC3.6: weights ----------------------------------------------------------------------------------------

def test_pure_resolver_with_explicit_weights_moves_a_checker_to_agy_only_when_claude_is_scarce():
    """The 0.3 weight is passed by the test: no production source sets it (plan 7 item 5a)."""
    by_role = {"security-auditor": {"model": "opus"}}
    cands = {"security-auditor": ["claude-code", ref(CLI_OPUS), ref(AGY_OPUS)]}
    agents = [CLI_OPUS, AGY_OPUS]
    scarce = resolve(by_role, agents, cands, weights={"claude-subscription": 0.3, "agy-claude-gpt": 1.0},
                     effective_ids=EFFECTIVE)
    assert scarce.roles["security-auditor"].ref == ref(AGY_OPUS)
    ample = resolve(by_role, agents, cands, weights={"claude-subscription": 1.0, "agy-claude-gpt": 0.7},
                    effective_ids=EFFECTIVE)
    assert ample.roles["security-auditor"].ref == "claude-code"
    equal = resolve(by_role, agents, cands, weights={"claude-subscription": 1.0, "agy-claude-gpt": 1.0},
                    effective_ids=EFFECTIVE)
    assert equal.roles["security-auditor"].ref == "claude-code"      # candidate order


def test_production_static_weights_place_same_model_roles_by_count_in_sorted_role_order():
    """Pinned behaviour (plan C6): claude 1.0 and agy 0.7 balance by role count, not by quota."""
    by_role = {r: {"model": "sonnet"} for r in ("tester", "doc-writer", "implementer")}
    cands = {r: ["claude-code", ref(AGY_SONNET)] for r in by_role}
    t = resolve(by_role, [AGY_SONNET], cands, effective_ids=EFFECTIVE)
    assert {r: ln.ref for r, ln in t.roles.items()} == {
        "doc-writer": "claude-code", "implementer": ref(AGY_SONNET), "tester": "claude-code"}


def test_a_checker_with_no_admissible_tier_one_candidate_is_blocked_never_substituted():
    by_role = {"verifier": {"model": "opus"}}
    t = resolve(by_role, [AGY_SONNET], {"verifier": [ref(AGY_SONNET)]}, effective_ids=EFFECTIVE)
    assert "verifier" in t.blocked and "verifier" not in t.roles
    t = resolve(by_role, [AGY_SONNET], {"verifier": [ref(AGY_SONNET), "claude-code"]}, effective_ids=EFFECTIVE)
    assert t.roles["verifier"].ref == "claude-code" and not t.blocked


# --- AC3.7: tier rule and pins ----------------------------------------------------------------------------

def test_tier_one_beats_tier_two_whatever_the_headroom():
    by_role = {"implementer": {"model": "sonnet"}}
    cands = {"implementer": ["claude-code", ref(AGY_SONNET46)]}
    t = resolve(by_role, [AGY_SONNET46], cands, effective_ids=EFFECTIVE,
                weights={"claude-subscription": 0.3, "agy-claude-gpt": 1.0})
    assert t.roles["implementer"].ref == "claude-code"


def test_tier_two_wins_for_a_writer_when_no_tier_one_candidate_is_admissible_and_names_both_ids():
    by_role = {"implementer": {"model": "sonnet"}}
    t = resolve(by_role, [AGY_SONNET46], {"implementer": [ref(AGY_SONNET46)]}, effective_ids=EFFECTIVE)
    assert t.roles["implementer"].ref == ref(AGY_SONNET46)
    why = " ".join(w.text for w in t.why if w.role == "implementer")
    assert "claude-sonnet-4-6" in why and "claude-sonnet-5-5" in why and "other version" in why


def test_a_checker_refuses_tier_two_and_a_drifted_claude_cli_checker_becomes_blocked():
    old_cli = L.LaneAgent("kit-lane-cli-opus46", "claude-cli", "anthropic/claude-opus-4-6", "claude-subscription")
    t = resolve({"verifier": {"model": "opus"}}, [old_cli], {"verifier": [ref(old_cli)]}, effective_ids=EFFECTIVE)
    assert "verifier" in t.blocked and "never takes a different version" in t.blocked["verifier"]
    legacy = resolve({"verifier": {"model": "opus"}}, [old_cli], {"verifier": [ref(old_cli)]})
    assert legacy.roles["verifier"].ref == ref(old_cli)          # effective_ids=None keeps phase 2 semantics


def test_a_listed_pin_ranks_first_for_that_role_only():
    by_role = {"implementer": {"model": "sonnet"}, "tester": {"model": "sonnet"}}
    cands = {"implementer": ["claude-code", ref(AGY_GEMINI)], "tester": ["claude-code", ref(AGY_GEMINI)]}
    t = resolve(by_role, [AGY_GEMINI], cands, pins={"implementer": [ref(AGY_GEMINI)]}, effective_ids=EFFECTIVE)
    assert t.roles["implementer"].ref == ref(AGY_GEMINI)
    assert t.roles["tester"].ref == "claude-code"                # not a pin for tester: refused (different model)
    why = [w.text for w in t.why if w.role == "tester"][0]
    assert "operator pin" in why


def test_a_pin_for_a_checker_or_for_a_non_candidate_is_a_lane_error():
    with pytest.raises(L.LaneError):
        resolve({"verifier": {"model": "opus"}}, [AGY_OPUS], {"verifier": [ref(AGY_OPUS)]},
                pins={"verifier": [ref(AGY_OPUS)]}, effective_ids=EFFECTIVE)
    with pytest.raises(L.LaneError):
        resolve({"implementer": {"model": "sonnet"}}, [AGY_GEMINI], {"implementer": ["claude-code"]},
                pins={"implementer": [ref(AGY_GEMINI)]}, effective_ids=EFFECTIVE)


def test_the_legacy_global_pin_flag_keeps_its_meaning_for_old_callers():
    by_role = {"implementer": {"model": "sonnet"}}
    t = resolve(by_role, [AGY_GEMINI], {"implementer": ["claude-code", ref(AGY_GEMINI)]},
                gates={**fk.OPEN_GATES, "operator_pin": True})
    assert t.roles["implementer"].ref == ref(AGY_GEMINI)


def test_second_for_a_checker_is_refused():
    for role in ("verifier", "security-auditor"):
        with pytest.raises(L.LaneError):
            resolve({role: {"model": "opus"}}, [AGY_OPUS], {role: ["claude-code"]}, {role: [ref(AGY_OPUS)]},
                    effective_ids=EFFECTIVE)


def test_a_gpt_oss_slug_is_refused_for_checkers_and_a_writer_needs_a_pin():
    assert rp.model_family("agy-cli-lane/gpt-oss-120b-medium") is None       # unknown family: fail closed
    with pytest.raises(L.LaneError):
        L._as_agent({"id": "kit-lane-agy-gpt", "backend": "agy-cli-lane",
                     "model": "agy-cli-lane/gpt-oss-120b-medium"}, "kit-lane-agy-gpt")


# --- determinism, digest, symmetry ------------------------------------------------------------------------

def test_the_digest_covers_effective_ids_pins_and_the_google_ack():
    by_role = {"implementer": {"model": "sonnet"}}
    base = dict(by_role=by_role, agents=[AGY_SONNET], candidates={"implementer": [ref(AGY_SONNET)]})
    d0 = resolve(**base).digest
    assert resolve(**base).digest == d0
    assert resolve(**base, effective_ids=EFFECTIVE).digest != d0
    assert resolve(**base, effective_ids={**EFFECTIVE, "sonnet": "claude-sonnet-5-6"}).digest != \
        resolve(**base, effective_ids=EFFECTIVE).digest
    other = copy.deepcopy(fk.OPEN_ACKS)
    other["google"]["text_sha256"] = "c" * 64
    assert resolve(**base, acks=other).digest != d0


def test_symmetry_the_same_fixture_on_either_label_gives_the_same_verdicts():
    by_role = {"implementer": {"model": "sonnet"}, "tester": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    out = []
    for sonnet, opus in ((CLI_SONNET, CLI_OPUS), (AGY_SONNET, AGY_OPUS)):
        t = resolve(by_role, [sonnet, opus],
                    {"implementer": [ref(sonnet)], "tester": [ref(sonnet)], "verifier": [ref(opus)]},
                    effective_ids=EFFECTIVE)
        out.append(({r: ln.ref.replace(sonnet.id, "S").replace(opus.id, "O") for r, ln in t.roles.items()},
                    t.blocked, {r: m for r, m in t.models.items()}))
    assert out[0][0] == out[1][0] and out[0][1] == out[1][1]
    assert {r: m.split("/")[-1] for r, m in out[0][2].items()} == {r: m.split("/")[-1] for r, m in out[1][2].items()}
