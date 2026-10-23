"""The deferred-write queue (ADR-0016 D6): setup writes nothing until Apply.

A step that used to write while it was being asked (hygiene, the agy MCP file, the CLAUDE.md migration, the operator rules)
now asks, builds an `Action` and enqueues it. Review lists the actions under "Will write", a dry run prints their plan text
and runs none of them, and Apply runs them in the order they were queued. Ctrl-C before Apply therefore leaves the disk as it
was. The only thing that stays immediate is a native sign-in handover, which is listed as "applies now" and never run again.

The queue lives on the in-memory state (`QUEUE_ATTR`), never in the saved file. An action has a `key`: queueing a second
action with the same key replaces the first, so a step asked again after Back never runs twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import ui

QUEUE_ATTR = "_deferred_writes"


@dataclass
class Action:
    key: str                                  # one action per key: queueing again replaces
    label: str                                # one line for Review > Will write
    plan_text: str = ""                       # what a dry run prints ("" = the label)
    run: Callable[[], Any] = lambda: None     # the write; executed once, at Apply
    applies_now: bool = False                 # already happened (a native handover): listed, never run again
    details: list[str] = field(default_factory=list)

    def text(self) -> str:
        return self.plan_text or self.label


def queue(s: Any) -> list[Action]:
    cur = getattr(s, QUEUE_ATTR, None)
    if cur is None:
        cur = []
        setattr(s, QUEUE_ATTR, cur)
    return cur


def enqueue(s: Any, action: Action) -> None:
    q = queue(s)
    for i, existing in enumerate(q):
        if existing.key == action.key:
            q[i] = action
            return
    q.append(action)


def discard(s: Any, key: str) -> None:
    q = queue(s)
    q[:] = [a for a in q if a.key != key]


def pending(s: Any) -> list[Action]:
    return [a for a in queue(s) if not a.applies_now]


def review_labels(s: Any) -> list[str]:
    """Lines for Review: what Apply will write, then what already happened (labelled "applies now")."""
    out = [a.label for a in pending(s)]
    out += [f"{a.label} (applies now, already done)" for a in queue(s) if a.applies_now]
    return out


def dry_run_lines(s: Any) -> list[str]:
    return [f"would run: {a.text()}" for a in pending(s)]


def run_all(s: Any, *, say: Callable[[str], None] = lambda m: None,
            warn: Callable[[str], None] = lambda m: None) -> list[tuple[str, bool]]:
    """Run every pending action in queue order. An action that raises (other than an interrupt) is reported and the next one
    still runs; its failure is the action's own. Returns [(key, ok)]. The queue is emptied: each action runs once."""
    results: list[tuple[str, bool]] = []
    todo = pending(s)
    setattr(s, QUEUE_ATTR, [])
    for a in todo:
        try:
            a.run()
            results.append((a.key, True))
        except (KeyboardInterrupt, SystemExit, ui.Cancelled, ui.GoBack):
            raise
        except Exception as e:  # noqa: BLE001 - one failed writer must not stop the others
            warn(f"{a.label}: failed ({type(e).__name__}: {e}); nothing else was changed by it.")
            results.append((a.key, False))
    return results
