"""Role policy shared by the lanes pass, the lane kit commands and `scripts/validate_kit.py` (ADR-0007).

Pure data and pure helpers: no I/O, no imports from the rest of the package, so the standalone
validator can load it without the scoring or setup code.

The table says what a role MAY do on a lane, never where it runs. There is no backend column: lanes
are peers and a role is ineligible only where a mechanism (a write contract) is missing.
"""
from __future__ import annotations

import re
from typing import Iterable

REPORT = "report"
SCOPE = "scope"
INTEGRATING = "integrating"
CONTRACTS = (REPORT, SCOPE, INTEGRATING)

# Roles that only read the run tree and write their step-scoped report.
REPORT_ROLES = frozenset({"explore", "planner", "software-architect", "code-reviewer",
                          "security-auditor", "verifier"})
# Writer roles that may take a `write_scope` (own worktree, declared disjoint scope).
SCOPE_ROLES = frozenset({"tester", "doc-writer", "generalPurpose", "package-upgrade",
                         "crashlytics-fixer", "sentry-fixer", "terraform-maintainer",
                         "crossplane-upjet-maintainer"})
DEFAULT_INTEGRATING_WRITER = "implementer"
# Checker roles: independent of every writer by model family (ADR-0007 section 8).
CHECKER_ROLES = frozenset({"verifier", "security-auditor"})

# Which write contracts a role may hold on a lane step. A role absent from this table is ineligible
# (there is no contract to enforce).
LANE_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    **{role: (REPORT,) for role in REPORT_ROLES},
    "tester": (REPORT, SCOPE),
    "doc-writer": (REPORT, SCOPE),
    "package-upgrade": (REPORT, SCOPE, INTEGRATING),
    "generalPurpose": (REPORT, SCOPE, INTEGRATING),
    "crashlytics-fixer": (REPORT, SCOPE, INTEGRATING),
    "sentry-fixer": (REPORT, SCOPE, INTEGRATING),
    "terraform-maintainer": (REPORT, SCOPE, INTEGRATING),
    "crossplane-upjet-maintainer": (REPORT, SCOPE, INTEGRATING),
    DEFAULT_INTEGRATING_WRITER: (REPORT, INTEGRATING),
}

# Explicit `openclaw-agent` steps (ADR-0005): the sets validate_kit used to hold literally.
EXPLICIT_READ_ONLY_ROLES = REPORT_ROLES
EXPLICIT_WARNED_ROLES = frozenset({"tester", "doc-writer"})

# Repo-relative paths no lane contract may write. The handoff file and other steps' `.agent-output/`
# files are run-specific and added by the kit command.
LANE_WRITE_FORBIDDEN = (".git", ".gitmodules", ".gitattributes", ".githooks/", ".claude/",
                        ".cursor/", ".openclaw/", "agy_gate_evidence.json")
# The committed evidence record of the Antigravity gates (ADR-0010 PD-7), repo-relative. Named in the forbidden list
# by its file name (the lists match a path component at any depth) and watched as a tripwire below.
AGY_GATE_EVIDENCE_REL = "scripts/ai_resources/data/agy_gate_evidence.json"
# A `write_scope` may additionally never sit in `.agent-output/` (step reports are allowed on their own).
SCOPE_FORBIDDEN = LANE_WRITE_FORBIDDEN + (".agent-output/", "handoff.md")
# Host files whose change fails a lane step's verify, on every lane (symmetric, ADR-0007 section 11). The
# first row is the phase 2 set; the second row is the persistence channels into the operator's other agents.
TRIPWIRE_PATHS = (AGY_GATE_EVIDENCE_REL, "~/.openclaw/openclaw.json", "~/.claude/settings.json", "~/.claude/hooks/",
                  "~/.config/ai-resources/", "~/.bashrc", "~/.bash_profile", "~/.profile",
                  "~/.zshrc", "~/.zprofile", "~/.zshenv",
                  "~/.claude/CLAUDE.md", "~/.claude/agents/", "~/.claude/skills/", "~/.agents/",
                  "~/.gitconfig", "~/.config/git/", "~/.ssh/", "~/.config/systemd/user/")
# Tripwires hashed by metadata (name, size, mtime, link target) instead of content: a directory of binaries.
TRIPWIRE_STAT_PATHS = ("~/.local/bin/",)
# The OpenClaw automation/cron store is located by the isolated harness (P3-S0) and recorded as evidence. Until
# then it is None and no path is guessed: a wrong path would only add false failures.
OPENCLAW_CRON_STORE_PATH: str | None = None
# `~/.gemini` config files an agy turn might write. Which of them are tripwires is DERIVED from V6/V7 (the files
# a benign agy turn does not write); nothing is active until that evidence is recorded (G2, G3).
GEMINI_TRIPWIRE_CANDIDATES = ("~/.gemini/settings.json", "~/.gemini/GEMINI.md", "~/.gemini/trustedFolders.json",
                              "~/.gemini/config/", "~/.gemini/antigravity-cli/settings.json",
                              "~/.gemini/antigravity-cli/mcp/")
