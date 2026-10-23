"""The effort resolver (ADR-0015 S3): one profile level, translated per cockpit, never raising."""
from __future__ import annotations

import pytest

from ai_resources.setup import effort, profiles, state

BALANCED = {
    "explore": "low", "doc-writer": "low", "tester": "low", "package-upgrade": "low",
    "implementer": "medium", "code-reviewer": "medium", "planner": "high", "software-architect": "high",
    "verifier": "high", "security-auditor": "high",
}


@pytest.mark.parametrize("name", ["balanced", "performance"])
def test_every_known_role_has_an_allowed_level_in_the_profiles_that_declare_a_table(name):
    doc = profiles.load_profile(name, "single-model")
    for role in profiles.KNOWN_ROLES:
        assert doc["role_effort"].get(role) in effort.LEVELS, role


def test_the_balanced_table_follows_the_plan():
    doc = profiles.load_profile("balanced", "single-model")
    for role, level in BALANCED.items():
        assert doc["role_effort"][role] == level, role
    perf = profiles.load_profile("performance", "single-model")
    assert perf["role_effort"]["tester"] == "medium" and perf["role_effort"]["package-upgrade"] == "medium"


def test_resolver_defaults_for_unknown_role_level_or_profile():
    assert effort.role_effort({"role_effort": {"planner": "high"}}, "planner") == "high"
    assert effort.role_effort({"role_effort": {"planner": "high"}}, "nobody") == effort.DEFAULT
    assert effort.role_effort({"role_effort": {"planner": "turbo"}}, "planner") == effort.DEFAULT
    assert effort.role_effort(None, "planner") == effort.DEFAULT
    assert effort.role_effort({"role_effort": "oops"}, "planner") == effort.DEFAULT


def test_cockpit_translation():
    assert [effort.for_cockpit(x, "claude") for x in effort.LEVELS] == ["low", "medium", "high"]
    assert [effort.for_cockpit(x, "openclaw") for x in effort.LEVELS] == ["low", "medium", "high"]
    assert effort.for_cockpit("bogus", "openclaw") == effort.DEFAULT
    with pytest.raises(ValueError):
        effort.for_cockpit("low", "cursor")


def test_openclaw_levels_are_accepted_by_the_installed_schema():
    # agents.entries.<id>.thinkingDefault enum on OpenClaw 2026.10.5 (verified fact, ADR-0015)
    enum = {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max", "ultra"}
    assert {effort.for_cockpit(x, "openclaw") for x in effort.LEVELS} <= enum
    assert effort.OPENCLAW_CHAT_LEVEL in enum and effort.NOTIFICATION_LEVEL in enum


def test_claude_frontmatter_follows_the_capability_flag(monkeypatch):
    prof = {"role_effort": {"planner": "high"}}
    assert effort.claude_frontmatter_value(prof, "planner") == "high"
    monkeypatch.setattr(effort, "CLAUDE_EFFORT_FRONTMATTER", False)
    assert effort.claude_frontmatter_value(prof, "planner") is None


def test_table_for_state_reads_the_saved_profile_and_never_raises():
    s = state.SetupState()
    s.profile.name = "balanced"
    assert effort.table_for_state(s)["planner"] == "high"
    s.profile.name = "no-such-profile"
    assert effort.table_for_state(s) == {}
    assert effort.table_for_state(object()) != None  # noqa: E711 - a state-less object still resolves (default profile)
