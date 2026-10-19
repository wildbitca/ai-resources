"""What the starting team really runs: provider mix, held and degraded Gemini, in pure functions.

ADR-0010 PD-1. Nothing here prompts, prints or touches the disk: the wizard, the doctor and the tests read the same
functions, so the held, degrade and next-action lines come from one place.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping


def _vendor(cfg: Mapping[str, Any]) -> str:
    model = str(cfg.get("model") or "")
    return model.split("/", 1)[0] if "/" in model else str(cfg.get("provider") or "")


def _enabled(providers: Mapping[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for pid, p in (providers or {}).items():
        on = p.get("enabled") if isinstance(p, Mapping) else getattr(p, "enabled", False)
        if on:
            out.add(pid)
    return out


def degrade_unrouted(executors: Mapping[str, Any], providers: Mapping[str, Any] | None) -> tuple[dict, list[str]]:
    """The executors with every `google/*` role that has no enabled Google provider moved onto its declared Claude
    fallback, plus the visible lines. A Google model left in a fallback list is dropped as well, because the LiteLLM
    renderer would register a `gemini/` deployment for it. A role with no Claude fallback is left as it is and named.

    Only meaningful on the legacy LiteLLM row: OpenRouter routes every vendor through one key."""
    out = copy.deepcopy(dict(executors))
    if "google" in _enabled(providers):
        return out, []
    moved: list[str] = []
    stuck: list[str] = []
    for role, cfg in (out.get("by_role") or {}).items():
        if not isinstance(cfg, dict):
            continue
        fallbacks = [str(f) for f in cfg.get("fallbacks") or []]
        if _vendor(cfg) == "google":
            claude = next((f for f in fallbacks if f.startswith("anthropic/")), None)
            if claude is None:
                stuck.append(role)
                continue
            cfg["model"] = claude
            fallbacks = [f for f in fallbacks if f != claude]
            moved.append(role)
        kept = [f for f in fallbacks if not f.startswith("google/")]
        if kept != fallbacks or "fallbacks" in cfg:
            cfg["fallbacks"] = kept
    defaults = out.get("defaults")
    if isinstance(defaults, dict) and defaults.get("fallbacks"):
        defaults["fallbacks"] = [f for f in defaults["fallbacks"] if not str(f).startswith("google/")]
    lines: list[str] = []
    if moved:
        lines.append("Gemini not active: Google is not enabled for this gateway; "
                     + ", ".join(moved) + " run on their Claude fallback")
    if stuck:
        lines.append("Google is not enabled for this gateway and has no Claude fallback for: " + ", ".join(stuck))
    return out, lines
