"""Static prompt estimate for the roles that run on Haiku (ADR-0014 D1).

Claude Haiku 5.5 is priced per prompt size: a call whose prompt exceeds 100k tokens costs five times more (see
`audit.LONG_CONTEXT`). A Haiku role must therefore start far below that threshold. `estimate_tokens` adds up what a role
starts with, at 4 characters per token:

- the fixed Claude Code prefix (system prompt and tool schemas; 39k measured on 2026-10-22 with `claude -p hi`),
- the role file plus its largest persona overlay,
- the skills preloaded for the role (`skill_scope.ROLE_SKILLS`), bodies in full,
- the native skill listing (the frontmatter of every skill linked by `skill_scope.include_list`).

The static part is multiplied by SAFETY_MARGIN because 4 chars/token is approximate. The runtime counterpart is the
audit's share of Haiku calls above 100k (`audit_snapshot.summarize`). Standard library only: `validate_kit.py` imports it
in CI where the package dependencies are absent.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import skill_scope

HAIKU_PROMPT_THRESHOLD_TOKENS = 100_000
CHARS_PER_TOKEN = 4
SAFETY_MARGIN = 1.25               # the 4 chars/token rule can undercount (code, non-English text, JSON schemas)
FIXED_PREFIX_TOKENS = 39_000       # `claude -p hi` prompt on the 2026-10-22 baseline

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def haiku_roles(root: Path) -> set[str]:
    """Roles that any shipped profile (including profiles/gateway) assigns to a Haiku model."""
    roles: set[str] = set()
    for path in sorted((root / "profiles").rglob("*.yaml")):
        text = _read(path)
        in_by_role = False
        for line in text.splitlines():
            if re.match(r"^by_role:\s*$", line):
                in_by_role = True
                continue
            if in_by_role and re.match(r"^\S", line):
                in_by_role = False
            m = re.match(r"^\s+([A-Za-z][\w-]*):\s*\{\s*model:\s*([^,}\s]+)", line) if in_by_role else None
            if m and "haiku" in m.group(2):
                roles.add(m.group(1))
    return roles


def _skill_ids(root: Path) -> list[str]:
    return sorted(p.parent.name for p in (root / "skills").glob("*/SKILL.md"))


def listing_chars(root: Path) -> int:
    """Characters of the native skill listing: the frontmatter of every skill the lean default links."""
    linked = skill_scope.include_list(_skill_ids(root), env={}) or set()
    total = 0
    for sid in linked:
        m = _FRONTMATTER.match(_read(root / "skills" / sid / "SKILL.md"))
        total += len(m.group(1)) if m else len(sid)
    return total


def role_chars(role: str, root: Path) -> int:
    """Characters of a role's own text: the role file plus the largest persona overlay of that role."""
    total = len(_read(root / "agents" / "roles" / f"{role}.md"))
    overlays = [len(_read(p)) for p in (root / "agents" / "personas").glob(f"{role}-*.md")]
    return total + (max(overlays) if overlays else 0)


def preload_chars(role: str, root: Path) -> int:
    return sum(len(_read(root / "skills" / sid / "SKILL.md")) for sid in skill_scope.ROLE_SKILLS.get(role, ()))


def estimate_tokens(role: str, root: Path) -> int:
    static_chars = role_chars(role, root) + preload_chars(role, root) + listing_chars(root)
    return FIXED_PREFIX_TOKENS + int(static_chars / CHARS_PER_TOKEN * SAFETY_MARGIN)


def over_budget(root: Path) -> list[tuple[str, int]]:
    """[(role, estimated tokens)] for every Haiku role above the threshold."""
    return [(r, e) for r in sorted(haiku_roles(root)) if (e := estimate_tokens(r, root)) > HAIKU_PROMPT_THRESHOLD_TOKENS]
