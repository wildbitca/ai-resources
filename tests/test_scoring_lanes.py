"""`scoring.lanes.resolve_lanes` (ADR-0007): pure lane resolution against fakes."""
from __future__ import annotations

import copy
import random

import pytest

import scoring_fakes as fk
from ai_resources import roles_policy as rp
from ai_resources.scoring import lanes as L

CLI_SONNET = L.LaneAgent("kit-lane-cli-sonnet55", "claude-cli", "anthropic/claude-sonnet-5-5", "claude-subscription")
CLI_SONNET_B = L.LaneAgent("kit-lane-cli-sonnet55-b", "claude-cli", "anthropic/claude-sonnet-5-5", "claude-subscription")
CLI_OPUS = L.LaneAgent("kit-lane-cli-opus55", "claude-cli", "anthropic/claude-opus-5-5", "claude-subscription")
AGY_SONNET = L.LaneAgent("kit-lane-agy-sonnet55", "agy-cli-lane", "anthropic/claude-sonnet-5-5", "agy-claude-gpt")
AGY_OPUS = L.LaneAgent("kit-lane-agy-opus55", "agy-cli-lane", "anthropic/claude-opus-5-5", "agy-claude-gpt")
AGY_GEMINI = L.LaneAgent("kit-lane-agy-gemini", "agy-cli-lane", "google/gemini-3.1-pro", "agy-gemini")


def ref(agent):
    return f"openclaw-agent:{agent.id}"


def agents_of(*items):
    return {a.id: a for a in items}


def resolve(by_role=None, agents=(), candidates=None, second=None, **kw):
    kw.setdefault("usage_verdicts", fk.lane_usage_verdicts())
    kw.setdefault("acks", copy.deepcopy(fk.OPEN_ACKS))
    kw.setdefault("gates", dict(fk.OPEN_GATES))
    return L.resolve_lanes(by_role if by_role is not None else fk.lane_by_role(), agents_of(*agents),
                           candidates or {}, second or {}, **kw)


# (a) -----------------------------------------------------------------------------------------------

def test_no_agents_everything_is_claude_code():
    by_role = fk.lane_by_role()
    table = resolve(by_role)
    assert set(table.roles) == set(by_role)
    assert all(lane.ref == "claude-code" for lane in table.roles.values())
    assert table.second == {} and table.agents == {} and table.blocked == {}
    assert all(table.models[r] == c["model"] for r, c in by_role.items())


def test_unknown_role_resolves_to_claude_code_with_a_reason():
    table = resolve({"made-up": {"model": "sonnet"}}, [CLI_SONNET], {"made-up": [ref(CLI_SONNET)]})
    assert table.roles["made-up"].ref == "claude-code"
    assert any("no write contract declared" in w.text for w in table.why if w.role == "made-up")


# (b) -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", [r for r in fk.ALL_ROLES if r not in rp.CHECKER_ROLES and r != "explore"
                                  and r not in ("planner", "software-architect")])
def test_any_eligible_role_resolves_to_an_admitted_claude_cli_agent(role):
    table = resolve({role: fk.lane_by_role()[role]}, [CLI_SONNET], {role: [ref(CLI_SONNET)]})
    assert table.roles[role].ref == ref(CLI_SONNET)
    assert table.roles[role].agentId == CLI_SONNET.id


def test_implementer_may_take_any_lane_including_agy():
    table = resolve({"implementer": {"model": "sonnet"}}, [AGY_SONNET], {"implementer": [ref(AGY_SONNET)]})
    assert table.roles["implementer"].ref == ref(AGY_SONNET)


# (c) / (d) ----------------------------------------------------------------------------------------

def test_same_pool_tie_is_broken_by_candidate_order_and_says_no_quota_gain():
    by_role = {"implementer": {"model": "sonnet"}}
    first = resolve(by_role, [CLI_SONNET], {"implementer": ["claude-code", ref(CLI_SONNET)]})
    assert first.roles["implementer"].ref == "claude-code"
    why = [w for w in first.why if w.role == "implementer"][0]
    assert "same pool, no quota gain" in why.text
    second = resolve(by_role, [CLI_SONNET], {"implementer": [ref(CLI_SONNET), "claude-code"]})
    assert second.roles["implementer"].ref == ref(CLI_SONNET)


def test_ties_between_agents_use_candidate_order():
    by_role = {"tester": {"model": "sonnet"}}
    t1 = resolve(by_role, [CLI_SONNET, CLI_SONNET_B], {"tester": [ref(CLI_SONNET_B), ref(CLI_SONNET)]})
    assert t1.roles["tester"].agentId == CLI_SONNET_B.id
    t2 = resolve(by_role, [CLI_SONNET, CLI_SONNET_B], {"tester": [ref(CLI_SONNET), ref(CLI_SONNET_B)]})
    assert t2.roles["tester"].agentId == CLI_SONNET.id


