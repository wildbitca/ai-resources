"""The kit-orchestration skill documents the `openclaw-agent` dispatch procedure (ADR 0005).

These are text-contract checks: an LLM orchestrator follows the skill, so the pieces a
verifier relies on (the exact Blocked text, the post-step check, the retry rename) must not
silently disappear.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = (REPO / "skills" / "kit-orchestration" / "SKILL.md").read_text(encoding="utf-8")
# The backend and lane procedure lives in an on-demand reference; the core keeps a pointer and a summary.
SKILL = CORE + "\n" + (REPO / "skills" / "kit-orchestration" / "references" / "backends-and-lanes.md").read_text(encoding="utf-8")
HANDOFF_REF = (REPO / "skills" / "kit-orchestration" / "references" / "handoff.md").read_text(encoding="utf-8")

BLOCKED = (
    "Block_reason: openclaw-agent step <id> (agentId=<x>) needs sessions_spawn in an OpenClaw-run "
    "orchestrator; missing: sessions_spawn tool in this session"
)


def test_section_exists():
    assert "## Choosing a backend" in SKILL
    assert CORE.index("## Running a step") < CORE.index("## Backends and lanes") < CORE.index("## Parallel groups")


def test_core_points_at_the_reference_and_keeps_the_blocked_summary():
    assert "references/backends-and-lanes.md" in CORE
    assert "## Choosing a backend" not in CORE and "## Lane steps" not in CORE
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


def test_phase_3_points_the_lane_flow_at_both_lanes_and_names_the_lanes_example():
    assert "claude-cli and agy" in SKILL and "agy later" not in SKILL
    assert "workflows/feature-lanes.workflow.yaml" in SKILL


def test_lane_section_states_the_breaker_the_head_and_the_static_weight_placement():
    section = _lane_section()
    for needle in ("--spawn-error", "--expect-head", "never write it from the child's report", "role count",
                   "not by quota", "Do not use agy interactively during a lane run", "pins the file's hash"):
        assert needle in section, needle


def test_parallel_groups_and_handoff_reference_mention_openclaw_members():
    parallel = SKILL[SKILL.index("## Parallel groups"):SKILL.index("## Subagent return format")]
    assert "openclaw-agent" in parallel
    assert "openclaw-agent" in HANDOFF_REF


def _lane_section() -> str:
    ref = SKILL[SKILL.index("# Backends and lanes"):]
    return ref[ref.index("## Lane steps"):]


def test_lane_section_sits_between_backend_and_parallel_groups():
    ref = SKILL[SKILL.index("# Backends and lanes"):]
    assert ref.index("## Choosing a backend") < ref.index("## Lane steps")


def test_lane_precedence_table_block_and_soft_fallback():
    section = _lane_section()
    assert "**Precedence.**" in section
    for needle in ("| `claude-code` | any | any | claude-code |",
                   "lane unavailable here",
                   "| `lane` | no entry for the role | any | claude-code (the off switch) |",
                   "Block (configuration error)",
                   "Block (\"kit commands cannot run here\")",
                   "Blocked: true"):
        assert needle in section, needle


def test_lane_flow_expect_and_kit_commands():
    section = _lane_section()
    for needle in ("lanes prepare", "lanes integrate", "lanes verify", "lanes land", "openclaw worktrees create",
                   "--expect", "<nonce>:<snapshot_digest>:<ledger_head>", "never reads a head from the ledger file",
                   "--ff-only", "--subject <step-id>@<sha>", "Never reset anything", "sessions_spawn agentId=<lane id>"):
        assert needle in section, needle
    assert section.index("lanes prepare") < section.index("lanes integrate") < section.index("lanes verify") \
        < section.index("lanes land")


def test_lane_handoff_join_reports_mixed_group_and_cap():
    section = _lane_section()
    assert "never writes `{{handoff_file}}`" in section
    assert "step-scoped report" in section and ".agent-output/<role>/<step-id>*" in section
    assert "**Mixed-group rule.**" in section and "`claude-code` writer step together with a `lane` step" in section
    assert "max_remote_concurrency" in section and "Caps never apply to a checker" in section
    assert "(V2)" in section


def test_one_writer_paragraph_is_rewritten_to_the_adr_0007_invariant():
    who = SKILL[SKILL.index("## Who writes what"):SKILL.index("## Finishing")]
    assert "**One writer per run.**" not in who
    assert "**One landing path per run (ADR-0007).**" in who
    for needle in ("`lanes land`", "at most one integrating writer", "own per-attempt worktree",
                   "no agent merges or lands", "A run without lane writers is unchanged"):
        assert needle in who, needle
    parallel = SKILL[SKILL.index("## Parallel groups"):SKILL.index("## Subagent return format")]
    assert "backend: lane" in parallel and "never mixes a `claude-code` writer" in parallel
