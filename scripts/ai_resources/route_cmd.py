"""`ai-resources route <role>`: where a role runs right now, resolved through the routing matrix (ADR-0019).

Reads live pool usage (or a `--usage-file` JSON `{pool: fraction}`), applies the matrix, and prints the route with the
cause whenever it is not the role's primary. `--run RUN_ID` adds the writer families the dispatch ledger recorded for that
run (a checker takes the cheapest adequate rung of another family); `--record --step S` appends the decision to the ledger. Exit 0 routed, 2 bad input, 3 blocked (no independent checker or no rung).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import dispatch_ledger, pool_usage
from .scoring import routing
from .setup import detection


def cmd_route(args: argparse.Namespace) -> int:
    try:
        if args.usage_file:
            usage = {p: (None if v is None else float(v)) for p, v in json.loads(Path(args.usage_file).read_text("utf-8")).items()}
        else:
            if getattr(args, "refresh", False):
                pool_usage.sync_caches()
            usage = pool_usage.usage_map(pool_usage.snapshot())
        writers = [w for w in (args.writers or "").split(",") if w.strip()]
        if args.run:
            writers += dispatch_ledger.writer_families(args.run)
        # an optional backend (agy, codex) that is not installed and signed in is skipped with its recorded cause
        unavailable = {**detection.inactive_backends(), **dict(u.split("=", 1) for u in args.unavailable or [])}
        route = routing.resolve(args.role, usage, writers, unavailable)
    except (OSError, ValueError, routing.RoutingError) as e:
        print(f"route: {e}", file=sys.stderr)
        return 2
    if args.record:
        primary = routing.merged().get(route.role)
        dispatch_ledger.record(
            route.role, "blocked" if route.blocked else "pending", run_id=args.run or "", step=args.step or "",
            cell=route.cell or "", fallback_from=(primary.primary or "") if route.cause and primary else "",
            fallback_cause=route.cause or "", writer_families=[routing.model_family(w) or w for w in writers],
            extra={"reason": route.reason[:300]} if route.blocked else None)
    doc = {"role": route.role, "cell": route.cell, "backend": route.backend, "model": route.model, "pool": route.pool,
           "fallbacks": list(route.fallbacks), "reason": route.reason, "cause": route.cause, "blocked": route.blocked}
    if args.json:
        print(json.dumps(doc, indent=2))
    elif route.blocked:
        print(f"{route.role}: BLOCKED - {route.reason}")
    else:
        tail = f"  (fallback, cause: {route.cause})" if route.cause else ""
        print(f"{route.role}: {route.cell} [{route.pool}]{tail}")
        if route.fallbacks:
            print("  then: " + " -> ".join(route.fallbacks))
    return 3 if route.blocked else 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("route", help="Resolve where a role runs now through the routing matrix")
    p.add_argument("role", help="Role name (see `routing.ROLES`)")
    p.add_argument("--run", metavar="RUN_ID", help="read the run's writer families from the dispatch ledger")
    p.add_argument("--record", action="store_true", help="append the decision to the dispatch ledger (outcome pending or blocked)")
    p.add_argument("--step", help="workflow step id, recorded with --record")
    p.add_argument("--writers", help="Comma-separated model refs/families of the run's writers (checkers avoid them)")
    p.add_argument("--usage-file", metavar="FILE", help="JSON {pool: used fraction}; default: live pool usage")
    p.add_argument("--refresh", action="store_true",
                   help="Refresh the Max and AI Pro usage first (`claude -p /usage`, `agy -p /usage`; 0 tokens)")
    p.add_argument("--unavailable", action="append", metavar="BACKEND_OR_CELL=CAUSE",
                   help="Mark a backend or cell down with a cause (repeatable), e.g. agy=agy-missing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_route)