def test_pressure_prefers_the_less_loaded_pool_and_equal_pressure_follows_order():
    by_role = {"security-auditor": {"model": "opus"}}
    cands = {"security-auditor": ["claude-code", ref(AGY_OPUS)]}
    scarce = resolve(by_role, [AGY_OPUS], cands, weights={"claude-subscription": 0.3})
    assert scarce.roles["security-auditor"].ref == ref(AGY_OPUS)
    ample = resolve(by_role, [AGY_OPUS], cands, weights={"claude-subscription": 1.0})
    assert ample.roles["security-auditor"].ref == "claude-code"
    equal = resolve(by_role, [AGY_OPUS], cands, weights={"claude-subscription": 0.7})
    assert equal.roles["security-auditor"].ref == "claude-code"
    swapped = resolve(by_role, [AGY_OPUS], {"security-auditor": [ref(AGY_OPUS), "claude-code"]},
                      weights={"claude-subscription": 0.7})
    assert swapped.roles["security-auditor"].ref == ref(AGY_OPUS)


def test_load_balances_roles_across_pools():
    by_role = {"tester": {"model": "sonnet"}, "doc-writer": {"model": "sonnet"}}
    cands = {r: ["claude-code", ref(AGY_SONNET)] for r in by_role}
    table = resolve(by_role, [AGY_SONNET], cands, weights={"claude-subscription": 0.7, "agy-claude-gpt": 0.7})
    assert {table.roles["doc-writer"].ref, table.roles["tester"].ref} == {"claude-code", ref(AGY_SONNET)}


# (e) / (f) ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["flag", "adr", "ack"])
def test_agy_is_rejected_without_flag_adr_or_google_ack(missing):
    gates, acks = dict(fk.OPEN_GATES), copy.deepcopy(fk.OPEN_ACKS)
    if missing == "flag":
        gates["antigravity_flag"] = False
    elif missing == "adr":
        gates["adr_0007_agy_accepted"] = False
    else:
        acks["google"]["roles"] = [r for r in acks["google"]["roles"] if r != "tester"]
    table = resolve({"tester": {"model": "sonnet"}}, [AGY_SONNET], {"tester": [ref(AGY_SONNET)]},
                    gates=gates, acks=acks)
    assert table.roles["tester"].ref == "claude-code"
    text = " ".join(w.text for w in table.why if w.role == "tester")
    assert "agy-claude-gpt" in text and "ADR-0007" in text and "direct-only" in text


def test_agy_gate_is_keyed_on_the_pool_and_works_for_gemini_too():
    gates = dict(fk.OPEN_GATES, operator_pin=True)
    table = resolve({"code-reviewer": {"model": "sonnet"}}, [AGY_GEMINI], {"code-reviewer": [ref(AGY_GEMINI)]},
                    gates=gates)
    assert table.roles["code-reviewer"].ref == ref(AGY_GEMINI)
    assert table.agents[AGY_GEMINI.id].pool == "agy-gemini"
    closed = resolve({"code-reviewer": {"model": "sonnet"}}, [AGY_GEMINI], {"code-reviewer": [ref(AGY_GEMINI)]},
                     gates=dict(gates, antigravity_flag=False))
    assert closed.roles["code-reviewer"].ref == "claude-code"
    assert any("agy-gemini" in w.text for w in closed.why)


def test_claude_cli_is_rejected_without_an_anthropic_ack_listing_the_role():
    acks = copy.deepcopy(fk.OPEN_ACKS)
    acks["anthropic"]["roles"] = ["tester"]
    table = resolve({"implementer": {"model": "sonnet"}, "tester": {"model": "sonnet"}}, [CLI_SONNET],
                    {"implementer": [ref(CLI_SONNET)], "tester": [ref(CLI_SONNET)]}, acks=acks)
    assert table.roles["implementer"].ref == "claude-code"
    assert table.roles["tester"].ref == ref(CLI_SONNET)
    assert any("Anthropic acknowledgement" in w.text for w in table.why if w.role == "implementer")
    none = resolve({"tester": {"model": "sonnet"}}, [CLI_SONNET], {"tester": [ref(CLI_SONNET)]}, acks={})
    assert none.roles["tester"].ref == "claude-code"


# (g) checker independence -------------------------------------------------------------------------

