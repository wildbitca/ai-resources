"""The single routing matrix and the consumption-balancing policy (ADR-0018).

Pure data and pure functions: no I/O, no clock, no credential. Two planes use four pools:

* `team` plane: kit roles (planner, implementer, explore, ...) run on Claude Code (Max subscription) or on
  Antigravity (Google AI Pro quota). These are PRIMARY routes, not fallbacks.
* `chat` plane: OpenClaw chat agents and utility work run on the Anthropic API credits (primary) and the Gemini API
  credits (cheap, tool-light work).

`render_section()` is what `lanes.yaml` carries under `routing:`; `choose_pool()` is the balancing rule the doctor
and any dispatcher can apply to live pool usage. A profile may override single matrix cells with a top-level
`routing:` block (same cell grammar); the matrix here is the default for every profile.
"""
from __future__ import annotations

from typing import Any, Mapping

POOL_MAX = "claude-subscription"       # Claude Code on the Max subscription (flat fee)
POOL_AGY_GEMINI = "agy-gemini"         # Antigravity, Google AI Pro quota, Gemini models
POOL_AGY_CLAUDE = "agy-claude-gpt"     # Antigravity, Google AI Pro quota, Claude / GPT models
POOL_ANTHROPIC_API = "anthropic-api"   # prepaid Anthropic API credits
POOL_GEMINI_API = "gemini-api"         # Gemini API (Google Cloud credit)

POOLS: dict[str, dict[str, str]] = {
    POOL_MAX: {"label": "Claude Code (Max subscription)", "kind": "subscription"},
    POOL_AGY_GEMINI: {"label": "Antigravity (AI Pro quota, Gemini)", "kind": "subscription"},
    POOL_AGY_CLAUDE: {"label": "Antigravity (AI Pro quota, Claude/GPT)", "kind": "subscription"},
    POOL_ANTHROPIC_API: {"label": "Anthropic API credits", "kind": "credit"},
    POOL_GEMINI_API: {"label": "Gemini API credits", "kind": "credit"},
}

# backend token -> how its pool is derived
_BACKEND_POOL = {"claude-code": POOL_MAX, "claude-cli": POOL_MAX, "anthropic-api": POOL_ANTHROPIC_API,
                 "gemini-api": POOL_GEMINI_API}
BACKENDS = ("claude-code", "claude-cli", "agy", "anthropic-api", "gemini-api")
PLANES = ("team", "chat")

SPILL_AT = 0.85            # a pool at or above this used fraction hands the class to the next pool


def pool_of(cell: str) -> str:
    """The pool a cell (`backend[:model]`) drains. `agy:<claude-*>` drains the Claude/GPT pool, other agy the Gemini pool."""
    backend, _, model = str(cell).partition(":")
    if backend == "agy":
        return POOL_AGY_CLAUDE if model.startswith(("claude", "gpt")) else POOL_AGY_GEMINI
    return _BACKEND_POOL.get(backend, "")


# Per-role matrix. Cells are `backend[:model]`. Primary is where the role runs by default; secondary is the next route
# (availability, or the pool is hot). Writers stay on Claude Code (ADR-0009 OD-4): no agy writer.
TEAM_MATRIX: dict[str, dict[str, str]] = {
    "explore":            {"primary": "agy:gemini-3.8-flash-high", "secondary": "claude-cli:haiku", "class": "team-read"},
    "code-reviewer":      {"primary": "agy:gemini-4-argon-high", "secondary": "claude-code:sonnet", "class": "team-read"},
    "planner":            {"primary": "claude-code:sonnet", "secondary": "agy:gemini-4-argon-high", "class": "team-plan"},
    "software-architect": {"primary": "claude-code:sonnet", "secondary": "agy:gemini-4-argon-high", "class": "team-plan"},
    "implementer":        {"primary": "claude-code:sonnet", "secondary": "claude-cli:sonnet", "class": "team-write"},
    "tester":             {"primary": "claude-code:haiku", "secondary": "claude-cli:haiku", "class": "team-write"},
    "doc-writer":         {"primary": "claude-code:haiku", "secondary": "claude-cli:haiku", "class": "team-write"},
    "generalPurpose":     {"primary": "claude-code:sonnet", "secondary": "claude-cli:sonnet", "class": "team-write"},
    "package-upgrade":    {"primary": "claude-code:haiku", "secondary": "claude-cli:haiku", "class": "team-write"},
    "verifier":           {"primary": "claude-code:opus", "secondary": "agy:claude-opus-5-5", "class": "team-check"},
    "security-auditor":   {"primary": "claude-code:opus", "secondary": "agy:claude-opus-5-5", "class": "team-check"},
}

