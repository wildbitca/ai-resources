"""Read-only headroom readers with injected fetchers.

No reader here has a default network client: the caller passes `fetch` (a callable) so the pure path
and the tests never touch the network. Every reader returns a `HeadroomReading` and never raises: an
exception, a timeout, an HTTP error or a missing field becomes `unknown` with the error class in `source`.

Readers run only for pools the operator opted into (`read_for_pool`), and never from detection.

What each pool can offer (usage research, 2026-10-13):
    claude-subscription  an EXISTING status-line sidecar JSON (`rate_limits.five_hour|seven_day`), Pro/Max only
    openrouter           GET /api/v1/key (`limit`, `limit_remaining`, `limit_reset`)
    vertex-claude        Cloud Quotas / Monitoring (the quota page usage may be inaccurate)
    anthropic-api, gemini-api, agy-*, codex-subscription, deepseek-api, openai-api: no reader (unknown)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from . import headroom as hr
from .headroom import HeadroomReading, UNKNOWN, KNOWN

Fetch = Callable[..., Any]

# Pools whose live state is unknown by construction, whatever a caller injects.
NO_READER_POOLS = ("anthropic-api", "gemini-api", "agy-gemini", "agy-claude-gpt", "codex-subscription",
                   "deepseek-api", "openai-api")


def _unknown(pool_id: str, why: str) -> HeadroomReading:
    return hr.unknown_reading(pool_id, source=why)


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _safe(pool_id: str, fn: Callable[[], HeadroomReading]) -> HeadroomReading:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a reader must never raise to the wizard
        return _unknown(pool_id, f"error:{type(e).__name__}")


def read_claude_sidecar(path: str | Path | None, now_epoch: int | None = None,
                        pool_id: str = "claude-subscription") -> HeadroomReading:
    """Parse a cached status-line JSON. A missing file, a missing window or a non-Pro/Max session is unknown.

    The kit never writes this file and never edits the operator's status line: it only reads one that exists."""
    def go() -> HeadroomReading:
        if not path:
            return _unknown(pool_id, "statusline-sidecar:no-path")
        p = Path(path)
        if not p.is_file():
            return _unknown(pool_id, "statusline-sidecar:absent")
        doc = json.loads(p.read_text(encoding="utf-8"))
        limits = doc.get("rate_limits") if isinstance(doc, dict) else None
        if not isinstance(limits, dict):
            return _unknown(pool_id, "statusline-sidecar:no-rate-limits")
        best: tuple[float, int | None] | None = None
        for window in ("five_hour", "seven_day"):
            w = limits.get(window)
            used = _num(w.get("used_percentage")) if isinstance(w, dict) else None
            if used is None:
                continue
            resets = w.get("resets_at")
            resets = int(resets) if isinstance(resets, (int, float)) and not isinstance(resets, bool) else None
            if best is None or used > best[0]:          # the tighter window wins
                best = (used, resets)
        if best is None:
            return _unknown(pool_id, "statusline-sidecar:no-window")
        # The reading is as old as the file the status line last wrote, not as old as this call.
        written = int(p.stat().st_mtime)
        return HeadroomReading(pool_id=pool_id, state=KNOWN, used_fraction=min(max(best[0] / 100.0, 0.0), 1.0),
                               resets_at=best[1], source="statusline-sidecar", read_at=written)
    return _safe(pool_id, go)


def read_openrouter_key(fetch: Fetch | None, now_epoch: int | None = None,
                        pool_id: str = "openrouter") -> HeadroomReading:
    """`fetch()` returns the parsed JSON of GET /api/v1/key. A null `limit` is unlimited credit: unknown."""
    def go() -> HeadroomReading:
        if fetch is None:
            return _unknown(pool_id, "openrouter-key:no-fetcher")
        doc = fetch()
        data = doc.get("data") if isinstance(doc, dict) and isinstance(doc.get("data"), dict) else doc
        if not isinstance(data, dict):
            return _unknown(pool_id, "openrouter-key:bad-response")
        limit, remaining = _num(data.get("limit")), _num(data.get("limit_remaining"))
        if limit is None or remaining is None or limit <= 0:
            return _unknown(pool_id, "openrouter-key:no-limit")
        used = min(max(1.0 - remaining / limit, 0.0), 1.0)
        return HeadroomReading(pool_id=pool_id, state=KNOWN, used_fraction=used, resets_at=None,
                               source="openrouter-key", read_at=now_epoch)
    return _safe(pool_id, go)


