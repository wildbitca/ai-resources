"""Role sets, the model-family normaliser and the write-scope helpers (ADR-0007, ADR-0019).

Pure data and pure helpers: no I/O, no imports from the rest of the package, so the standalone validator can load it
without the scoring or setup code. The sets say what a role MAY do, never where it runs: the routing matrix decides that.
"""
from __future__ import annotations

import re
from typing import Iterable

# Roles that only read the tree and write their step-scoped report (a role property, not a backend restriction).
REPORT_ROLES = frozenset({"explore", "planner", "software-architect", "code-reviewer",
                          "security-auditor", "verifier"})
# Writer roles besides the implementer: a run that has one claims a write scope (`scope_guard`).
SCOPE_ROLES = frozenset({"tester", "doc-writer", "generalPurpose", "package-upgrade",
                         "crashlytics-fixer", "sentry-fixer", "terraform-maintainer",
                         "crossplane-upjet-maintainer"})
# Roles that may run on an explicit `openclaw-agent` step with a warning (they run commands or write files).
WARNED_ROLES = frozenset({"tester", "doc-writer"})
# Checker roles: independent of every writer by model family (ADR-0007 section 8).
CHECKER_ROLES = frozenset({"verifier", "security-auditor"})

_PROVIDER_PREFIXES = frozenset({"anthropic", "claude-cli", "claude-kit", "google", "vertex", "agy",
                                "agy-cli", "agy-cli-lane", "antigravity", "openrouter"})
_CLAUDE_FAMILIES = ("sonnet", "opus", "haiku", "fable")
_GEMINI_VARIANTS = ("flash", "pro", "ultra", "nano", "argon")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def model_family(ref: object) -> str | None:
    """The single normaliser for model refs (C1): a family such as `sonnet` or `gemini-pro`, or None.

    Accepts aliases (`sonnet`), `anthropic/…`, `claude-cli/…`, `claude-kit/…`, `google/…` and agy slugs
    or display names. `None` means "unknown family": checkers treat it as conflicting (fail closed).
    """
    if not isinstance(ref, str):
        return None
    text = ref.strip().lower()
    if not text:
        return None
    while "/" in text:
        head, _, rest = text.partition("/")
        if head not in _PROVIDER_PREFIXES:
            return None
        text = rest.strip()
    tokens = [t for t in _TOKEN_SPLIT.split(text) if t]
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0] in _CLAUDE_FAMILIES:
        return tokens[0]
    if tokens[0] == "claude":
        found = [t for t in tokens[1:] if t in _CLAUDE_FAMILIES]
        return found[0] if len(set(found)) == 1 else None
    if tokens[:2] == ["gpt", "oss"]:
        return "gpt-oss"                  # Antigravity's open-weight model: its own family, never a Claude one
    if tokens[0] == "gemini":
        variants = [t for t in tokens[1:] if t in _GEMINI_VARIANTS]
        return f"gemini-{variants[0]}" if variants else None
    return None


def scopes_overlap(a: str, b: str) -> bool:
    """True when one scope path is, or sits below, the other (component-wise)."""
    ca = tuple(a.rstrip("/").split("/"))
    cb = tuple(b.rstrip("/").split("/"))
    n = min(len(ca), len(cb))
    return ca[:n] == cb[:n]


def path_in_scope(path: str, scope: Iterable[str]) -> bool:
    """True when the repo-relative `path` is covered by an entry of `scope`."""
    comp = tuple(path.split("/"))
    for s in scope:
        sc = tuple(s.rstrip("/").split("/"))
        if comp[: len(sc)] == sc:
            return True
    return False