# Chat plane: OpenClaw agents and utility work (ADR-0017 workload map). `gated` names what must be proven before a
# cell becomes the default; until then the entry is `primary: <current>` and the gated cell is only the secondary.
CHAT_MATRIX: dict[str, dict[str, str]] = {
    "main":          {"primary": "anthropic-api:claude-sonnet-5-5", "secondary": "anthropic-api:claude-haiku-5-5", "class": "chat"},
    "channel-agents": {"primary": "anthropic-api:claude-sonnet-5-5", "secondary": "anthropic-api:claude-haiku-5-5", "class": "chat"},
    "utility":       {"primary": "gemini-api:gemini-3.8-flash", "secondary": "anthropic-api:claude-haiku-5-5", "class": "chat-light"},
    "summarization": {"primary": "gemini-api:gemini-3.8-flash", "secondary": "anthropic-api:claude-haiku-5-5", "class": "chat-light"},
    "explore":       {"primary": "gemini-api:gemini-3.8-flash", "secondary": "anthropic-api:claude-haiku-5-5", "class": "chat-light"},
    "heartbeat":     {"primary": "anthropic-api:claude-haiku-5-5", "secondary": "gemini-api:gemini-3.8-flash", "class": "chat-light",
                      "gated": "Gemini tool-calling through OpenClaw is unproven; heartbeat stays on Haiku"},
}

# Which pools each class of work drains, in order. The first pool under `spill_at` serves the class.
CLASS_POOLS: dict[str, tuple[str, ...]] = {
    "chat":       (POOL_ANTHROPIC_API,),
    "chat-light": (POOL_GEMINI_API, POOL_ANTHROPIC_API),
    "team-read":  (POOL_AGY_GEMINI, POOL_MAX),
    "team-plan":  (POOL_MAX, POOL_AGY_GEMINI),
    "team-write": (POOL_MAX,),
    "team-check": (POOL_MAX, POOL_AGY_CLAUDE),
}

# gentle-ai ideas adopted (docs/research/gentle-ai): the negative routing rule (a writer's model never adds a
# checker), per-class token budgets, and size-gated delegation. Budgets are tokens of prompt per call, advisory.
TOKEN_BUDGETS: dict[str, int] = {"team-read": 100_000, "chat-light": 100_000, "team-plan": 200_000,
                                 "team-write": 200_000, "team-check": 200_000, "chat": 200_000}


class RoutingError(ValueError):
    pass


def validate_cell(cell: Any) -> str | None:
    """None when `cell` is a valid `backend[:model]`, else the reason."""
    if not isinstance(cell, str) or not cell:
        return "cell must be a non-empty string"
    backend, _, model = cell.partition(":")
    if backend not in BACKENDS:
        return f"unknown backend {backend!r} (expected one of {', '.join(BACKENDS)})"
    if backend != "claude-code" and not model:
        return f"{backend} needs a model (`{backend}:<model>`)"
    return None


