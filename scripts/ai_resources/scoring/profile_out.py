"""Composition -> `ProfileState.customizations` (an overlay on `balanced-claude-55`).

The output goes through the existing `profiles.merge_customizations` and `profiles.to_executors`, both
unchanged. A role entry is `{"model": "<vendor>/<id>", "fallbacks": [same-vendor ids]}` and never carries
`backend` or `agentId` (ADR-0005). There is no user profile directory in v1.
"""
from __future__ import annotations

from typing import Any

from .compose import Composition
from .evidence import confidence


def to_customizations(comp: Composition) -> dict[str, dict[str, Any]]:
    """The overlay to store: empty when the engine kept the default or refused."""
    if comp.refused or comp.kept_default:
        return {}
    return {role: {"model": entry["model"], **({"fallbacks": list(entry["fallbacks"])} if entry.get("fallbacks") else {})}
            for role, entry in sorted(comp.by_role.items())}


def provenance(comp: Composition) -> dict[str, Any]:
    """What `ProfileState.composed` records: enough to explain the team later without recomputing it."""
    return {
        "mode": comp.mode, "shape": comp.shape, "input_digest": comp.input_digest,
        "dataset_version": comp.dataset_version, "as_of": comp.as_of, "allow_unverified": comp.allow_unverified,
        "kept_default": comp.kept_default, "recommendation": comp.recommendation,
        "labels": {a.role: a.label for a in comp.assignments},
        "evidence": {a.role: {"tag": "score-derived", "label": a.label, "confidence": confidence(a.label),
                              "effort": a.effort} for a in comp.assignments},
        "routes": {a.role: a.route for a in comp.assignments},
        "unverified_roles": sorted(a.role for a in comp.assignments if not a.verified),
        "headroom": {p.pool_id: {"state": p.state, "source": p.source, "basis": p.basis,
                                 "static_weight": p.static_weight} for p in comp.pools},
    }
