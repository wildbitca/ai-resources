"""Live per-pool usage for the four pools the routing matrix balances (ADR-0018). Read-only and tolerant.

The credit ledger `~/.config/ai-resources/credits.json` (credits.py: declared minus measured) wins when it holds a
declared amount for the pool. Max subscription and AI Pro quota come from the readers below (status-line sidecar,
quota block, kit counters; `sync_caches` refreshes the sidecar and the counters from `claude -p /usage` and `agy -p /usage`). The two credit pools need a spend feed: `~/.cache/ai-resources/api-spend.json`, shape
`{"anthropic-api": {"used_usd": 12.3}, "gemini-api": {"used_usd": 1.2}}`, with the credit total from
`ANTHROPIC_API_CREDITS_USD` / `GOOGLE_API_CREDITS_USD` (ADR-0017 defaults 200 and 10). Without a feed the pool reads
`unknown`, never zero. Nothing here reads a key value, makes a network call or raises: a reader that fails is
`unknown` with the reason.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

Reading = tuple["float | None", str]     # (used fraction, source or the reason it is unknown)

from .scoring import quota_signal, routing


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def default_deps(home: str | Path | None = None) -> dict[str, Path]:
    """Where local state may already hold a measured usage: the operator's status-line sidecar, the quota-block signal
    of the StopFailure hook and the kit counters file. Nothing here is created and nothing reaches the network."""
    base = Path(home) if home else Path.home()
    sidecar, counters = os.environ.get("AI_RESOURCES_CLAUDE_SIDECAR"), os.environ.get("AI_RESOURCES_USAGE_COUNTERS")
    return {"claude_sidecar_path": Path(sidecar) if sidecar else base / ".cache" / "ai-resources" / "claude-rate-limits.json",
            "quota_blocks_path": quota_signal.default_path(home),
            "kit_counters_path": Path(counters) if counters else base / ".cache" / "ai-resources" / "usage-counters.json"}


def _json(path: Path | None) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else None


def read_sidecar(path: Path | None) -> Reading:
    """A cached status-line JSON (`rate_limits.five_hour|seven_day.used_percentage`): the tighter window wins."""
    try:
        doc = _json(path)
        if doc is None:
            return None, "statusline-sidecar:absent"
        limits = doc.get("rate_limits") if isinstance(doc, dict) else None
        used = [u for w in ("five_hour", "seven_day")
                if isinstance(limits, dict) and isinstance(limits.get(w), dict)
                for u in [_num(limits[w].get("used_percentage"))] if u is not None]
        return (min(max(max(used) / 100.0, 0.0), 1.0), "statusline-sidecar") if used else (None, "statusline-sidecar:no-window")
    except Exception as e:  # noqa: BLE001 - a reader must never raise to its caller
        return None, f"error:{type(e).__name__}"


def read_quota_block(path: Path | None, pool: str, now: int) -> Reading:
    """A live quota block recorded by hooks/kit_quota_signal.py: the pool is exhausted until it lifts. Positive-only:
    no block is `unknown`, not headroom."""
    try:
        if not path or not Path(path).is_file():
            return None, "quota-block:signal-unavailable"
        block = quota_signal.active_block(path, pool, now)
        if block is None:
            return None, "quota-block:none-active"
        why = "details" if block.get("reset_source") == "details" else "assumed-ttl"
        return 1.0, f"quota-block:{block.get('error', 'unknown')}:{why}"
    except Exception as e:  # noqa: BLE001
        return None, f"error:{type(e).__name__}"


def read_counters(path: Path | None, pool: str) -> Reading:
    """`usage-counters.json`, shape `{"<pool>": {"used": n, "limit": m}}`; absent or incomplete is unknown."""
    try:
        entry = (_json(path) or {}).get(pool)
        used, limit = (_num(entry.get("used")), _num(entry.get("limit"))) if isinstance(entry, dict) else (None, None)
        if used is None or limit is None or limit <= 0:
            return None, "kit-counters:absent" if entry is None else "kit-counters:incomplete"
        return min(max(used / limit, 0.0), 1.0), "kit-counters"
    except Exception as e:  # noqa: BLE001
        return None, f"error:{type(e).__name__}"


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
    deps = default_deps(home)
    out: dict[str, dict[str, Any]] = {}
    for pool, meta in routing.POOLS.items():
        entry: dict[str, Any] = {"label": meta["label"], "kind": meta["kind"], "used_fraction": None,
                                 "detail": "unknown", "source": "none"}
        try:
            if pool in CREDIT_ENV:
                entry.update(_credit(pool, env, home))
                entry["key_present"] = bool(env.get(KEY_ENV[pool]))
            else:
                if pool == routing.POOL_MAX:        # a hit limit beats a percentage written earlier
                    used, source = read_quota_block(deps["quota_blocks_path"], pool, now)
                    if used is None:
                        used, source = read_sidecar(deps["claude_sidecar_path"])
                else:
                    used, source = read_counters(deps["kit_counters_path"], pool)
                if used is not None:
                    entry.update(used_fraction=used, source=source, detail=f"{used * 100:.0f}% used")
                else:
                    entry.update(source=source, detail=f"unknown ({source})")
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


def _probe(argv: list[str], timeout: int) -> dict[str, Any]:
    return json.loads(subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout or "{}")


def _write(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def sync_caches(home: str | Path | None = None, timeout: int = 15) -> dict[str, Any]:
    """Probe `claude -p /usage` and `agy -p /usage` locally (0 tokens) and write their window percentages into the
    sidecar and the counters file that `snapshot` reads. Never raises; returns what was updated."""
    now, paths = int(time.time()), default_deps(home)
    summary: dict[str, Any] = {"updated": [], "errors": {}}
    if shutil.which("claude"):
        try:
            text = str(_probe(["claude", "-p", "/usage", "--output-format", "json"], timeout).get("result") or "")
            windows = {"five_hour": (r"Current session:\s*(\d+(?:\.\d+)?)%\s*used", 5),
                       "seven_day": (r"Current week \(all models\):\s*(\d+(?:\.\d+)?)%\s*used", 48)}
            rl = {name: {"used_percentage": float(m.group(1)), "resets_at": now + hours * 3600}
                  for name, (rx, hours) in windows.items() for m in [re.search(rx, text)] if m}
            if rl:
                _write(paths["claude_sidecar_path"], {"capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                                                      "source": "claude-cli-usage", "rate_limits": rl})
                summary["updated"].append(routing.POOL_MAX)
        except Exception as e:  # noqa: BLE001
            summary["errors"]["claude"] = str(e)
    if shutil.which("agy"):
        try:
            groups = (((_probe(["agy", "--output-format", "json", "-p", "/usage"], timeout).get("command") or {})
                       .get("data") or {}).get("groups") or [])
            pools = {"Gemini Models": routing.POOL_AGY_GEMINI, "Gemini 4 Argon": routing.POOL_AGY_GEMINI,
                     "Claude and GPT models": routing.POOL_AGY_CLAUDE}
            counters: dict[str, Any] = {}
            for g in (g for g in groups if isinstance(g, dict) and g.get("name") in pools):
                fracs = [float(b["remaining_fraction"]) for b in g.get("buckets") or []
                         if isinstance(b, dict) and isinstance(b.get("remaining_fraction"), (int, float))]
                if fracs:       # one pool, several groups (Gemini and Argon share it): the tightest wins
                    used = round((1.0 - min(max(min(fracs), 0.0), 1.0)) * 100.0, 2)
                    held = counters.get(pools[g["name"]], {"used": 0.0})["used"]
                    counters[pools[g["name"]]] = {"used": max(used, held), "limit": 100.0}
            if counters:
                _write(paths["kit_counters_path"], counters)
                summary["updated"].extend(sorted(counters))
        except Exception as e:  # noqa: BLE001
            summary["errors"]["agy"] = str(e)
    return summary
