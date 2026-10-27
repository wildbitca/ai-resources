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

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from ..roles_policy import model_family

POOL_MAX = "claude-subscription"       # Claude Code on the Max subscription (flat fee)
POOL_AGY_GEMINI = "agy-gemini"         # Antigravity, Google AI Pro quota, Gemini models
POOL_AGY_CLAUDE = "agy-claude-gpt"     # Antigravity, Google AI Pro quota, Claude / GPT models
POOL_ANTHROPIC_API = "anthropic-api"   # prepaid Anthropic API credits
POOL_GEMINI_API = "gemini-api"         # Gemini API (Google Cloud credit)
POOL_CODEX = "codex"                   # OPTIONAL pool: active only when Codex is installed and signed in, in no default cell

CREDIT_POOLS = frozenset({POOL_ANTHROPIC_API, POOL_GEMINI_API})

POOLS: dict[str, dict[str, str]] = {
    POOL_MAX: {"label": "Claude Code (Max subscription)", "kind": "subscription"},
    POOL_AGY_GEMINI: {"label": "Antigravity (AI Pro quota, Gemini)", "kind": "subscription"},
    POOL_AGY_CLAUDE: {"label": "Antigravity (AI Pro quota, Claude/GPT)", "kind": "subscription"},
    POOL_ANTHROPIC_API: {"label": "Anthropic API credits", "kind": "credit"},
    POOL_GEMINI_API: {"label": "Gemini API credits", "kind": "credit"},
    POOL_CODEX: {"label": "Codex (optional)", "kind": "subscription"},
}

# backend token -> how its pool is derived
_BACKEND_POOL = {"claude-code": POOL_MAX, "claude-cli": POOL_MAX, "anthropic-api": POOL_ANTHROPIC_API,
                 "gemini-api": POOL_GEMINI_API, "codex": POOL_CODEX}
BACKENDS = ("claude-code", "claude-cli", "agy", "anthropic-api", "gemini-api", "codex")

SPILL_AT = 0.85            # a pool at or above this used fraction hands the class to the next pool


def pool_of(cell: str) -> str:
    """The pool a cell (`backend[:model]`) drains. `agy:<claude-*>` drains the Claude/GPT pool, other agy the Gemini pool."""
    backend, _, model = str(cell).partition(":")
    if backend == "agy":
        return POOL_AGY_CLAUDE if model.startswith(("claude", "gpt")) else POOL_AGY_GEMINI
    return _BACKEND_POOL.get(backend, "")


# Fallback triggers (ADR-0019): a role's fallback fires ONLY when its pool is exhausted or the model does not answer.
FALLBACK_CAUSES = ("pool-exhausted", "pool-hot", "timeout", "5xx", "empty", "agy-missing", "codex-missing", "key-invalid")

# Quota-exhaustion texts as the orchestrator witnesses them, for every pool. A child's own report is never an input.
_COMMON_QUOTA_PATTERNS: tuple[str, ...] = (
    r"hit your limit",
    r"usage limit",
    r"rate[_ -]?limit",
    r"quota[_ -]?exceeded",
    r"resource[_ -]?exhausted",
    r"too many requests",
    r"\b429\b",
    r"overloaded_error",
    r"credit balance is too low",
    r"out of .*credits",
)
QUOTA_PATTERNS: dict[str, tuple[str, ...]] = {pool: _COMMON_QUOTA_PATTERNS for pool in POOLS}


def classify_quota(text: str, pool: str, patterns: Mapping[str, tuple[str, ...]] | None = None) -> str | None:
    """The matching pattern when `text` is a quota-exhaustion error of `pool`, else None."""
    for pat in (QUOTA_PATTERNS if patterns is None else patterns).get(pool, ()):
        if re.search(pat, text, re.I | re.M):
            return pat
    return None


@dataclass(frozen=True)
class Role:
    """One row of the matrix: ONE primary and ONE ordered fallback chain. Checkers have no fixed primary: they take
    the first `CHECKER_LADDER` rung whose model family is not one of the run's writer families."""
    name: str
    kind: str                                  # "team" (kit roles) | "chat" (OpenClaw chat plane)
    klass: str                                 # key of CLASS_POOLS
    primary: str | None
    fallback: tuple[str, ...] = ()
    checker: bool = False
    writes: bool = False
    note: str = ""

    @property
    def chain(self) -> tuple[str, ...]:
        return (CHECKER_LADDER if self.checker and self.primary is None else (self.primary,)) + self.fallback


