"""Live per-pool usage for the four pools the routing matrix balances (ADR-0018). Read-only and tolerant.

The credit ledger `~/.config/ai-resources/credits.json` (credits.py: declared minus measured) wins when it holds a
declared amount for the pool. Max subscription and AI Pro quota come from the readers `scoring.headroom_sources` already owns (status-line sidecar,
quota block, kit counters). The two credit pools need a spend feed: `~/.cache/ai-resources/api-spend.json`, shape
`{"anthropic-api": {"used_usd": 12.3}, "gemini-api": {"used_usd": 1.2}}`, with the credit total from
`ANTHROPIC_API_CREDITS_USD` / `GOOGLE_API_CREDITS_USD` (ADR-0017 defaults 200 and 10). Without a feed the pool reads
`unknown`, never zero. Nothing here reads a key value, makes a network call or raises: a reader that fails is
`unknown` with the reason.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from .scoring import headroom_sources as hs
from .scoring import routing

CREDIT_ENV = {routing.POOL_ANTHROPIC_API: ("ANTHROPIC_API_CREDITS_USD", 200.0),
              routing.POOL_GEMINI_API: ("GOOGLE_API_CREDITS_USD", 10.0)}
KEY_ENV = {routing.POOL_ANTHROPIC_API: "ANTHROPIC_API_KEY", routing.POOL_GEMINI_API: "GEMINI_API_KEY"}
ALERT_AT = 0.80


def spend_path(home: str | Path | None = None) -> Path:
    override = os.environ.get("AI_RESOURCES_API_SPEND")
    if override:
        return Path(override)
    return (Path(home) if home else Path.home()) / ".cache" / "ai-resources" / "api-spend.json"


def _ledger_entry(pool: str) -> "dict[str, float] | None":
    """The declared credit and measured spend of the credit ledger (credits.py), when both are there."""
    try:
        from . import credits
        e = credits.pool_summary().get(pool) or {}
        declared, used = credits._usd(e.get("declared_usd")), credits._usd(e.get("measured_usd"))
    except Exception:  # noqa: BLE001 - a reader must never break the caller
        return None
    return {"declared_usd": declared, "measured_usd": used} if declared is not None and used is not None else None


def _credit(pool: str, env: Mapping[str, str], home: str | Path | None) -> dict[str, Any]:
    name, default = CREDIT_ENV[pool]
    try:
        total = float(env.get(name, default))
    except ValueError:
        total = default
    out: dict[str, Any] = {"used_fraction": None, "detail": f"credit ${total:,.2f}", "source": "none",
                           "total_usd": total}
    ledger = _ledger_entry(pool)
    if ledger is not None:
        declared, used = ledger["declared_usd"], ledger["measured_usd"]
        out.update(used_fraction=min(max(used / declared, 0.0), 1.0) if declared > 0 else None, source="credits.json",
                   total_usd=declared, detail=f"${used:,.2f} of ${declared:,.2f} (measured, estimate)")
        return out
    if total <= 0:
        out["detail"] = "no credit configured (0)"
        return out
    try:
        doc = json.loads(spend_path(home).read_text(encoding="utf-8"))
        used = float((doc.get(pool) or {}).get("used_usd"))
    except (OSError, ValueError, TypeError, AttributeError):
        out["detail"] += "; spend feed absent"
        return out
    out.update(used_fraction=min(max(used / total, 0.0), 1.0), source="api-spend.json",
               detail=f"${used:,.2f} of ${total:,.2f}")
    return out


def snapshot(now_epoch: int | None = None, env: Mapping[str, str] | None = None,
             home: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """`{pool: {label, kind, used_fraction|None, detail, source, key_present?}}` for the five routing pools."""
    env = os.environ if env is None else env
    now = int(time.time()) if now_epoch is None else now_epoch
    deps = hs.default_lane_deps(home)
    out: dict[str, dict[str, Any]] = {}
    for pool, meta in routing.POOLS.items():
        entry: dict[str, Any] = {"label": meta["label"], "kind": meta["kind"], "used_fraction": None,
                                 "detail": "unknown", "source": "none"}
        try:
            if pool in CREDIT_ENV:
                entry.update(_credit(pool, env, home))
                entry["key_present"] = bool(env.get(KEY_ENV[pool]))
            else:
                reading = (hs.read_claude_pool(deps, now, pool) if pool == routing.POOL_MAX
                           else hs.read_kit_counters(deps.get("kit_counters_path"), pool, now))
                if reading.state == hs.KNOWN and reading.used_fraction is not None:
                    entry.update(used_fraction=reading.used_fraction, source=reading.source,
                                 detail=f"{reading.used_fraction * 100:.0f}% used")
                else:
                    entry.update(source=reading.source, detail=f"unknown ({reading.source})")
        except Exception as e:  # noqa: BLE001 - a reader must never break the caller
            entry["detail"] = f"unknown ({e.__class__.__name__})"
        out[pool] = entry
    return out


def usage_map(snap: Mapping[str, Mapping[str, Any]]) -> dict[str, float | None]:
    return {p: v.get("used_fraction") for p, v in snap.items()}


def lines(snap: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Doctor/status rows: one per pool, then where each class of work would run right now."""
    out = []
    for pool, v in snap.items():
        frac = v.get("used_fraction")
        flag = ""
        if frac is not None and frac >= ALERT_AT:
            flag = "  <-- at or above 80%"
        key = ""
        if "key_present" in v:
            key = "; env key set" if v["key_present"] else "; no env key (the OpenClaw profile may hold it)"
        out.append(f"{pool:<20} {v['label']}: {v['detail']}{key}{flag}")
    use = usage_map(snap)
    for cls in sorted(routing.CLASS_POOLS):
        pool, why = routing.choose_pool(cls, use)
        out.append(f"route now {cls:<11} -> {pool} ({why})")
    return out
