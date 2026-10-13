"""The kit-orchestration skill documents the `openclaw-agent` dispatch procedure (ADR 0005).

These are text-contract checks: an LLM orchestrator follows the skill, so the pieces a
verifier relies on (the exact Blocked text, the post-step check, the retry rename) must not
silently disappear.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = (REPO / "skills" / "kit-orchestration" / "SKILL.md").read_text(encoding="utf-8")
HANDOFF_REF = (REPO / "skills" / "kit-orchestration" / "references" / "handoff.md").read_text(encoding="utf-8")

BLOCKED = (
    "Block_reason: openclaw-agent step <id> (agentId=<x>) needs sessions_spawn in an OpenClaw-run "
    "orchestrator; missing: sessions_spawn tool in this session"
)


def test_section_exists():
    assert "## Choosing a backend" in SKILL
    assert SKILL.index("## Running a step") < SKILL.index("## Choosing a backend") < SKILL.index("## Parallel groups")


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