def test_checker_lane_agent_in_the_writer_family_conflicts_through_model_family():
    gates = dict(fk.OPEN_GATES, operator_pin=True)
    with pytest.raises(L.LaneConflict) as exc:
        resolve({"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}}, [CLI_SONNET],
                {"verifier": [ref(CLI_SONNET)]}, gates=gates)
    assert set(exc.value.roles) == {"verifier", "implementer"}
    assert "sonnet" in str(exc.value)


def test_checker_with_an_unknown_family_conflicts_fail_closed():
    with pytest.raises(L.LaneConflict) as exc:
        resolve({"implementer": {"model": "sonnet"}, "verifier": {"model": "inherit"}}, [], {"verifier": ["claude-code"]})
    assert "unknown" in str(exc.value)


def test_why_row_for_a_pinned_agent_against_an_alias_role_notes_drift():
    table = resolve({"implementer": {"model": "sonnet"}}, [CLI_SONNET], {"implementer": [ref(CLI_SONNET)]})
    why = " ".join(w.text for w in table.why)
    assert f"lane agent pinned {CLI_SONNET.model}; alias may resolve newer" in why


def test_multi_model_union_conflict_names_the_edge():
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"},
               "planner": {"model": "sonnet", "fallbacks": ["opus"]}}
    with pytest.raises(L.LaneConflict) as exc:
        resolve(by_role, [], {"verifier": ["claude-code"]}, mode="multi-model")
    assert any("sonnet -> opus" in e and "planner" in e for e in exc.value.edges)
    # the same table is fine in single-model mode: fallbacks are inert there
    ok = resolve(by_role, [], {"verifier": ["claude-code"]}, mode="single-model")
    assert ok.roles["verifier"].ref == "claude-code"


def test_multi_model_default_fallbacks_are_part_of_the_union():
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    with pytest.raises(L.LaneConflict) as exc:
        resolve(by_role, [], {"verifier": ["claude-code"]}, mode="multi-model", default_fallbacks=["opus"])
    assert any("defaults.fallbacks" in e for e in exc.value.edges)


def test_same_backend_different_model_passes_on_agy():
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    table = resolve(by_role, [AGY_SONNET, AGY_OPUS], {"implementer": [ref(AGY_SONNET)], "verifier": [ref(AGY_OPUS)]})
    assert table.roles["implementer"].ref == ref(AGY_SONNET)
    assert table.roles["verifier"].ref == ref(AGY_OPUS)


def test_verifier_on_a_sonnet_agent_on_any_lane_conflicts():
    gates = dict(fk.OPEN_GATES, operator_pin=True)
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    with pytest.raises(L.LaneConflict):
        resolve(by_role, [AGY_SONNET], {"verifier": [ref(AGY_SONNET)]}, gates=gates)


def test_security_auditor_conflicts_with_a_scope_writer_not_just_the_implementer():
    with pytest.raises(L.LaneConflict) as exc:
        resolve({"tester": {"model": "opus"}, "security-auditor": {"model": "opus"}}, [], {"tester": ["claude-code"]})
    assert set(exc.value.roles) == {"security-auditor", "tester"}


# (h) / (i) / (j) -----------------------------------------------------------------------------------

def test_a_lane_never_moves_the_model():
    by_role = fk.lane_by_role()
    before = copy.deepcopy(by_role)
    cands = {r: [ref(CLI_SONNET if by_role[r]["model"] != "opus" else CLI_OPUS), "claude-code"] for r in by_role
             if by_role[r]["model"] in ("sonnet", "opus")}
    table = resolve(by_role, [CLI_SONNET, CLI_OPUS], cands)
    assert by_role == before
    assert any(ln.agentId for ln in table.roles.values()), "no role landed on a lane agent"
    for role, cfg in by_role.items():
        assert rp.model_family(table.models[role]) == rp.model_family(cfg["model"])
    assert set(table.to_lanes()) == {"agents", "roles", "second"}


def test_a_pinned_other_model_needs_the_pin_flag_and_is_labelled():
    cands = {"code-reviewer": [ref(AGY_GEMINI)]}
    refused = resolve({"code-reviewer": {"model": "sonnet"}}, [AGY_GEMINI], cands)
    assert refused.roles["code-reviewer"].ref == "claude-code"
    assert any("pin" in w.text for w in refused.why)
    allowed = resolve({"code-reviewer": {"model": "sonnet"}}, [AGY_GEMINI], cands,
                      gates=dict(fk.OPEN_GATES, operator_pin=True))
    assert allowed.roles["code-reviewer"].ref == ref(AGY_GEMINI)
    assert any("operator pin, not scored" in w.text for w in allowed.why)


