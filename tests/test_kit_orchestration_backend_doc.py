"""The kit-orchestration skill documents the `openclaw-agent` dispatch procedure (ADR 0005).

These are text-contract checks: an LLM orchestrator follows the skill, so the pieces a
verifier relies on (the exact Blocked text, the post-step check, the retry rename) must not
silently disappear.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = (REPO / "skills" / "kit-orchestration" / "SKILL.md").read_text(encoding="utf-8")
# The backend procedure lives in an on-demand reference; the core keeps a pointer and a summary.
SKILL = CORE + "\n" + (REPO / "skills" / "kit-orchestration" / "references" / "backends.md").read_text(encoding="utf-8")
HANDOFF_REF = (REPO / "skills" / "kit-orchestration" / "references" / "handoff.md").read_text(encoding="utf-8")

BLOCKED = (
    "Block_reason: openclaw-agent step <id> (agentId=<x>) needs sessions_spawn in an OpenClaw-run "
    "orchestrator; missing: sessions_spawn tool in this session"
)


def test_section_exists():
    assert "## Choosing a backend" in SKILL
    assert CORE.index("## Running a step") < CORE.index("## Backends") < CORE.index("## Parallel groups")


def test_core_points_at_the_reference_and_keeps_the_blocked_summary():
    assert "references/backends.md" in CORE
    assert "## Choosing a backend" not in CORE
    assert "do not fall back to `claude-code`" in CORE and "Return_to_step" in CORE


def test_core_size_ceiling_is_a_regression_guard_not_a_cost_claim():
    # Guards against the lanes text creeping back into the always-loaded skill; it measures bytes, not tokens or cost.
    assert len(CORE.encode("utf-8")) < 12_700


def test_blocked_text_is_exact_and_has_no_fallback():
    assert BLOCKED in SKILL
    assert "Return_to_step: <id>" in SKILL
    assert "do not fall back to `claude-code`" in SKILL


def test_prompt_contract_and_waiting_rules():
    for needle in (
        "git rev-parse --show-toplevel",
        "$AGENT_KIT/agents/roles/<role>.md",
        "at most 60 s",
        "20 minutes",
        "<step-id>.attempt-<n>.md",
        "At most 3 attempts",
        "<step-id>.done",
    ):
        assert needle in SKILL, needle


def test_post_step_check_lists_four_checks():
    section = SKILL[SKILL.index("**Post-step check.**"):SKILL.index("**Retries.**")]
    for n in "1234":
        assert f"\n{n}. " in section
    assert "git -C <worktree> status --porcelain" in section


def test_example_team_states_models_as_agent_properties_and_rule_017():
    assert "agy-flash" in SKILL and "agy-pro" in SKILL
    assert "rules/017-multimodel-routing.mdc" in SKILL
    assert "external prerequisite" in SKILL


def test_parallel_groups_and_handoff_reference_mention_openclaw_members():
    parallel = SKILL[SKILL.index("## Parallel groups"):SKILL.index("## Subagent return format")]
    assert "openclaw-agent" in parallel
    assert "openclaw-agent" in HANDOFF_REF


def test_lane_flow_is_gone_and_the_write_scope_claim_is_taught():
    assert "lanes prepare" not in SKILL and "lanes land" not in SKILL and "backend: lane" not in SKILL
    assert "write-scope claim" in SKILL
    who = SKILL[SKILL.index("## Who writes what"):SKILL.index("## Finishing")]
    assert "**Writers edit the checkout.**" in who and "One landing path" not in who