# Checker ladder, cheapest adequate first. The checker must be of a family that wrote none of the run's work.
CHECKER_LADDER = ("claude-code:sonnet", "claude-code:opus", "agy:claude-opus-5-5")

_FLASH, _ARGON = "agy:gemini-3.8-flash-high", "agy:gemini-4-argon-high"
_SONNET, _HAIKU = "claude-code:sonnet", "claude-code:haiku"
_API_SONNET, _API_HAIKU, _GEMINI_API = ("anthropic-api:claude-sonnet-5-5", "anthropic-api:claude-haiku-5-5",
                                         "gemini-api:gemini-3.8-flash")


def _team(name: str, klass: str, primary: str | None, fallback: tuple[str, ...], **kw: Any) -> tuple[str, Role]:
    return name, Role(name, "team", klass, primary, fallback, **kw)


def _chat(name: str, klass: str, primary: str, fallback: str, note: str = "") -> tuple[str, Role]:
    return name, Role(name, "chat", klass, primary, (fallback,), note=note)


# Inferred matrix (ADR-0019 D4), overridable per profile. Antigravity primaries: read, doc, test and upgrade work; Claude
# (Max subscription) primaries: planning, architecture, implementation and the fixers. Team fallbacks never sit on an
# API-credit pool.
ROLES: dict[str, Role] = dict([
    _team("explore", "team-read", _FLASH, (_HAIKU,)),
    _team("code-reviewer", "team-read", _ARGON, (_SONNET,)),
    _team("doc-writer", "team-write", _FLASH, (_HAIKU,), writes=True),
    _team("tester", "team-write", _ARGON, (_SONNET,), writes=True),
    _team("package-upgrade", "team-write", _FLASH, (_HAIKU,), writes=True),
    _team("generalPurpose", "team-write", _ARGON, (_SONNET,), writes=True),
    _team("planner", "team-plan", _SONNET, (_ARGON,)),
    _team("software-architect", "team-plan", _SONNET, (_ARGON,)),
    *[_team(r, "team-write", _SONNET, (_ARGON,), writes=True)
      for r in ("implementer", "crashlytics-fixer", "sentry-fixer", "terraform-maintainer",
                "crossplane-upjet-maintainer")],
    _team("verifier", "team-check", None, (), checker=True),
    _team("security-auditor", "team-check", None, (), checker=True),
    # Chat plane: OpenClaw agents and utility work (ADR-0017 workload map).
    _chat("main", "chat", _API_SONNET, _API_HAIKU),
    _chat("channel-agents", "chat", _API_SONNET, _API_HAIKU),
    _chat("utility", "chat-light", _GEMINI_API, _API_HAIKU),
    _chat("summarization", "chat-light", _GEMINI_API, _API_HAIKU),
    _chat("explore-bulk", "chat-light", _GEMINI_API, _API_HAIKU),
    _chat("heartbeat", "chat-light", _API_HAIKU, _GEMINI_API,
          "Gemini tool-calling through OpenClaw is unproven; heartbeat stays on Haiku"),
])

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


def _chain_of(value: Any, where: str) -> tuple[str, ...]:
    cells = (value,) if isinstance(value, str) else tuple(value) if isinstance(value, (list, tuple)) else None
    if cells is None:
        raise RoutingError(f"{where}: expected a cell or a list of cells")
    for c in cells:
        bad = validate_cell(c)
        if bad:
            raise RoutingError(f"{where}: {bad}")
    return cells


def merged(profile_doc: Mapping[str, Any] | None = None) -> dict[str, Role]:
    """`ROLES` with a profile's `routing: {role: {primary, fallback}}` overrides laid over it. Raises `RoutingError`."""
    over = (profile_doc or {}).get("routing") or {}
    if not isinstance(over, Mapping):
        raise RoutingError("routing: must be a mapping")
    out = dict(ROLES)
    for role, cells in over.items():
        if role not in out:
            raise RoutingError(f"routing.{role}: unknown role (expected one of {', '.join(sorted(ROLES))})")
        if not isinstance(cells, Mapping):
            raise RoutingError(f"routing.{role}: expected a mapping")
        changes: dict[str, Any] = {}
        if "primary" in cells:
            changes["primary"] = _chain_of(cells["primary"], f"routing.{role}.primary")[0]
        if "fallback" in cells:
            changes["fallback"] = _chain_of(cells["fallback"], f"routing.{role}.fallback")
        if "class" in cells:
            if cells["class"] not in CLASS_POOLS:
                raise RoutingError(f"routing.{role}.class: unknown class {cells['class']!r}")
            changes["klass"] = cells["class"]
        out[role] = replace(out[role], **changes)
    for r in out.values():
        if r.kind == "team":
            for cell in r.chain:
                if pool_of(cell) in CREDIT_POOLS:
                    raise RoutingError(f"routing.{r.name}: {cell} drains an API-credit pool; team roles never fall back onto one")
    return out


