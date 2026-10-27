"""The kit-orchestration skill documents the routed dispatch procedure (ADR 0005, decision D6).

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

BLOCKED = "Block_reason: <role> step <id>: <cause from route / agy run>"


def test_section_exists():
    assert "## Dispatch: the matrix decides" in SKILL
    assert CORE.index("## Running a step") < CORE.index("## Backends") < CORE.index("## Parallel groups")


def test_core_points_at_the_reference_and_keeps_the_blocked_summary():
    assert "references/backends.md" in CORE
    assert "## Choosing a backend" not in CORE
    assert "no silent switch to `claude-code`" in CORE and "Return_to_step" in CORE
    assert "ai-resources route <role> --run $RUN" in CORE and "ai-resources agy run" in CORE


def test_core_size_ceiling_is_a_regression_guard_not_a_cost_claim():
    # Guards against the lanes text creeping back into the always-loaded skill; it measures bytes, not tokens or cost.
    assert len(CORE.encode("utf-8")) < 12_700


def test_blocked_text_is_exact_and_has_no_soft_fallback():
    assert BLOCKED in SKILL
    assert "Return_to_step: <id>" in SKILL
    assert "no silent switch to `claude-code`" in SKILL
    for gone in ("lane unavailable", "second opinion", "(second)", "candidates", "display-only"):
        assert gone not in SKILL, gone


def test_dispatch_table_names_all_three_runtimes_and_the_fallback_rule():
    assert "Task tool" in SKILL and "sessions_spawn agentId=kit-role-<role>" in SKILL
    assert "ai-resources agy run --role <role>" in SKILL
    assert "pool is exhausted" in SKILL and "does not respond" in SKILL
    assert "no-independent-checker" in SKILL


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


def test_parallel_groups_and_handoff_reference_mention_spawned_members():
    parallel = SKILL[SKILL.index("## Parallel groups"):SKILL.index("## Subagent return format")]
    assert "sessions_spawn" in parallel and "agy run" in parallel
    assert "agy run" in HANDOFF_REF


def test_lane_flow_is_gone_and_the_write_scope_claim_is_taught():
    assert "lanes prepare" not in SKILL and "lanes land" not in SKILL and "backend: lane" not in SKILL
    assert "write-scope claim" in SKILL
    who = SKILL[SKILL.index("## Who writes what"):SKILL.index("## Finishing")]
    assert "**Writers edit the checkout.**" in who and "One landing path" not in who
