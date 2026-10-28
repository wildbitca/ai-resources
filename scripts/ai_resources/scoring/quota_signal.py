"""The provider quota-block signal: what Claude Code tells a hook when a turn ends on a rate limit or a billing cap.

Source (verified against Claude Code 2.1.310 and its hooks reference): the `StopFailure` hook event carries a structured
`error` field (`rate_limit`, `overloaded`, `billing_error`, `authentication_failed`, ...), an optional free-text
`error_details` (for example "429 Too Many Requests") and `last_assistant_message` (the rendered API error text). There is
NO structured reset time in that payload, so `resets_at` is read from the text only when the text states one; otherwise it is
unknown and the block is assumed to hold for `DEFAULT_TTL_S`, recorded as `reset_source: "assumed-ttl"`, never presented as a
measured reset.

The hook (hooks/kit_quota_signal.py) writes a small sidecar file; `pool_usage.read_quota_block` reads it. The signal is
positive-only: a block means the pool is exhausted now, and no block (or no file, or no hook installed) means "unknown", never
"there is headroom".

Sidecar format (`~/.cache/ai-resources/quota-blocks.json`, override `AI_RESOURCES_QUOTA_BLOCKS`):

    {"version": 1, "blocks": {"<pool id>": {"error": "rate_limit", "observed_at": 1790000000, "resets_at": 1790000900,
                                            "reset_source": "details|assumed-ttl", "details": "429 Too Many Requests",
                                            "source": "claude-code-stopfailure"}}}
"""
from __future__ import annotations

import json
import os
import re
from .. import scratch as _scratch
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

FORMAT_VERSION = 1
QUOTA_ERRORS = ("rate_limit", "billing_error")       # `overloaded` is capacity, not the operator's quota
DEFAULT_TTL_S = 15 * 60
MAX_TTL_S = 7 * 24 * 3600                              # a stated reset further away than this is not believed
DEFAULT_POOL = "claude-subscription"
SOURCE = "claude-code-stopfailure"
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\b")
_EPOCH = re.compile(r"resets?_at\"?\s*[:=]\s*\"?(\d{9,11})\b", re.I)
_IN = re.compile(r"(?:retry|try\s+again)[\w\s-]{0,12}?(?:in|after)\s*:?\s*(\d+)\s*(second|sec|s|minute|min|m|hour|hr|h)s?\b", re.I)
_UNIT = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600}


def default_path(home: str | Path | None = None) -> Path:
    env = os.environ.get("AI_RESOURCES_QUOTA_BLOCKS")
    if env:
        return Path(env)
    return (Path(home) if home else Path.home()) / ".cache" / "ai-resources" / "quota-blocks.json"


def parse_reset(text: str, now: int) -> int | None:
    """An epoch second at which the limit lifts, only when `text` states one in the future and within `MAX_TTL_S`."""
    found: list[int] = []
    for m in _EPOCH.finditer(text or ""):
        found.append(int(m.group(1)))
    for m in _ISO.finditer(text or ""):
        raw = m.group(1).replace(" ", "T")
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        found.append(int(when.timestamp()))
    m = _IN.search(text or "")
    if m:
        found.append(now + int(m.group(1)) * _UNIT[m.group(2).lower()])
    future = [t for t in found if now < t <= now + MAX_TTL_S]
    return min(future) if future else None


def block_from_payload(payload: Mapping[str, Any], now: int) -> dict[str, Any] | None:
    """The block a StopFailure payload describes, or None when it is not a quota error (or not a StopFailure)."""
    if not isinstance(payload, Mapping) or payload.get("hook_event_name") != "StopFailure":
        return None
    error = payload.get("error")
    if error not in QUOTA_ERRORS:
        return None
    details = payload.get("error_details")
    details = details if isinstance(details, str) else json.dumps(details) if details is not None else ""
    message = payload.get("last_assistant_message")
    text = f"{details}\n{message if isinstance(message, str) else ''}"
    resets = parse_reset(text, now)
    return {"error": error, "observed_at": now, "resets_at": resets if resets is not None else now + DEFAULT_TTL_S,
            "reset_source": "details" if resets is not None else "assumed-ttl", "details": details[:200], "source": SOURCE}


def load(path: str | Path) -> dict[str, dict[str, Any]]:
    """The blocks of the sidecar; a missing, unreadable or foreign-format file is no blocks."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    blocks = doc.get("blocks") if isinstance(doc, dict) and doc.get("version") == FORMAT_VERSION else None
    return {k: v for k, v in blocks.items() if isinstance(v, dict)} if isinstance(blocks, dict) else {}


def record(path: str | Path, pool_id: str, block: Mapping[str, Any], now: int) -> None:
    """Merge one block into the sidecar atomically, dropping blocks that already expired."""
    p = Path(path)
    blocks = {k: v for k, v in load(p).items() if isinstance(v.get("resets_at"), int) and v["resets_at"] > now}
    blocks[pool_id] = dict(block)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _scratch.atomic_tmp(dir=p.parent, prefix=".quota-blocks.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": FORMAT_VERSION, "blocks": blocks}, fh, indent=2)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def active_block(path: str | Path | None, pool_id: str, now: int) -> dict[str, Any] | None:
    """The unexpired block recorded for `pool_id`, else None."""
    if not path:
        return None
    block = load(path).get(pool_id)
    resets, seen = (block or {}).get("resets_at"), (block or {}).get("observed_at")
    if not block or isinstance(resets, bool) or not isinstance(resets, int) or not isinstance(seen, int) or seen > now + 60:
        return None
    return block if now < resets else None