def test_no_invented_ids():
    with pytest.raises(L.LaneError):
        resolve({"tester": {"model": "sonnet"}}, [CLI_SONNET], {"tester": ["openclaw-agent:kit-lane-ghost"]})
    with pytest.raises(L.LaneError):
        resolve({"tester": {"model": "sonnet"}}, [CLI_SONNET], {"tester": ["agy-flash"]})
    table = resolve({"tester": {"model": "sonnet"}}, [CLI_SONNET], {"tester": [ref(CLI_SONNET)]})
    assert {ln.agentId for ln in table.roles.values() if ln.agentId} <= {CLI_SONNET.id}


def test_resolution_is_deterministic_and_order_independent():
    by_role = fk.lane_by_role()
    cands = {r: ["claude-code", ref(CLI_SONNET)] for r in by_role if by_role[r]["model"] == "sonnet"}
    cands.update({r: ["claude-code", ref(AGY_OPUS)] for r in ("verifier", "security-auditor")})
    base = resolve(by_role, [CLI_SONNET, AGY_OPUS], cands)
    items = list(by_role.items())
    for seed in range(5):
        random.Random(seed).shuffle(items)
        shuffled = resolve(dict(items), [AGY_OPUS, CLI_SONNET],
                           dict(random.Random(seed).sample(list(cands.items()), len(cands))))
        assert shuffled.roles == base.roles and shuffled.digest == base.digest and shuffled.why == base.why


def test_digest_changes_with_every_input():
    by_role = {"tester": {"model": "sonnet"}, "verifier": {"model": "opus"}}
    cands = {"tester": [ref(CLI_SONNET)]}
    base = resolve(by_role, [CLI_SONNET], cands)
    assert base.digest == resolve(by_role, [CLI_SONNET], cands).digest
    digests = {
        "agent model": resolve(by_role, [L.LaneAgent(CLI_SONNET.id, "claude-cli", "anthropic/claude-sonnet-5-6")], cands),
        "stale": resolve(by_role, [L.LaneAgent(CLI_SONNET.id, "claude-cli", CLI_SONNET.model, stale=True)], cands),
        "candidates": resolve(by_role, [CLI_SONNET], {"tester": ["claude-code", ref(CLI_SONNET)]}),
        "second": resolve(by_role, [CLI_SONNET], cands, second={"code-reviewer": ["claude-code"]}),
        "caps": resolve(by_role, [CLI_SONNET], cands, pool_caps={"claude-subscription": 3}),
        "mode": resolve(by_role, [CLI_SONNET], cands, mode="multi-model"),
        "weights": resolve(by_role, [CLI_SONNET], cands, weights={"claude-subscription": 0.3}),
        "by_role": resolve({**by_role, "tester": {"model": "sonnet", "fallbacks": ["haiku"]}}, [CLI_SONNET], cands),
        "gates": resolve(by_role, [CLI_SONNET], cands, gates={"antigravity_flag": False}),
    }
    acks = copy.deepcopy(fk.OPEN_ACKS)
    acks["anthropic"]["text_sha256"] = "c" * 64
    digests["ack hash"] = resolve(by_role, [CLI_SONNET], cands, acks=acks)
    acks2 = copy.deepcopy(fk.OPEN_ACKS)
    acks2["anthropic"]["roles"] = ["tester"]
    digests["ack roles"] = resolve(by_role, [CLI_SONNET], cands, acks=acks2)
    for name, table in digests.items():
        assert table.digest != base.digest, name
    assert len({t.digest for t in digests.values()}) == len(digests)


# (k) -----------------------------------------------------------------------------------------------

def test_unknown_backend_is_a_lane_error():
    bad = L.LaneAgent("kit-lane-x", "mystery-cli", "anthropic/claude-sonnet-5-5", "")
    with pytest.raises(L.LaneError, match="unknown backend"):
        resolve({"tester": {"model": "sonnet"}}, [bad], {"tester": [ref(bad)]})


def test_pool_mismatch_and_unknown_mode_are_lane_errors():
    bad = L.LaneAgent("kit-lane-x", "claude-cli", "anthropic/claude-sonnet-5-5", "agy-claude-gpt")
    with pytest.raises(L.LaneError, match="pool"):
        resolve({}, [bad], {})
    with pytest.raises(L.LaneError, match="mode"):
        resolve({}, [], {}, mode="hybrid")


def test_second_member_for_a_writer_only_role_is_an_error():
    with pytest.raises(L.LaneError, match="report-contract"):
        resolve({"tester": {"model": "sonnet"}}, [CLI_SONNET], {}, second={"tester": [ref(CLI_SONNET)]})


