"""Per-cockpit authentication answers (ADR-0015 D1).

Each cockpit asks its own auth; the kit state keeps only method names and provider ids, never a key or a token
(`SetupState.cockpit_auth`, additive, no schema bump). The legacy global fields (`mode`, `cli_team`, `cli_team_api`)
stay readable and are DERIVED from these answers by `apply_legacy_flags`; they no longer drive anything.

Pure functions over a `SetupState`: no prompt, no I/O.
"""
from __future__ import annotations

from typing import Any, NamedTuple

# cockpit id -> allowed methods. OpenClaw's methods are per provider (api_key | cli).
CLAUDE_METHODS = ("subscription", "api_key", "vertex_adc")
OPENCLAW_PROVIDER_METHODS = ("api_key", "cli")
AGY_METHODS = ("google_account",)
CURSOR_METHODS = ("managed",)

METHODS: dict[str, tuple[str, ...]] = {
    "claude": CLAUDE_METHODS,
    "openclaw": OPENCLAW_PROVIDER_METHODS,
    "agy": AGY_METHODS,
    "cursor": CURSOR_METHODS,
}

# The cockpits that ask a question. Cursor is listed so the review can say "managed in Cursor".
ASKING = ("claude", "agy", "openclaw")
TEAM_PROFILE = "balanced"        # ADR-0010 N4: the one default preset; replaces the CLI_TEAM_DEFAULT_PROFILE logic

SECRET_KEY_WORDS = ("key", "token", "secret", "password")


class Row(NamedTuple):
    """One line of the one-time old -> new table."""
    cockpit: str
    old: str
    new: str


def default_profile_for(s: Any = None) -> str:
    """The profile a fresh setup starts from, whichever cockpits are ticked (replaces the global-mode logic)."""
    return TEAM_PROFILE


def entry(s: Any, cockpit: str) -> dict:
    cur = (getattr(s, "cockpit_auth", None) or {}).get(cockpit)
    return cur if isinstance(cur, dict) else {}


def method(s: Any, cockpit: str) -> str:
    return str(entry(s, cockpit).get("method") or "")


def providers(s: Any) -> dict[str, str]:
    """OpenClaw: provider id -> method name, in the order they were enabled."""
    raw = entry(s, "openclaw").get("providers")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _slot(s: Any, cockpit: str) -> dict:
    if not isinstance(getattr(s, "cockpit_auth", None), dict):
        s.cockpit_auth = {}
    cur = s.cockpit_auth.get(cockpit)
    if not isinstance(cur, dict):
        cur = {}
        s.cockpit_auth[cockpit] = cur
    return cur


def set_method(s: Any, cockpit: str, value: str) -> None:
    if value not in METHODS.get(cockpit, ()):
        raise ValueError(f"{value!r} is not an auth method of {cockpit}")
    _slot(s, cockpit)["method"] = value


def set_providers(s: Any, chosen: dict[str, str]) -> None:
    """Replace OpenClaw's provider map (ids and method names only)."""
    for prov, how in chosen.items():
        if how not in OPENCLAW_PROVIDER_METHODS:
            raise ValueError(f"{how!r} is not an auth method of provider {prov}")
    slot = _slot(s, "openclaw")
    slot["providers"] = dict(chosen)
    slot.setdefault("method", next(iter(chosen.values()), "api_key"))


def set_agent_providers(s: Any, per_agent: dict[str, list[str]]) -> None:
    """OpenClaw: agent id -> the provider ids OpenClaw reported usable for that agent (ids only)."""
    _slot(s, "openclaw")["agents"] = {str(a): sorted(p) for a, p in per_agent.items()}


def agent_providers(s: Any, agent: str) -> "list[str] | None":
    """The providers recorded usable for one agent, or None when that agent was never read."""
    raw = entry(s, "openclaw").get("agents")
    got = raw.get(agent) if isinstance(raw, dict) else None
    return [str(p) for p in got] if isinstance(got, list) else None


def set_none_usable(s: Any, flag: bool) -> None:
    """OpenClaw was readable and reported no usable provider: an authoritative empty set, not "never asked"."""
    slot = _slot(s, "openclaw")
    if flag:
        slot["none_usable"] = True
    else:
        slot.pop("none_usable", None)


def none_usable(s: Any) -> bool:
    return bool(entry(s, "openclaw").get("none_usable"))