# Credential files: never tripwired, never read, never hashed.
GEMINI_CREDENTIAL_NAMES = ("antigravity-oauth-token", "gemini-credentials.json", "mcp-oauth-tokens.json")


def gemini_tripwires(benign_writes: Iterable[str] | None = None) -> tuple[str, ...]:
    """The `~/.gemini` tripwires derived from a recorded benign agy turn: the candidates that turn did NOT write.

    `None` (no V6/V7 evidence) derives nothing, so no `~/.gemini` file is tripwired by assumption.
    """
    if benign_writes is None:
        return ()
    written = {w.rstrip("/") for w in benign_writes}
    return tuple(c for c in GEMINI_TRIPWIRE_CANDIDATES
                 if c.rstrip("/") not in written and not c.endswith(GEMINI_CREDENTIAL_NAMES))


# Quota-exhaustion error texts as the ORCHESTRATOR witnesses them (the `sessions_spawn` result or the
# completion the gateway returned), per pool: regex strings, recorded verbatim from a probe run. Empty until a
# V6/V7 run records one: a guessed pattern would let a malformed text trip or miss the breaker. A child's own
# report is never an input.
QUOTA_PATTERNS: dict[str, tuple[str, ...]] = {
    "claude-subscription": (),
    "agy-claude-gpt": (),
    "agy-gemini": (),
}


def classify_quota(text: str, pool: str, patterns: dict[str, tuple[str, ...]] | None = None) -> str | None:
    """The matching pattern when `text` is a recorded quota-exhaustion error of `pool`, else None."""
    for pat in (QUOTA_PATTERNS if patterns is None else patterns).get(pool, ()):
        if re.search(pat, text, re.I | re.M):
            return pat
    return None


# Exact tolerance list: changes here never fail a contract.
TOLERANCE_LIST = ("IDENTITY.md", "SOUL.md", "USER.md", ".pytest_cache/", "__pycache__/")
# OpenClaw bootstrap files: tolerated only while untracked (a commit adding one fails verify, both lanes).
BOOTSTRAP_FILES = ("IDENTITY.md", "SOUL.md", "USER.md")
# A commit message matching any of these is an assistant attribution (case-insensitive, multiline).
ATTRIBUTION_PATTERNS = (
    r"^\s*co-authored-by:",
    r"^.*generated\s+(?:with|by)\b.*(?:claude|anthropic|gemini|antigravity|google|openclaw|codex|openai|agy)",
    "\U0001F916",
)

_PROVIDER_PREFIXES = frozenset({"anthropic", "claude-cli", "claude-kit", "google", "vertex", "agy",
                                "agy-cli", "agy-cli-lane", "antigravity", "openrouter"})
_CLAUDE_FAMILIES = ("sonnet", "opus", "haiku", "fable")
_GEMINI_VARIANTS = ("flash", "pro", "ultra", "nano")
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


def role_of(name: str, roles: Iterable[str] | None = None) -> str | None:
    """A role name, or the role a `<role>-<domain>` persona belongs to (longest prefix match)."""
    known = set(roles) if roles is not None else set(LANE_ELIGIBILITY)
    if name in known:
        return name
    best = [r for r in known if name.startswith(f"{r}-")]
    return max(best, key=len) if best else None


def lane_eligible(role: object) -> bool:
    return isinstance(role, str) and role in LANE_ELIGIBILITY


def contracts_for(role: object) -> tuple[str, ...]:
    return LANE_ELIGIBILITY.get(role, ()) if isinstance(role, str) else ()


def default_contract(role: str, *, has_scope: bool = False, integrating: bool = False) -> str:
    """The contract a step runs under: integrating, scope (with a scope) or report."""
    if integrating:
        return INTEGRATING
    if has_scope:
        return SCOPE
    return REPORT


def scope_problems(path: object, forbidden: Iterable[str] = SCOPE_FORBIDDEN) -> list[str]:
    """Why a `write_scope` entry is invalid; empty when it is acceptable."""
    if not isinstance(path, str) or not path.strip():
        return ["must be a non-empty string"]
    if path != path.strip():
        return ["has leading or trailing whitespace"]
    problems: list[str] = []
    if "\\" in path or "\x00" in path:
        problems.append("contains a backslash or NUL")
    if path.startswith("/") or path.startswith("~") or re.match(r"^[A-Za-z]:", path):
        problems.append("must be relative to the repository")
    if any(ch in path for ch in "*?[]{}"):
        problems.append("must not contain a glob (only a trailing '/' marks a directory)")
    body = path[:-1] if path.endswith("/") else path
    parts = body.split("/") if body else []
    if not parts or body in {".", ""}:
        problems.append("must name a path below the repository root, not '.'")
    if ".." in parts:
        problems.append("must not contain '..'")
    if any(p in ("", ".") for p in parts):
        problems.append("is not normalised ('//' or '.' components)")
    if problems:
        return problems
    comp = tuple(parts)
    for f in forbidden:
        fcomp = tuple(f.rstrip("/").split("/"))
        if comp[: len(fcomp)] == fcomp or fcomp[: len(comp)] == comp:
            problems.append(f"overlaps the forbidden path '{f}'")
            break
    return problems


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
