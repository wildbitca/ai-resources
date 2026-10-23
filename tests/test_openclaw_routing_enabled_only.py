"""OpenClaw routing, fallbacks and role routes use only the enabled providers (ADR-0015 S7, acceptance 9)."""
from __future__ import annotations

import json

import pytest

from ai_resources.setup import cockpit_auth, profiles, state
from ai_resources.setup.cockpits import _openclaw_providers as P
from ai_resources.setup.cockpits import openclaw as oc

DOC: dict = {}


def _patch(engine_id, model, enabled, **kw):
    return oc.build_patch(oc.ENGINES[engine_id], model, DOC, "/kit/skills", "engram", fallbacks=True,
                          enabled_providers=enabled, **kw)


def test_no_restriction_keeps_the_fallbacks_exactly_as_before():
    base = oc._fallbacks_for("claude-cli/claude-sonnet-5-5", "claude-code")
    assert base and all(r.startswith("agy-cli/") for r in base)
    assert oc._fallbacks_for("claude-cli/claude-sonnet-5-5", "claude-code", enabled=None) == base


def test_anthropic_and_openai_only_drops_every_agy_fallback():
    assert oc._fallbacks_for("claude-cli/claude-sonnet-5-5", "claude-code", enabled={"anthropic", "openai"}) == []
    patch = _patch("claude-code", "claude-sonnet-5-5", {"anthropic", "openai"})
    assert "fallbacks" not in patch["agents"]["defaults"]["model"]


def test_google_enabled_keeps_the_google_fallbacks():
    got = oc._fallbacks_for("claude-cli/claude-sonnet-5-5", "claude-code", enabled={"anthropic", "google"})
    assert got and all(P.provider_of_ref(r) == "google" for r in got)


def test_the_antigravity_patch_is_restricted_too():
    kw = dict(worker_model="claude-sonnet-5-5", ak_path="/kit", agy_bin="/b/agy", claude_bin="/b/claude",
              python3_bin="/b/python3")
    free = _patch("antigravity", "gemini-3.8-flash-low", None, **kw)
    only = _patch("antigravity", "gemini-3.8-flash-low", {"google"}, **kw)
    assert free["agents"]["entries"]["claude"]["model"]["fallbacks"]
    assert only["agents"]["entries"]["claude"]["model"].get("fallbacks", []) == [
        r for r in free["agents"]["entries"]["claude"]["model"]["fallbacks"] if P.provider_of_ref(r) == "google"]
    for ref in only["agents"]["defaults"]["model"].get("fallbacks", []):
        assert P.provider_of_ref(ref) == "google"


def test_a_rerun_builds_the_identical_patch():
    a = json.dumps(_patch("claude-code", "claude-sonnet-5-5", {"anthropic", "openai"}), sort_keys=True)
    b = json.dumps(_patch("claude-code", "claude-sonnet-5-5", {"openai", "anthropic"}), sort_keys=True)
    assert a == b


def test_enabled_providers_reads_the_state_and_is_none_before_the_question():
    s = state.SetupState()
    assert oc.enabled_providers(s) is None
    cockpit_auth.set_providers(s, {"anthropic": "cli", "openai": "api_key"})
    assert oc.enabled_providers(s) == ["anthropic", "openai"]


def test_a_role_with_no_enabled_provider_falls_back_to_the_cheapest_enabled_one_and_is_listed():
    by_role = profiles.load_profile("balanced", "single-model")["by_role"]
    catalog = {"openai": ("gpt-5.5", "gpt-5.5-mini")}
    routed, fell = P.route_roles(by_role, ["openai"], catalog)
    assert set(routed.values()) == {"openai/gpt-5.5-mini"}
    assert {r for r, _w, _t in fell} == set(routed) and all(want == "anthropic" for _r, want, _t in fell)


def test_roles_keep_their_provider_when_it_is_enabled():
    by_role = profiles.load_profile("balanced", "single-model")["by_role"]
    routed, fell = P.route_roles(by_role, ["anthropic", "openai"], {"openai": ("gpt-5.5",)})
    assert fell == [] and routed["planner"] == "anthropic/sonnet" and routed["verifier"] == "anthropic/opus"
    assert P.route_roles(by_role, None)[1] == []


def test_a_gateway_profile_ref_keeps_its_provider_prefix():
    routed, fell = P.route_roles({"planner": {"model": "google/gemini-4-argon"}}, ["google"], {})
    assert routed == {"planner": "google/gemini-4-argon"} and fell == []


def test_nothing_to_fall_back_on_keeps_the_ref_and_lists_the_role():
    routed, fell = P.route_roles({"planner": {"model": "sonnet"}}, ["deepseek"], {})
    assert routed["planner"] == "anthropic/sonnet" and fell == [("planner", "anthropic", "anthropic/sonnet")]


def test_provider_of_ref_maps_runtimes_to_their_provider():
    assert [P.provider_of_ref(r) for r in ("claude-cli/x", "claude-kit/x", "codex/x", "agy-cli/x", "agy-cli-lane/x",
                                           "deepseek/x")] == ["anthropic", "anthropic", "openai", "google", "google",
                                                              "deepseek"]
    assert P.restrict_refs(["agy-cli/a", "anthropic/b"], ["anthropic"]) == ["anthropic/b"]