def test_stale_agent_is_refused():
    stale = L.LaneAgent(CLI_SONNET.id, "claude-cli", CLI_SONNET.model, stale=True)
    table = resolve({"tester": {"model": "sonnet"}}, [stale], {"tester": [ref(stale)]})
    assert table.roles["tester"].ref == "claude-code"
    assert any("stale" in w.text for w in table.why)


# (l) / (m) -----------------------------------------------------------------------------------------

def test_pool_caps_limit_writers_and_never_a_checker():
    by_role = {"tester": {"model": "sonnet"}, "doc-writer": {"model": "sonnet"}, "implementer": {"model": "sonnet"},
               "verifier": {"model": "opus"}}
    only_cli = {r: [ref(CLI_SONNET)] for r in ("tester", "doc-writer", "implementer")}
    only_cli["verifier"] = [ref(CLI_OPUS)]
    table = resolve(by_role, [CLI_SONNET, CLI_OPUS], only_cli, pool_caps={"claude-subscription": 1})
    on_agent = [r for r in ("tester", "doc-writer", "implementer") if table.roles[r].agentId]
    assert len(on_agent) == 1
    assert any("cap reached" in w.text for w in table.why)
    assert table.roles["verifier"].agentId == CLI_OPUS.id          # never capped


def test_checker_with_no_admissible_candidate_is_blocked_with_the_reason():
    acks = copy.deepcopy(fk.OPEN_ACKS)
    acks["google"]["roles"] = []
    table = resolve({"verifier": {"model": "opus"}}, [AGY_OPUS], {"verifier": [ref(AGY_OPUS)]}, acks=acks,
                    pool_caps={"agy-claude-gpt": 0})
    assert "verifier" not in table.roles
    assert "verifier" in table.blocked and "Google acknowledgement" in table.blocked["verifier"]
    assert any(w.lane == "blocked" for w in table.why)


def test_checker_second_candidate_on_the_same_model_is_the_availability_fallback():
    by_role = {"security-auditor": {"model": "opus"}}
    cands = {"security-auditor": ["claude-code", ref(AGY_OPUS)]}
    scarce = resolve(by_role, [AGY_OPUS], cands, weights={"claude-subscription": 0.3},
                     pool_caps={"claude-subscription": 0})
    assert scarce.roles["security-auditor"].ref == ref(AGY_OPUS)
    assert scarce.models["security-auditor"] == AGY_OPUS.model
    assert rp.model_family(scarce.models["security-auditor"]) == "opus"
    unlisted = copy.deepcopy(fk.OPEN_ACKS)
    unlisted["google"]["roles"] = []
    closed = resolve(by_role, [AGY_OPUS], cands, weights={"claude-subscription": 0.3}, acks=unlisted)
    assert closed.roles["security-auditor"].ref == "claude-code"


# second members ------------------------------------------------------------------------------------

def test_second_member_resolves_and_equal_to_primary_adds_nothing():
    by_role = {"code-reviewer": {"model": "sonnet"}}
    table = resolve(by_role, [AGY_SONNET], {"code-reviewer": ["claude-code"]},
                    second={"code-reviewer": [ref(AGY_SONNET)]})
    assert table.roles["code-reviewer"].ref == "claude-code"
    assert table.second["code-reviewer"].ref == ref(AGY_SONNET)
    same = resolve(by_role, [AGY_SONNET], {"code-reviewer": ["claude-code"]}, second={"code-reviewer": ["claude-code"]})
    assert "code-reviewer" not in same.second
    assert any("adds nothing" in w.text for w in same.why)


def test_fallback_union_matches_the_litellm_renderer():
    """The pure union in lanes.py equals the one `setup/litellm.py` builds (same keying and ordering)."""
    from ai_resources.setup import litellm
    by_role = {"a": {"model": "anthropic/claude-sonnet-5.5", "fallbacks": ["anthropic/claude-haiku-5.5"]},
               "b": {"model": "anthropic/claude-sonnet-5.5", "fallbacks": ["anthropic/claude-opus-5.5",
                                                                          "anthropic/claude-haiku-5.5"]},
               "c": {"model": "anthropic/claude-opus-5.5"}}
    ours = {k: [fb for fb, _ in v] for k, v in L.fallback_union(by_role).items()}
    rendered = litellm._render_litellm_yaml({"by_role": by_role, "classes": {}, "defaults": {}},
                                            {"anthropic": {"enabled": True}}, "LITELLM_MASTER_KEY")
    import yaml
    doc = yaml.safe_load(rendered)
    theirs = {}
    for entry in doc["router_settings"]["fallbacks"]:
        theirs.update(entry)
    assert ours == theirs
