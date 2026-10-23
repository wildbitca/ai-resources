"""Team and role limits of the orchestrator (ADR-0015 D3): per-role timeouts, `maxTeams` and the FIFO queue.

There is no spend cap (operator answer 4): the provider's own limits (API spend caps, rate limits, subscription quotas)
are the brake. What the orchestrator bounds is time (per-role timeouts) and concurrency (`maxTeams` teams at once, the
rest queued in arrival order).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

DEFAULT_ROLE_TIMEOUTS: dict[str, int] = {"planner": 1200, "implementer": 9000, "tester": 2700, "verifier": 1800}
DEFAULT_TIMEOUT = 1800
DEFAULT_MAX_TEAMS = 2
MIN_TIMEOUT, MAX_TIMEOUT = 30, 86400
# A run in one of these states holds a team slot; `queued` waits for one; the others have released theirs.
ACTIVE = frozenset({"running", "awaiting_approval"})
FINISHED = frozenset({"completed", "failed", "cancelled"})


def role_timeout(role: str, overrides: Mapping[str, Any] | None = None) -> int:
    """Seconds a step of `role` may run. An override outside [30 s, 24 h] or not a number is ignored."""
    raw = (overrides or {}).get(role)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and MIN_TIMEOUT <= raw <= MAX_TIMEOUT:
        return int(raw)
    return DEFAULT_ROLE_TIMEOUTS.get(role, DEFAULT_TIMEOUT)


def clamp_max_teams(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TEAMS
    return max(1, min(n, 16))


def active_count(runs: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for r in runs if r.get("status") in ACTIVE)


def queue_order(runs: Iterable[Mapping[str, Any]]) -> list[str]:
    """Queued run ids, oldest first (FIFO on the enqueue time, then on the id so the order is total)."""
    queued = [r for r in runs if r.get("status") == "queued"]
    queued.sort(key=lambda r: (r.get("queued_at") or r.get("created_at") or 0, str(r.get("id"))))
    return [str(r["id"]) for r in queued]


def free_slots(runs: Iterable[Mapping[str, Any]], max_teams: int) -> int:
    return max(0, clamp_max_teams(max_teams) - active_count(runs))


def admit(runs: Iterable[Mapping[str, Any]], max_teams: int) -> str:
    """`running` when a slot is free, else `queued`."""
    return "running" if free_slots(list(runs), max_teams) > 0 else "queued"


def next_to_promote(runs: list[Mapping[str, Any]], max_teams: int) -> list[str]:
    """The queued runs that fit in the free slots, in FIFO order."""
    return queue_order(runs)[:free_slots(runs, max_teams)]