def read_vertex_quota(fetch: Fetch | None, project: str, base_model: str, now_epoch: int | None = None,
                      pool_id: str = "vertex-claude") -> HeadroomReading:
    """`fetch(project, base_model)` returns {"limit": n, "usage": m}. Google warns the usage may be inaccurate."""
    def go() -> HeadroomReading:
        if fetch is None or not project:
            return _unknown(pool_id, "vertex-quotas:no-fetcher")
        doc = fetch(project, base_model)
        limit, usage = (_num(doc.get("limit")), _num(doc.get("usage"))) if isinstance(doc, dict) else (None, None)
        if limit is None or usage is None or limit <= 0:
            return _unknown(pool_id, "vertex-quotas:no-data")
        return HeadroomReading(pool_id=pool_id, state=KNOWN, used_fraction=min(max(usage / limit, 0.0), 1.0),
                               source="vertex-quotas (usage may be inaccurate)", read_at=now_epoch)
    return _safe(pool_id, go)


def read_kit_counters(path: str | Path | None, pool_id: str, now_epoch: int | None = None) -> HeadroomReading:
    """Read `usage-counters.json` if the dispatch code ever writes it (no writer exists in Phase A).

    Shape: {"<pool id>": {"used": n, "limit": m}}. Absent or incomplete means unknown."""
    def go() -> HeadroomReading:
        if not path or not Path(path).is_file():
            return _unknown(pool_id, "kit-counters:absent")
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        entry = doc.get(pool_id) if isinstance(doc, dict) else None
        used, limit = (_num(entry.get("used")), _num(entry.get("limit"))) if isinstance(entry, dict) else (None, None)
        if used is None or limit is None or limit <= 0:
            return _unknown(pool_id, "kit-counters:incomplete")
        return HeadroomReading(pool_id=pool_id, state=KNOWN, used_fraction=min(max(used / limit, 0.0), 1.0),
                               source="kit-counters", read_at=now_epoch)
    return _safe(pool_id, go)


def read_for_pool(pool: Mapping[str, Any], opted_in: bool, deps: Mapping[str, Any] | None = None,
                  now_epoch: int | None = None) -> HeadroomReading:
    """The live reading for one pool; unknown unless the operator opted in and a reader exists.

    `deps` carries the injected inputs: `claude_sidecar_path`, `openrouter_fetch`, `vertex_fetch`,
    `vertex_project`, `vertex_base_model`, `kit_counters_path`."""
    pid = str(pool.get("id", ""))
    if not opted_in:
        return _unknown(pid, "not-requested")
    if pid in NO_READER_POOLS:
        return _unknown(pid, "no-reader")
    deps = deps or {}
    source = pool.get("headroom_source")
    if source == "statusline-sidecar":
        return read_claude_sidecar(deps.get("claude_sidecar_path"), now_epoch, pid)
    if source == "openrouter-key":
        return read_openrouter_key(deps.get("openrouter_fetch"), now_epoch, pid)
    if source == "vertex-quotas":
        return read_vertex_quota(deps.get("vertex_fetch"), str(deps.get("vertex_project", "")),
                                 str(deps.get("vertex_base_model", "anthropic-claude-opus")), now_epoch, pid)
    if source == "kit-counters":
        return read_kit_counters(deps.get("kit_counters_path"), pid, now_epoch)
    return _unknown(pid, "no-reader")