def _cell_doc(cell: str) -> dict[str, str]:
    return {"cell": cell, "pool": pool_of(cell)}


def render_section(profile_doc: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `routing:` section of `lanes.yaml`: matrix (role -> class, primary, fallback chain) and the spill rule."""
    matrix: dict[str, Any] = {}
    for name, r in sorted(merged(profile_doc).items()):
        chain = r.chain
        entry: dict[str, Any] = {"class": r.klass, "primary": _cell_doc(chain[0]),
                                 "fallback": [_cell_doc(c) for c in chain[1:]]}
        if r.checker and r.primary is None:
            entry["checker"] = True
        if r.note:
            entry["note"] = r.note
        matrix[name] = entry
    return {"matrix": matrix,
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


def _show(cell: str) -> str:
    return f"{cell} [{pool_of(cell)}]"


def table_rows(profile_doc: Mapping[str, Any] | None = None, *, chat: bool = True,
               unavailable: Mapping[str, str] | None = None) -> list[tuple[str, str, str]]:
    """`(role, "primary [pool]", "fallback [pool] -> ...")` rows: the one table docs, setup and doctor print.

    `chat=False` leaves the OpenClaw chat plane out. A role whose primary cell is skipped (`unavailable` maps a backend
    to its cause, as `detection.inactive_backends()` does) shows the cell it runs on instead, and its Fallback column
    names the cause and the skipped cell, so a fallback is never silent."""
    rows = []
    for name, r in sorted(merged(profile_doc).items(), key=lambda kv: (kv[1].kind, kv[0])):
        if r.kind == "chat" and not chat:
            continue
        if r.checker and r.primary is None:
            rows.append((name, "per-run checker ladder", " -> ".join(_show(c) for c in r.chain)))
            continue
        chain = r.chain
        fallback = " -> ".join(_show(c) for c in chain[1:]) or "-"
        route = resolve(name, unavailable=unavailable, profile_doc=profile_doc) if unavailable else None
        if route is not None and route.cause and route.cell != chain[0]:
            used = _show(route.cell) if route.cell else "blocked"
            rest = " -> ".join(_show(c) for c in route.fallbacks)
            fallback = f"{route.cause}: {chain[0]} skipped" + (f"; then {rest}" if rest else "")
            rows.append((name, used, fallback))
            continue
        if r.note:
            fallback += f" (gated: {r.note})"
        rows.append((name, _show(chain[0]), fallback))
    return rows


def table_lines(profile_doc: Mapping[str, Any] | None = None) -> list[str]:
    return [f"{role:<28} {primary}  ->  {fb}" for role, primary, fb in table_rows(profile_doc)]


_OPENCLAW_CLAUDE = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
_OPENCLAW_PROVIDER = {"agy": "agy-cli-lane", "gemini-api": "google", "anthropic-api": "anthropic",
                      "codex": "codex"}
CLAUDE_CLI_RUNTIME = "claude-cli"      # OpenClaw agent runtime that runs a Claude cell on the Max subscription


def cell_to_openclaw_ref(cell: str, effective: Mapping[str, str] | None = None) -> str:
    """The OpenClaw model ref of a cell (`claude-code:sonnet` -> `anthropic/claude-sonnet-5-5`).

    `effective` maps a Claude alias to the id the host runs now (`model_pins.effective()`); without it the shipped
    ids above apply, so this stays pure."""
    bad = validate_cell(cell)
    if bad:
        raise RoutingError(bad)
    backend, _, model = cell.partition(":")
    if backend in ("claude-code", "claude-cli"):
        alias = model or "sonnet"
        bare = (effective or {}).get(alias) or _OPENCLAW_CLAUDE.get(alias, alias)
        return f"anthropic/{bare}"
    return f"{_OPENCLAW_PROVIDER[backend]}/{model}"


def runtime_of_cell(cell: str) -> str:
    """The OpenClaw agent runtime a cell must be pinned to: `claude-cli` for the Max cells (so a kit-role agent never
    bills the Anthropic API), "" for every other backend."""
    return CLAUDE_CLI_RUNTIME if cell.partition(":")[0] in ("claude-code", "claude-cli") else ""


def claude_alias(cell: str) -> str:
    """The Claude Code `model:` word of a Max cell (`claude-code:sonnet` -> `sonnet`; bare `claude-code` -> `inherit`)."""
    backend, _, model = cell.partition(":")
    return model if backend in ("claude-code", "claude-cli") and model else "inherit"


def openclaw_chain(role: Role, effective: Mapping[str, str] | None = None) -> tuple[str, tuple[str, ...]]:
    """`(primary ref, fallback refs)` of a role for an OpenClaw agent, duplicates and repeats of the primary removed.
    A dynamic checker (no fixed primary) takes the first ladder rung as primary and the rest as fallbacks: OpenClaw
    cannot know the run's writer families, so the dispatcher overrides per spawn (`routing.resolve`)."""
    refs: list[str] = []
    for cell in role.chain:
        ref = cell_to_openclaw_ref(cell, effective)
        if ref not in refs:
            refs.append(ref)
    return refs[0], tuple(refs[1:])


@dataclass(frozen=True)
class Route:
    role: str
    cell: str | None
    pool: str
    fallbacks: tuple[str, ...]
    reason: str
    cause: str | None = None          # why the route is not the role's primary (one of FALLBACK_CAUSES), else None
    blocked: bool = False

    @property
    def backend(self) -> str:
        return (self.cell or "").partition(":")[0]

    @property
    def model(self) -> str:
        return (self.cell or "").partition(":")[2]


UNKNOWN_FAMILY = "unknown"        # the family recorded for a model `model_family` does not know (never silently dropped)


def _family(cell: str) -> str | None:
    return model_family(cell.partition(":")[2])


def resolve(role: str, usage: Mapping[str, float | None] | None = None, writers: Iterable[str] = (),
            unavailable: Mapping[str, str] | None = None, profile_doc: Mapping[str, Any] | None = None,
            spill_at: float = SPILL_AT) -> Route:
    """Where `role` runs now. Pure. Walks the role's chain and takes the first rung that is available, under `spill_at`
    and (for a checker) of a family that wrote none of the run; the cause of each skip is the recorded fallback cause.

    `usage` is {pool: used fraction|None}; `writers` are model refs or families of the run's writers; `unavailable`
    maps a backend or a cell to a cause (`agy-missing`, `key-invalid`, `timeout`, ...). A checker with no independent
    rung is `blocked`, never silently weaker."""
    table = merged(profile_doc)
    if role not in table:
        raise RoutingError(f"no routing for role {role!r}")
    r, usage, unavailable = table[role], usage or {}, unavailable or {}
    families = {model_family(w) or UNKNOWN_FAMILY for w in writers if str(w).strip()}
    cause: str | None = None
    skipped: list[str] = []
    chain = r.chain
    unknown_writer = r.checker and UNKNOWN_FAMILY in families        # a writer of unknown family: fail closed
    if unknown_writer:
        return Route(role, None, "", (), "a writer of unknown model family: independence cannot be shown",
                     "no-independent-checker", blocked=True)
    independence_skip = False
    for i, cell in enumerate(chain):
        pool = pool_of(cell)
        down = unavailable.get(cell) or unavailable.get(cell.partition(":")[0])
        used = usage.get(pool)
        if down:
            cause, why = down, f"{cell} unavailable ({down})"
        elif used is not None and used >= spill_at:
            cause = "pool-exhausted" if used >= 1.0 else "pool-hot"
            why = f"{pool} at {used * 100:.0f}%"
        elif r.checker and families and (_family(cell) is None or _family(cell) in families):
            skipped.append(f"{cell} shares a family with the writers")
            independence_skip = True
            continue                      # independence selection, not a fallback: no cause recorded
        else:
            return Route(role, cell, pool, chain[i + 1:], "; ".join(skipped) or "primary available", cause)
        skipped.append(why)
    return Route(role, None, "", (), "; ".join(skipped) or "no route",
                 "no-independent-checker" if r.checker and (independence_skip or not cause) else cause, blocked=True)
