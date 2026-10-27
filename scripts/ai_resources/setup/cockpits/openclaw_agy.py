"""Antigravity (`agy-cli-lane`) provider plumbing for OpenClaw agents (ADR-0007 s11, ADR-0019).

A kit-role agent whose matrix primary is `agy:<slug>` runs `agy-cli-lane/<slug>`. OpenClaw refuses an unknown model, so the
config needs the catalog stub `models.providers.agy-cli-lane` listing the slugs. An agy-primary agent holds the same
tools as a Claude one (ADR-0019 O1): no agy-only deny. This module owns exactly the stub and nothing about which
agents exist: that is the routing matrix (`openclaw_roles`).
"""
from __future__ import annotations

from typing import Any, Mapping


STUB_PROVIDER = "agy-cli-lane"
STUB_BASE_URL = "http://127.0.0.1:9/"
STUB_API = "openai-completions"          # the schema value is not proven (isolated run pending): see ADR-0007 11
STUB_API_KEY = "kit-lane-agy-placeholder"      # a constant, never a credential


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def is_agy_ref(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith(f"{STUB_PROVIDER}/")


def stub_patch_for_slugs(slugs: list[str], doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `models.providers.agy-cli-lane` stub for these slugs (fill-only), or None when nothing is missing.

    ADR-0005 finding 1: `sessions_spawn` refuses `Unknown model` without a catalog entry. The key is the placeholder
    constant; an existing provider entry keeps every operator value and only gains missing models."""
    if not slugs:
        return None
    current = _get(doc, "models", "providers", STUB_PROVIDER)
    if not isinstance(current, Mapping):
        return {"baseUrl": STUB_BASE_URL, "api": STUB_API, "apiKey": STUB_API_KEY,
                "models": [{"id": s, "name": s} for s in sorted(set(slugs))]}
    have = [m for m in (current.get("models") or []) if isinstance(m, Mapping)]
    have_ids = {m.get("id") for m in have}
    missing = [s for s in sorted(set(slugs)) if s not in have_ids]
    if not missing:
        return None
    return {"models": have + [{"id": s, "name": s} for s in missing]}