def merged(profile_doc: Mapping[str, Any] | None = None) -> dict[str, dict[str, dict[str, str]]]:
    """The matrix with a profile's `routing:` cell overrides laid over the default. Raises `RoutingError`."""
    out = {"team": {r: dict(c) for r, c in TEAM_MATRIX.items()}, "chat": {r: dict(c) for r, c in CHAT_MATRIX.items()}}
    over = (profile_doc or {}).get("routing") or {}
    if not isinstance(over, Mapping):
        raise RoutingError("routing: must be a mapping")
    for plane, roles in over.items():
        if plane not in PLANES or not isinstance(roles, Mapping):
            raise RoutingError(f"routing.{plane}: expected one of {', '.join(PLANES)} mapping roles to cells")
        for role, cells in roles.items():
            if not isinstance(cells, Mapping):
                raise RoutingError(f"routing.{plane}.{role}: expected a mapping")
            for key in ("primary", "secondary"):
                if key in cells:
                    bad = validate_cell(cells[key])
                    if bad:
                        raise RoutingError(f"routing.{plane}.{role}.{key}: {bad}")
            out[plane].setdefault(role, {"class": "chat" if plane == "chat" else "team-write"}).update(
                {k: v for k, v in cells.items() if k in ("primary", "secondary", "class", "gated")})
    for plane, roles in out.items():
        for role, cells in roles.items():
            for key in ("primary", "secondary"):
                bad = validate_cell(cells.get(key)) if cells.get(key) else (f"missing {key}" if key == "primary" else None)
                if bad:
                    raise RoutingError(f"routing.{plane}.{role}.{key}: {bad}")
            if cells.get("class") not in CLASS_POOLS:
                raise RoutingError(f"routing.{plane}.{role}.class: unknown class {cells.get('class')!r}")
    return out


def render_section(profile_doc: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `routing:` section of `lanes.yaml`: matrix (cells with their pool), classes and the spill rule."""
    matrix = merged(profile_doc)
    plane_doc: dict[str, Any] = {}
    for plane, roles in sorted(matrix.items()):
        plane_doc[plane] = {}
        for role, cells in sorted(roles.items()):
            entry: dict[str, Any] = {"class": cells["class"],
                                     "primary": {"cell": cells["primary"], "pool": pool_of(cells["primary"])}}
            if cells.get("secondary"):
                entry["secondary"] = {"cell": cells["secondary"], "pool": pool_of(cells["secondary"])}
            if cells.get("gated"):
                entry["gated"] = cells["gated"]
            plane_doc[plane][role] = entry
    return {"matrix": plane_doc,
            "balancing": {"spill_at": SPILL_AT, "classes": {c: list(p) for c, p in sorted(CLASS_POOLS.items())}}}


def choose_pool(work_class: str, usage: Mapping[str, float | None], spill_at: float = SPILL_AT) -> tuple[str, str]:
    """`(pool, reason)` for a class of work given `usage` = {pool: used fraction 0..1, or None when unknown}.

    The first pool of the class under `spill_at` serves it (unknown counts as available: absence of a reading is
    not evidence of exhaustion). When every pool is hot the least used one serves it and the reason says so.
    Raises `RoutingError` for an unknown class."""
    pools = CLASS_POOLS.get(work_class)
    if not pools:
        raise RoutingError(f"unknown class {work_class!r}")
    for i, pool in enumerate(pools):
        used = usage.get(pool)
        if used is None or used < spill_at:
            note = "unknown usage, assumed available" if used is None else f"{used * 100:.0f}% used"
            return pool, (f"{note}" if i == 0 else f"spill: {pools[0]} at or above {spill_at * 100:.0f}%; {note}")
    known = [(usage.get(p) if usage.get(p) is not None else 0.0, p) for p in pools]
    used, pool = min(known)
    return pool, f"all pools of {work_class} at or above {spill_at * 100:.0f}%; least used is {used * 100:.0f}%"


def class_of(plane: str, role: str, profile_doc: Mapping[str, Any] | None = None) -> str:
    cells = merged(profile_doc).get(plane, {}).get(role)
    if not cells:
        raise RoutingError(f"no routing for {plane}/{role}")
    return cells["class"]


def table_lines(profile_doc: Mapping[str, Any] | None = None) -> list[str]:
    """Plain text rows `plane role  primary [pool]  ->  secondary [pool]` for docs, explain and doctor."""
    lines: list[str] = []
    for plane, roles in sorted(merged(profile_doc).items()):
        for role, c in sorted(roles.items()):
            sec = f" -> {c['secondary']} [{pool_of(c['secondary'])}]" if c.get("secondary") else ""
            lines.append(f"{plane:<5} {role:<19} {c['primary']} [{pool_of(c['primary'])}]{sec}")
    return lines
