"""The usage-policy gate: direct-only and instructions-only surfaces, routability (AC-22)."""
from __future__ import annotations

import pytest

from ai_resources.scoring import db as scores_db
from ai_resources.scoring import gate as gate_mod
from ai_resources.scoring.compose import MODES, compose
from ai_resources.setup import compat
from scoring_fakes import GEMINI, SONNET, SOL, fixture_doc, make_input, row

DB = scores_db.ScoresDB(scores_db.load_shipped())


def test_agy_and_the_antigravity_engine_are_direct_only_with_the_faq_citation():
    for surface in ("agy", "engine:antigravity"):
        v = gate_mod.usage_gate(DB, surface)
        assert not v.allowed and v.kind == gate_mod.KIND_USAGE and v.grade == gate_mod.BLOCKED
        assert "direct-only" in v.reason and "usage-google.md:9,56-58" in v.reason and "ToS" in v.reason


def test_codex_cockpit_is_instructions_only():
    v = gate_mod.usage_gate(DB, "codex")
    assert not v.allowed and "instructions-only" in v.reason and "21 compat cells" in v.reason


def test_agy_pools_are_direct_only_whatever_the_surface():
    v = gate_mod.usage_gate(DB, "claude-code", "agy-gemini")
    assert not v.allowed and v.kind == gate_mod.KIND_USAGE


def test_claude_code_surface_and_dispatchable_pools_pass():
    assert gate_mod.usage_gate(DB, "claude-code", "claude-subscription").allowed


@pytest.mark.parametrize("allow", [False, True])
def test_no_flag_lifts_direct_only(allow):
    gate = gate_mod.make_gate(DB, allow_unverified=allow)
    assert not gate(SOL, "openclaw-codex-engine").allowed       # codex surface: instructions-only
    assert gate_mod.usage_gate(DB, "agy").allowed is False


def test_a_model_the_kit_does_not_route_is_rejected_with_the_fixed_reason():
    gate = gate_mod.make_gate(DB, allow_unverified=True)
    v = gate(GEMINI, "openrouter")
    assert not v.allowed and v.reason == "not routable by kit" and v.kind == gate_mod.KIND_NOT_ROUTABLE


def test_routable_dispatchable_pair_passes_the_composite_gate():
    v = gate_mod.make_gate(DB)(SONNET, "anthropic-direct")
    assert v.allowed and v.verified and v.grade == gate_mod.DECLARED


def test_route_that_needs_the_other_gateway_is_rejected():
    gate = gate_mod.make_gate(DB, mode_key=compat.MULTI_OPENROUTER, allow_unverified=True)
    assert not gate(SOL, "litellm").allowed


def agy_doc():
    """A fixture database in which an agy route exists and carries the best score of every role."""
    rows = [row(r, SONNET, 6.0) for r in ("planner", "implementer", "tester", "code-reviewer")]
    doc = fixture_doc(rows)
    doc["routes"]["agy-cli"] = {"provider": "google", "surface": "agy"}
    doc["models"][GEMINI]["route_ids"]["agy-cli"] = "gemini-3.8-flash"
    for r in ("planner", "implementer", "tester", "code-reviewer"):
        doc["rows"].append(row(r, GEMINI, 10.0, route="agy-cli"))
    return doc


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("allow", [False, True])
def test_agy_is_never_assigned_in_any_mode_with_or_without_the_flag(mode, allow):
    db = scores_db.ScoresDB(agy_doc())
    inp = make_input(db, mode=mode, allow_unverified=allow, enabled=("anthropic", "google"),
                     roles=("planner", "implementer", "tester", "code-reviewer"))
    comp = compose(inp)
    assert comp.assignments
    assert all(a.route != "agy-cli" for a in comp.assignments)
    reasons = [x for x in comp.rejected if x.route == "agy-cli"]
    assert reasons and all(x.kind == gate_mod.KIND_USAGE and "Antigravity" in x.reason for x in reasons)
