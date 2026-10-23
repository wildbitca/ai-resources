"""ADR-0016 S4: the live OpenClaw reader over a parsed openclaw.json (read-only, no gateway call)."""
from __future__ import annotations

import json
from pathlib import Path

from ai_resources.setup import live_openclaw as lo

FIX = Path(__file__).parent / "fixtures" / "openclaw_live"


def doc():
    return json.loads((FIX / "all_anthropic.json").read_text(encoding="utf-8"))


def test_every_agent_reports_its_primary_and_runtime():
    live = lo.live_agents(doc())
    assert live["main"] == ("anthropic/claude-sonnet-5-5", "openclaw")
    assert live["security"][0] == "anthropic/claude-sonnet-5-5"
    assert live[""] == ("anthropic/claude-sonnet-5-5", "openclaw")


def test_an_agent_without_a_model_runs_the_defaults_primary():
    d = {"agents": {"defaults": {"model": {"primary": "anthropic/x"}}, "entries": {"a": {"workspace": "/w"}}}}
    assert lo.live_agents(d)["a"] == ("anthropic/x", "")


def test_an_agent_model_may_be_a_bare_string_and_the_list_form_is_tolerated():
    d = {"agents": {"list": [{"id": "z", "model": "agy-cli/gemini-3.8-flash-low"}]}}
    assert lo.live_agents(d) == {"z": ("agy-cli/gemini-3.8-flash-low", "")}


def test_an_unconfigured_install_reports_nothing():
    assert lo.live_agents({}) == {} and not lo.has_live_agents({}) and lo.live_default({}) == ("", "")


def test_runtime_falls_back_to_the_provider_wildcard():
    d = {"agents": {"defaults": {"models": {"anthropic/*": {"agentRuntime": {"id": "claude-cli"}}}}}}
    assert lo.runtime_of(d, "anthropic/claude-x") == "claude-cli"


def test_helpers_classify_refs():
    assert lo.is_anthropic("anthropic/claude-sonnet-5-5") and lo.is_anthropic("claude-opus")
    assert lo.is_agy("agy-cli/gemini-4-argon-high") and not lo.is_agy("anthropic/x")
    assert lo.live_fallbacks(doc())[0] == "anthropic/claude-opus-5-5"
