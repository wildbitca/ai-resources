"""What the operator's OpenClaw is configured to run RIGHT NOW (ADR-0016 D2).

Read-only over an already parsed `openclaw.json` document (`openclaw.read_config`); no gateway call (ADR-0003). The saved
setup state never overrides what this module reports: it fills only where the live config says nothing.
"""
from __future__ import annotations

from typing import Any, Mapping

ANTHROPIC_PREFIXES = ("anthropic/", "claude-cli/")
AGY_PREFIX = "agy-cli/"


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _primary(model: Any) -> str:
    if isinstance(model, str):
        return model
    if isinstance(model, dict) and isinstance(model.get("primary"), str):
        return model["primary"]
    return ""


def _fallbacks(model: Any) -> list[str]:
    if isinstance(model, dict) and isinstance(model.get("fallbacks"), list):
        return [f for f in model["fallbacks"] if isinstance(f, str)]
    return []


def runtime_of(doc: Mapping, ref: str) -> str:
    """`agents.defaults.models.<ref>.agentRuntime.id`, falling back to the provider wildcard (`anthropic/*`)."""
    models = _get(doc, "agents", "defaults", "models")
    if not isinstance(models, dict) or not ref:
        return ""
    for key in (ref, ref.split("/", 1)[0] + "/*"):
        rid = _get(models.get(key), "agentRuntime", "id")
        if isinstance(rid, str) and rid:
            return rid
    return ""


def _entries(doc: Mapping) -> dict[str, dict]:
    """agent id -> entry. `agents.entries` is a dict keyed by id on current OpenClaw; an `agents.list` of `{id, ...}` is
    tolerated for older documents."""
    out: dict[str, dict] = {}
    agents = _get(doc, "agents")
    ent = agents.get("entries") if isinstance(agents, dict) else None
    if isinstance(ent, dict):
        for aid, e in ent.items():
            if isinstance(aid, str):
                out[aid] = e if isinstance(e, dict) else {}
    lst = agents.get("list") if isinstance(agents, dict) else None
    if isinstance(lst, list):
        for e in lst:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                out.setdefault(e["id"], e)
    return out


def live_agents(doc: Mapping) -> dict[str, tuple[str, str]]:
    """agent id -> (primary model, runtime id) as configured now. An agent with no model of its own reports the
    defaults' primary (that is what it runs). `defaults` is reported under the id "" when the document names a default
    model, so a caller can tell an unconfigured install (empty result) from a configured one."""
    default_model = _get(doc, "agents", "defaults", "model")
    default_primary = _primary(default_model)
    out: dict[str, tuple[str, str]] = {}
    if default_primary:
        out[""] = (default_primary, runtime_of(doc, default_primary))
    for aid, entry in _entries(doc).items():
        primary = _primary(entry.get("model")) or default_primary
        if primary:
            out[aid] = (primary, runtime_of(doc, primary))
    return out


def live_fallbacks(doc: Mapping) -> list[str]:
    return _fallbacks(_get(doc, "agents", "defaults", "model"))


def has_live_agents(doc: Mapping) -> bool:
    return bool(live_agents(doc))


def live_default(doc: Mapping) -> tuple[str, str]:
    """(primary, runtime) of the orchestrator: the `main` agent when it names one, else the defaults."""
    agents = live_agents(doc)
    return agents.get("main") or agents.get("") or ("", "")


def provider_of(ref: str) -> str:
    return ref.split("/", 1)[0] if "/" in ref else ""


def is_anthropic(ref: str) -> bool:
    return ref.startswith(ANTHROPIC_PREFIXES) or ref.startswith("claude-")


def is_agy(ref: str) -> bool:
    return ref.startswith(AGY_PREFIX)
