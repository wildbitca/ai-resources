"""Gates between a scored candidate and a proposed assignment.

Two gates, in this order, then the routability check:

1. usage policy (`usage_gate`): the database marks a cockpit/engine/pool `dispatch-ok`, `direct-only` or
   `instructions-only`. `direct-only` (agy and the OpenClaw antigravity engine) is never a composition
   target and NO flag lifts it. `instructions-only` (the Codex cockpit) cannot dispatch a role either.
2. routability: the id the route would use must be in the kit's routing lists.
3. compat (`gate_from_compat`): the cockpit x provider x mode cell. It reads `setup/compat.py` read-only and
   never changes its semantics; the grade is coarse (DECLARED / UNVERIFIED) because the matrix cannot prove
   runtime behaviour. The free-text `source` note is not parsed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..setup import compat
from .db import ScoresDB

DECLARED = "DECLARED"
UNVERIFIED = "UNVERIFIED"
BLOCKED = "BLOCKED"

OK = "ok"
KIND_USAGE = "usage"
KIND_NOT_ROUTABLE = "not-routable"
KIND_COMPAT_SKIP = "compat-skip"
KIND_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    verified: bool
    grade: str
    reason: str = ""
    kind: str = OK


def usage_gate(db: ScoresDB, surface: str, pool_id: str = "") -> GateVerdict:
    """`direct-only` and `instructions-only` surfaces (or pools) are rejected with the cited reason."""
    checks = []
    if surface:
        checks.append(("surface " + surface, db.surfaces.get(surface) or {}))
    pool = db.pool(pool_id) if pool_id else None
    if pool is not None:
        checks.append(("pool " + pool_id, {"usage": pool.get("usage"), "reason": "; ".join(pool.get("facts") or ()),
                                           "source_ref": pool.get("source_ref", "")}))
    for what, spec in checks:
        usage = spec.get("usage", "dispatch-ok")
        if usage != "dispatch-ok":
            cite = f" ({spec.get('source_ref')})" if spec.get("source_ref") else ""
            return GateVerdict(False, False, BLOCKED, f"{usage}: {spec.get('reason', what)}{cite}", KIND_USAGE)
    return GateVerdict(True, True, DECLARED, "", OK)


def gate_from_compat(cockpit: str, engine: str | None, provider: str, mode: str,
                     allow_unverified: bool = False) -> GateVerdict:
    """The compat cell for (cockpit[, engine], provider, mode key). Read-only."""
    try:
        if cockpit == "openclaw" and engine:
            cell = compat.ENGINE_CELLS[(engine, provider)]
        else:
            cell = compat.cell(cockpit, provider, mode)
    except KeyError:
        return GateVerdict(False, False, BLOCKED, f"no compat cell for {cockpit} x {provider} x {mode}",
                           KIND_COMPAT_SKIP)
    if cell.action not in (compat.CONFIGURE, compat.VIA_GATEWAY):
        # skip, or instructions only: the cell writes no model setting, so it cannot serve a role.
        return GateVerdict(False, False, BLOCKED,
                           cell.reason or f"{cell.action} in the compat matrix", KIND_COMPAT_SKIP)
    if not cell.verified:
        if allow_unverified:
            return GateVerdict(True, False, UNVERIFIED, cell.reason or compat.UNVERIFIED, KIND_UNVERIFIED)
        return GateVerdict(False, False, UNVERIFIED, cell.reason or compat.UNVERIFIED, KIND_UNVERIFIED)
    return GateVerdict(True, True, DECLARED, "", OK)


def make_gate(db: ScoresDB, *, cockpit: str = "claude", mode_key: str = compat.MULTI_LITELLM,
              allow_unverified: bool = False, enabled_pools=()) -> Callable[[str, str], GateVerdict]:
    """The composite gate `compose` receives: (model, route) -> GateVerdict."""
    def gate(model: str, route: str) -> GateVerdict:
        vendor = db.vendor_of(model)
        pool_id = db.pool_for(vendor, route, enabled_pools)
        v = usage_gate(db, db.surface_of(route), pool_id)
        if not v.allowed:
            return v
        if not db.routable(model, route):
            return GateVerdict(False, False, BLOCKED, "not routable by kit", KIND_NOT_ROUTABLE)
        want = (db.routes.get(route) or {}).get("mode_key")
        if want and mode_key in (compat.MULTI_LITELLM, compat.MULTI_OPENROUTER) and want != mode_key:
            return GateVerdict(False, False, BLOCKED, f"route {route} needs the {want} gateway, setup uses {mode_key}",
                               KIND_COMPAT_SKIP)
        return gate_from_compat(cockpit, None, db.compat_provider(model, route), mode_key, allow_unverified)
    return gate
