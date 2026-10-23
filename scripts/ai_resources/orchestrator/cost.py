"""Cost of one run, from the OpenClaw transcript events (ADR-0015 A6, ADR-0013).

There is no cap and nothing here pauses a run: this only REPORTS. The figure is an API-equivalent estimate priced with the
audit's own table (`audit_snapshot._cost`): it lags billing, and subscription lanes cost the operator quota, not dollars.
A run's calls are found by the session ids its steps reported plus the time window it was active, restricted to the
`kit-role-*` agents the engine dispatched to.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .. import audit_snapshot, audit_sources

LABEL = "USD API-equivalent estimate (it lags billing and does not count subscription quota)"


def _agents(run: Mapping[str, Any]) -> set[str]:
    return {str(s.get("agent")) for s in (run.get("steps") or {}).values() if s.get("agent")}


def _sessions(run: Mapping[str, Any]) -> dict[str, str]:
    return {str(s["session"]): str(sid) for sid, s in (run.get("steps") or {}).items() if s.get("session")}


def summarize(run: Mapping[str, Any], usages: Iterable[Any], price: Callable[[Any], float | None] | None = None
              ) -> dict[str, Any]:
    """Calls, tokens and USD of the run, in total and per role. Pure over already loaded usage records."""
    price = price or audit_snapshot._cost
    sessions = _sessions(run)
    agents = _agents(run)
    start = float(run.get("started_at") or run.get("created_at") or 0)
    end = float(run.get("finished_at") or run.get("updated_at") or 1e18)
    by_role: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "usd": 0.0,
                                                                "unpriced_calls": 0})
    steps = run.get("steps") or {}
    total = {"calls": 0, "input": 0, "output": 0, "usd": 0.0, "unpriced_calls": 0}
    for u in usages:
        ts = u.ts.timestamp()
        mine = u.session in sessions or (u.agent in agents and start <= ts <= end)
        if not mine:
            continue
        role = (steps.get(sessions.get(u.session, ""), {}) or {}).get("role") or u.agent.removeprefix("kit-role-")
        cost = price(u)
        for bucket in (by_role[role], total):
            bucket["calls"] += 1
            bucket["input"] += u.input
            bucket["output"] += u.output
            if cost is None:
                bucket["unpriced_calls"] += 1
            else:
                bucket["usd"] += cost
    for bucket in list(by_role.values()) + [total]:
        bucket["usd"] = round(bucket["usd"], 4)
    return {"run": run.get("id"), "label": LABEL, "total": total,
            "by_role": {k: by_role[k] for k in sorted(by_role)}}


def run_cost(run: Mapping[str, Any], agents_dir: Path, *, loader: Callable[..., Any] | None = None) -> dict[str, Any]:
    """`summarize` over the events in `agents_dir` (read-only; the loader opens each database with mode=ro)."""
    load = loader or audit_sources.load_openclaw_events
    since = datetime.fromtimestamp(float(run.get("started_at") or run.get("created_at") or 0), timezone.utc)
    usages, stats = load(agents_dir, since=since)
    out = summarize(run, usages)
    out["databases"] = getattr(stats, "databases", 0)
    out["missing"] = bool(getattr(stats, "missing", False))
    return out
