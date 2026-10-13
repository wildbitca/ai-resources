"""Headroom model: billing-class pools, a first-class `unknown` state and the static-weight fallback.

Pools are keyed by billing class (subscription, metered-api, gateway-metered). A pool's weight is the static
weight the operator chose in setup (ample 1.0, normal 0.7, scarce 0.3; default 1.0). A live reading may only
scale that weight down by the used fraction; it can change which band member is picked, never the band.
When the reading is `unknown` or `stale` the static weight is used, and the why table says so.

Pure: no I/O and no clock (the caller passes `now_epoch`).
"""
from __future__ import annotations

from dataclasses import dataclass

KNOWN = "known"
STALE = "stale"
UNKNOWN = "unknown"
STATES = (KNOWN, STALE, UNKNOWN)

LEVELS = {"ample": 1.0, "normal": 0.7, "scarce": 0.3}
DEFAULT_WEIGHT = 1.0
EPSILON = 0.05
DEFAULT_MAX_AGE_S = 5 * 3600           # the shortest window any reader reports (Claude 5h)


@dataclass(frozen=True)
class Pool:
    id: str
    billing_class: str
    static_weight: float = DEFAULT_WEIGHT
    source: str = "none"


@dataclass(frozen=True)
class HeadroomReading:
    pool_id: str
    state: str = UNKNOWN                 # known | stale | unknown
    used_fraction: float | None = None   # 0.0 .. 1.0
    resets_at: int | None = None         # epoch seconds
    source: str = "none"
    as_of: str = ""                      # ISO date of the read, for the why table
    read_at: int | None = None           # epoch seconds of the read


@dataclass(frozen=True)
class EffectiveWeight:
    weight: float
    basis: str                           # "static" | "live"
    note: str = ""


def unknown_reading(pool_id: str, source: str = "none") -> HeadroomReading:
    return HeadroomReading(pool_id=pool_id, state=UNKNOWN, source=source)


def level_weight(level: str | None) -> float:
    return LEVELS.get(level or "", DEFAULT_WEIGHT)


def effective_weight(static_weight: float, reading: HeadroomReading | None, now_epoch: int | None = None,
                     max_age_s: int = DEFAULT_MAX_AGE_S) -> EffectiveWeight:
    """The weight the engine balances with.

    known  -> static x (1 - used_fraction), floored at EPSILON
    stale  -> static (the reading is older than its window, or past `resets_at`)
    unknown (or no reading) -> static
    """
    static = static_weight if static_weight and static_weight > 0 else DEFAULT_WEIGHT
    if reading is None or reading.state == UNKNOWN or reading.used_fraction is None:
        return EffectiveWeight(static, "static", "headroom unknown; static weight")
    state = reading.state
    if state == KNOWN and now_epoch is not None:
        if reading.resets_at is not None and now_epoch >= reading.resets_at:
            state = STALE
        elif reading.read_at is not None and now_epoch - reading.read_at > max_age_s:
            state = STALE
    if state != KNOWN:
        return EffectiveWeight(static, "static", "headroom reading stale; static weight")
    used = min(max(float(reading.used_fraction), 0.0), 1.0)
    return EffectiveWeight(max(static * (1.0 - used), EPSILON), "live", f"{used * 100:.0f}% used via {reading.source}")


def pressure(load_p: float, role_load: float, weight: float) -> float:
    """Projected pressure of putting `role_load` on a pool that already carries `load_p`."""
    return (load_p + role_load) / (weight if weight > 0 else EPSILON)


def agy_reporting_pool(model_id: str) -> str:
    """Antigravity's own pool rule, reused by import (reporting only: agy is never a composition target)."""
    from ..setup.cockpits import _agy_quota
    return _agy_quota.pool_for_model(model_id)