def legacy_label(s: Any) -> str:
    """Which of the four old answers a saved state expressed, '' for a state that never answered."""
    if getattr(s, "mode", "") == "multi-model":
        return "advanced"
    if getattr(s, "cli_team_api", False):
        return "team_api"
    if getattr(s, "cli_team", False):
        return "team"
    if getattr(s, "last_run", "") or getattr(s, "cockpits", None):
        return "alone"
    return ""


_LEGACY_TEXT = {
    "advanced": "Advanced (gateway)",
    "team_api": "As a team (Vertex AI / API keys)",
    "team": "As a team (subscriptions)",
    "alone": "Each on its own",
}


def _selected(s: Any) -> list[str]:
    targets = (getattr(getattr(s, "profile", None), "customizations", None) or {}).get("__targets__") or {}
    return list(targets.get("selected") or [])


def migrate_legacy(s: Any, *, vertex: bool = False) -> list[Row]:
    """Fill `cockpit_auth` from the old global answer, fill-only: a non-empty entry is never touched.

    Returns the old -> new rows for what was filled (empty when nothing was). A state that already holds answers
    returns [] on every later call, which is what makes the table appear once. `vertex` says a Vertex project is
    configured, so an API-key team maps to `vertex_adc`.
    """
    label = legacy_label(s)
    if not label:
        return []
    chosen = [c for c in _selected(s) if c in ASKING] or ["claude"]
    api_method = "vertex_adc" if vertex else "api_key"
    claude_new = {"advanced": "subscription", "team_api": api_method, "team": "subscription",
                  "alone": "subscription"}[label]
    provider_new = {"advanced": "api_key", "team_api": "api_key", "team": "cli", "alone": "cli"}[label]
    rows: list[Row] = []
    old = _LEGACY_TEXT[label]
    for cockpit in chosen:
        if method(s, cockpit):
            continue
        new = {"claude": claude_new, "openclaw": provider_new, "agy": "google_account"}[cockpit]
        set_method(s, cockpit, new)
        rows.append(Row(cockpit, old, new))
    return rows


def apply_legacy_flags(s: Any) -> None:
    """Derive the old global fields from the cockpit answers so a reader that still looks at them agrees.

    The team is always the kit team on the CLIs the operator has (`mode: single-model`, `cli_team`); `cli_team_api` is
    set when any selected cockpit authenticates with an API key or Vertex. The gateway is never selected here: a saved
    `backend` is left as it is (nothing is torn down).
    """
    s.mode = "single-model"
    s.cli_team = True
    wanted = set(_selected(s)) or {"claude"}
    api = False
    if "claude" in wanted and method(s, "claude") in ("api_key", "vertex_adc"):
        api = True
    if "openclaw" in wanted and "api_key" in providers(s).values():
        api = True
    s.cli_team_api = api


import re as _re

_TOKEN_LIKE = _re.compile(r"(sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]+|[A-Za-z0-9_-]{40,})")


def looks_like_secret(text: str) -> bool:
    """Whether a string looks like a key or token (the kit stores paths and commands, never one of these)."""
    return bool(_TOKEN_LIKE.search(text or ""))


# --- live sign-in (ADR-0016 D1): what the cockpit itself reports, recorded as a kind name ------------------------------

def live_kind(s: Any, cockpit: str) -> str:
    return str(entry(s, cockpit).get("live") or "")


def set_live(s: Any, cockpit: str, kind: str) -> None:
    _slot(s, cockpit)["live"] = kind


def set_provider_live(s: Any, kinds: dict[str, str]) -> None:
    """OpenClaw: provider id -> the live credential kind OpenClaw reports (ids and kind names only)."""
    _slot(s, "openclaw")["live"] = dict(kinds)


def helper(s: Any, cockpit: str = "claude") -> str:
    """The apiKeyHelper path or command the operator chose (never a key)."""
    return str(entry(s, cockpit).get("helper") or "")


def set_helper(s: Any, cockpit: str, value: str) -> None:
    if looks_like_secret(value):
        raise ValueError("a key or token cannot be stored as a helper")
    if not value:
        if "helper" in entry(s, cockpit):
            _slot(s, cockpit).pop("helper", None)         # forgetting a helper leaves no empty key behind
        return
    _slot(s, cockpit)["helper"] = value


def secret_findings(doc: Any) -> list[str]:
    """Paths in a serialized `cockpit_auth` that look like a secret (a secret-named key or a token-like value)."""
    token_like = _TOKEN_LIKE
    bad: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if any(w in str(k).lower() for w in SECRET_KEY_WORDS):
                    bad.append(f"{path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and token_like.search(node):
            bad.append(path)

    walk(doc, "cockpit_auth")
    return bad
