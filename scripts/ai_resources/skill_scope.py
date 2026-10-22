"""Which kit skills Claude Code lists natively.

Every skill linked into ~/.claude/skills adds its name and description to the prefix of every
session (the 2026-10-22 cost analysis measured ~15k tokens for 161 skills, of which 20 were used
in 30 days). The lean default links only the skills below; everything else stays on disk in the
kit and is loaded by path (workflow steps read `$AGENT_KIT/skills/<id>/SKILL.md`) or preloaded
through a role's `skills:` frontmatter (`claude.ROLE_SKILLS`).

Opt out or widen with AI_RESOURCES_SKILLS:
  unset / "lean"      the include list below
  "all"               every kit skill (the pre-2.4 behaviour)
  "flutter,terraform" the include list plus every skill whose id starts with those prefixes
Only the Claude Code cockpit is scoped; other cockpits keep linking every skill.
"""
from __future__ import annotations

import os

# Domain prefixes linked natively (a trailing "-" is part of the prefix).
INCLUDE_PREFIXES: tuple[str, ...] = ("workflow-",)

# Exact ids linked natively: user commands, orchestration, and the few skills a session wants unprompted.
INCLUDE_IDS: tuple[str, ...] = (
    "kit-orchestration",
    "delegate",
    "setup-project",
    "self-update",
    "knowledge-audit",
    "using-git-worktrees",
    "finishing-a-development-branch",
    "common-debugging",
    "common-feedback-reporter",
    "openclaw-operations",
)

# Skills a workflow step names that are neither linked nor preloaded for that step's role: the step reads
# `$AGENT_KIT/skills/<id>/SKILL.md` by path (kit-orchestration says so). validate_kit.py fails on a step skill that is in
# none of INCLUDE_*, ROLE_SKILLS (for the step's role) or this list, so a new step skill must be placed on purpose.
LOADABLE_BY_PATH: tuple[str, ...] = (
    "common-architecture-diagramming",
    "common-best-practices",
    "common-code-review",
    "common-context-optimization",
    "common-error-handling",
    "common-product-requirements",
    "common-security-standards",
    "common-system-design",
)

ENV_VAR = "AI_RESOURCES_SKILLS"


def include_list(skill_ids, env: dict | None = None) -> set[str] | None:
    """The skill ids to link, or None for "all" (no filtering)."""
    raw = ((env if env is not None else os.environ).get(ENV_VAR) or "").strip().lower()
    if raw == "all":
        return None
    extra = tuple(p if p.endswith("-") else p + "-" for p in (x.strip() for x in raw.split(",")) if p and p != "lean")
    prefixes = INCLUDE_PREFIXES + extra
    ids = set(INCLUDE_IDS)
    ids |= {sk for skills in ROLE_SKILLS.values() for sk in skills}   # preloads must resolve: Claude Code skips unlinked ones
    return {s for s in skill_ids if s in ids or s.startswith(prefixes)}


# Skills preloaded into a role's subagent (`skills:` frontmatter). Derived from the workflow steps: the skills EVERY
# step of that role names (the intersection), so a preload never carries a skill a step of that role would not read.
# Every id here is linked too (include_list) because Claude Code resolves frontmatter skills through the linked set and
# skips a missing one; the common-* ids carry `disable-model-invocation: true`, so they stay out of the main listing
# (the 2.1.308 preload path checks existence, type and policy, not that flag). A role absent here renders exactly as before. Manual overrides edit this table; validate_kit.py checks it
# still covers the workflows.
ROLE_SKILLS: dict[str, tuple[str, ...]] = {
    "explore": ("common-context-optimization",),
    "implementer": ("common-best-practices",),
    "planner": ("common-system-design",),
    "security-auditor": ("common-security-audit", "common-security-standards"),
    "tester": ("common-tdd",),
    "verifier": ("common-protocol-enforcement",),
    "doc-writer": ("knowledge-audit",),
}

# MCP servers a role's subagent may use (`mcpServers:` frontmatter). Empty by default: a role absent here keeps
# inheriting the session's servers, as before. A role listed here sees only these (engram is how roles hand state over).
ROLE_MCP_SERVERS: dict[str, tuple[str, ...]] = {}
