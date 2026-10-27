"""Dispatch ledger (ADR-0019 D7): one append-only JSON line per dispatch of a role, next to the run ledger.

`<state home>/ai-resources/dispatches.jsonl`, appended atomically under `flock` (`run_ledger.append_jsonl`). A row says which
role ran where (backend, model, family, pool), what it cost (tokens, seconds), how it ended (`ok|fail|blocked`; `pending`
marks a dispatch that has started or a route that was only recorded) and, when the role ran outside its primary, why
(`fallback_from` + `fallback_cause`, one of `routing.FALLBACK_CAUSES`). `writer_families` lists the model families that wrote
the run when the row was made, which is what a checker must be independent of.

This is NOT a second scorecard. `scorecard.py` and `run_ledger.py` keep sizing runs (LITE/STANDARD/FULL); they read the
per-run actuals from here through `actuals` (`ai-resources runs record --from-dispatches RUN`). The per-role/model
`scores/` stay fed by `models_cmd`. Tokens come from `run_ledger.measure` over the audit transcripts, or from the
backend's own result (`agy run`).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import roles_policy, run_ledger
from .scoring import routing

OUTCOMES = ("ok", "fail", "blocked", "pending")
_TEXT = ("run_id", "step", "role", "pool", "backend", "model", "family", "outcome", "fallback_from", "fallback_cause")


def ledger_path() -> Path:
    return run_ledger.ledger_path().parent / "dispatches.jsonl"


def problems(rec: dict[str, Any]) -> list[str]:
    out = [f"{k} must be text" for k in _TEXT if not isinstance(rec.get(k, ""), str)]
    if not rec.get("role"):
        out.append("role is required")
    if rec.get("outcome") not in OUTCOMES:
        out.append(f"outcome must be one of {OUTCOMES}")
    if rec.get("fallback_cause") and rec["fallback_cause"] not in (*routing.FALLBACK_CAUSES, "no-independent-checker"):
        out.append(f"fallback_cause must be one of {routing.FALLBACK_CAUSES}")
    if not isinstance(rec.get("writer_families", []), list):
        out.append("writer_families must be a list")
    for k in ("tokens_in", "tokens_out"):
        if not isinstance(rec.get(k, 0), int) or isinstance(rec.get(k, 0), bool):
            out.append(f"{k} must be an integer")
    if not isinstance(rec.get("duration_s", 0), (int, float)):
        out.append("duration_s must be a number")
    if rec.get("run_id") and not valid_run_id(rec["run_id"]):
        out.append("run_id must be letters, digits, '.', '_' or '-' (at most 80), with no '..'")
    return out


def valid_run_id(run_id: str) -> bool:
    """The engine's own run-id rule (`orchestrator.state.RUN_ID_RE`), so an id is safe in a path and a ledger row."""
    from .orchestrator import state
    return isinstance(run_id, str) and bool(state.RUN_ID_RE.match(run_id)) and ".." not in run_id


def record(role: str, outcome: str, *, run_id: str = "", step: str = "", cell: str = "", backend: str = "", model: str = "",
           pool: str = "", tokens_in: int = 0, tokens_out: int = 0, duration_s: float = 0.0, fallback_from: str = "",
           fallback_cause: str = "", writer_families: Iterable[str] = (), extra: dict[str, Any] | None = None,
           path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Append one dispatch row and return it. `cell` (`backend:model`) fills backend, model and pool; family comes from the model."""
    cell_backend, _, cell_model = cell.partition(":")
    backend, model = backend or cell_backend, model or cell_model
    rec: dict[str, Any] = {
        "ts": datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id, "step": step, "role": role, "pool": pool or routing.pool_of(f"{backend}:{model}"),
        "backend": backend, "model": model, "family": roles_policy.model_family(model) or (routing.UNKNOWN_FAMILY if model else ""),
        "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0), "duration_s": round(float(duration_s or 0), 2),
        "outcome": outcome, "fallback_from": fallback_from, "fallback_cause": fallback_cause or "",
        "writer_families": sorted({str(f) for f in writer_families if f}), **(extra or {})}
    bad = problems(rec)
    if bad:
        raise ValueError("; ".join(bad))
    run_ledger.append_jsonl(path or ledger_path(), rec)
    return rec


def _ts(row: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(row["ts"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def load(run_id: str = "", since: float = 0.0, path: Path | None = None, skipped: list[int] | None = None) -> list[dict[str, Any]]:
    """Rows in file order, optionally of one run and not older than `since` (epoch seconds). A torn or malformed row is
    skipped, never fatal and never hiding the others; `skipped` (a one-item list) receives how many were skipped."""
    try:
        lines = run_ledger.read_lines(path or ledger_path())
    except OSError:
        return []
    rows, bad = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(row, dict) or problems(row) or _ts(row) is None:
            bad += 1
            continue
        if (run_id and row.get("run_id") != run_id) or (since and _ts(row) < since):
            continue
        rows.append(row)
    if skipped is not None:
        skipped[:] = [bad]
    return rows


def writer_families(run_id: str, path: Path | None = None) -> list[str]:
    """The model families that wrote this run: rows of writer roles that started or finished fine, and a row that moved to a
    fallback of unknown model (`family_candidates`: every family of the role's chain may have written)."""
    table = routing.merged()
    rows = [r for r in load(run_id, path=path) if (r["outcome"] in ("ok", "pending") or r.get("family_candidates"))
            and (table[r["role"]].writes if r["role"] in table else r["role"] in roles_policy.SCOPE_ROLES)]
    return sorted({f for r in rows for f in [r["family"], *(r.get("family_candidates") or [])] if f})


def run_writers(run_id: str, role: str, path: Path | None = None) -> list[str]:
    """The writer families a `role` must be independent of in `run_id`, as the ledger records them. A checker of an engine run
    that has a write step but no recorded writer row gets the `unknown` family (fail closed: it cannot show independence)."""
    families = writer_families(run_id, path=path)
    table = routing.merged()
    if families or role not in table or not table[role].checker:
        return families
    from .orchestrator import workflow_loader
    from .orchestrator.state import RunStore, StateError
    try:
        steps = RunStore().load(run_id).get("workflow_steps") or []
    except (StateError, OSError, ValueError):
        return families                          # an ad-hoc run id: nothing says a writer exists
    if any(d.get("role") in workflow_loader.WRITER_ROLES or d.get("role") in roles_policy.SCOPE_ROLES for d in steps):
        return [routing.UNKNOWN_FAMILY]
    return families


def actuals(run_id: str, path: Path | None = None) -> dict[str, Any]:
    """Per-run actuals for `runs record --from-dispatches`: finished dispatches only (a `pending` row is not an outcome)."""
    done = [r for r in load(run_id, path=path) if r["outcome"] != "pending"]
    if not done:
        return {}
    return {"steps": len({r["step"] or r["role"] for r in done if r["outcome"] == "ok"}),
            "agents": len({(r["role"], r["model"]) for r in done}), "input": sum(r["tokens_in"] for r in done),
            "output": sum(r["tokens_out"] for r in done), "calls": len(done),
            "measured": "dispatch ledger", "rework": sum(1 for r in done if r["outcome"] != "ok")}


def summarize(rows: Iterable[dict[str, Any]], profile_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """ACTUAL against the MATRIX (ADR-0019 D7), the one aggregation behind `doctor` and `dispatch list --summary`.

    Per pool and per role: dispatches, tokens, fallbacks. A deviation is a dispatch whose `backend:model` is neither the
    role's primary nor a rung of the checker ladder (a checker has no fixed primary) and that carries no
    `fallback_cause`: that is a role that ran outside the matrix for no recorded reason. A spill WITH a cause is fine.
    Blocked dispatches ran nowhere and are not counted."""
    table = routing.merged(profile_doc)
    pools: dict[str, dict[str, int]] = {}
    roles: dict[str, dict[str, int]] = {}
    causes: dict[str, int] = {}
    deviations: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("backend"):
            continue
        cell, tokens, cause = f"{r['backend']}:{r['model']}", r["tokens_in"] + r["tokens_out"], r["fallback_cause"]
        role = table.get(r["role"])
        for bucket, key in ((pools, r["pool"] or "unknown"), (roles, r["role"])):
            b = bucket.setdefault(key, {"dispatches": 0, "tokens": 0, "fallbacks": 0})
            b["dispatches"] += 1
            b["tokens"] += tokens
            b["fallbacks"] += bool(cause)
        if cause:
            causes[cause] = causes.get(cause, 0) + 1
        elif role and cell != role.primary and not (role.checker and cell in routing.CHECKER_LADDER):
            deviations.append({"ts": r["ts"], "role": r["role"], "cell": cell, "primary": role.primary, "run_id": r["run_id"]})
    return {"dispatches": sum(p["dispatches"] for p in pools.values()), "pools": pools, "roles": roles,
            "causes": causes, "deviations": deviations}


def summary_lines(sm: dict[str, Any], top: int = 5) -> list[str]:
    """One line per pool, the busiest roles, the fallback causes. Deviations are printed by the caller (they are alerts)."""
    if not sm["dispatches"]:
        return ["no dispatches recorded in this window"]
    out = [f"{p}: {v['dispatches']} dispatches, {v['tokens']:,} tokens, {v['fallbacks']} fallbacks" for p, v in sorted(sm["pools"].items())]
    busiest = sorted(sm["roles"].items(), key=lambda kv: (-kv[1]["tokens"], -kv[1]["dispatches"], kv[0]))[:top]
    out.append("top roles: " + ", ".join(f"{r} {v['dispatches']}x/{v['tokens']:,}t" for r, v in busiest))
    out.append("fallbacks by cause: " + (", ".join(f"{c} {n}" for c, n in sorted(sm["causes"].items())) or "none"))
    return out


def tokens_between(start: float, end: float, sessions: Iterable[str] = ()) -> tuple[int, int]:
    """(input, output) tokens of the audit transcripts in the window, through the run ledger's own measurement."""
    from . import audit_snapshot
    usages, _ = audit_snapshot.collect(datetime.fromtimestamp(start, timezone.utc) - timedelta(minutes=1),
                                       datetime.fromtimestamp(end, timezone.utc) + timedelta(minutes=1))
    m = run_ledger.measure(usages, start, end, sessions)
    return m["input"], m["output"]


# -- CLI ----------------------------------------------------------------------------------------------------------
def cmd_record(args: argparse.Namespace) -> int:
    t_in, t_out = args.tokens_in, args.tokens_out
    if args.measure_from and not (t_in or t_out):
        try:
            t_in, t_out = tokens_between(args.measure_from, args.measure_to or time.time(), args.session or ())
        except Exception as e:                  # measurement is best effort; the row still lands
            print(f"dispatch: tokens unavailable: {e}", file=sys.stderr)
    try:
        rec = record(args.role, args.outcome, run_id=args.run or "", step=args.step or "", cell=args.cell or "",
                     tokens_in=t_in, tokens_out=t_out, duration_s=args.duration, fallback_from=args.fallback_from or "",
                     fallback_cause=args.cause or "", writer_families=[w for w in (args.writers or "").split(",") if w])
    except ValueError as e:
        print(f"dispatch: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rec, sort_keys=True))
    return 0


def _since(text: str) -> float:
    """`2h`, `3d`, `45m` ago, or an ISO date/time, as epoch seconds."""
    if text[:-1].isdigit() and text[-1] in "mhd":
        return time.time() - int(text[:-1]) * {"m": 60, "h": 3600, "d": 86400}[text[-1]]
    when = datetime.fromisoformat(text)
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def cmd_list(args: argparse.Namespace) -> int:
    if args.run and not valid_run_id(args.run):
        print("dispatch: bad --run", file=sys.stderr)
        return 2
    try:
        rows = load(args.run or "", _since(args.since) if args.since else 0.0)
    except ValueError as e:
        print(f"dispatch: bad --since: {e}", file=sys.stderr)
        return 2
    if args.summary:
        sm = summarize(rows)
        print("\n".join(summary_lines(sm) + [f"DEVIATION {d['role']} ran {d['cell']} (primary {d['primary']}) with no fallback cause"
                                             for d in sm["deviations"]]))
        return 1 if sm["deviations"] else 0
    for r in rows[-args.last:] if args.last else rows:
        print(json.dumps(r, sort_keys=True))
    return 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dispatch", help="Dispatch ledger: one row per role dispatch (backend, model, tokens, outcome, fallback cause)")
    v = p.add_subparsers(dest="dispatch_command", required=True)
    r = v.add_parser("record", help="Append one dispatch row")
    r.add_argument("--role", required=True)
    r.add_argument("--outcome", required=True, choices=OUTCOMES)
    r.add_argument("--run")
    r.add_argument("--step")
    r.add_argument("--cell", help="backend:model the role ran on")
    r.add_argument("--tokens-in", type=int, default=0)
    r.add_argument("--tokens-out", type=int, default=0)
    r.add_argument("--duration", type=float, default=0.0, help="seconds")
    r.add_argument("--fallback-from", help="the cell the role should have run on")
    r.add_argument("--cause", help=f"fallback cause, one of {', '.join(routing.FALLBACK_CAUSES)}")
    r.add_argument("--writers", help="comma-separated model families that wrote the run")
    r.add_argument("--measure-from", type=float, help="epoch seconds: fill tokens from the audit transcripts since then")
    r.add_argument("--measure-to", type=float)
    r.add_argument("--session", action="append", help="transcript session id to price (repeatable)")
    r.set_defaults(func=cmd_record)
    li = v.add_parser("list", help="Print dispatch rows")
    li.add_argument("--run")
    li.add_argument("--since", help="2h | 3d | 45m | an ISO date")
    li.add_argument("--last", type=int, default=0, help="only the last N rows")
    li.add_argument("--summary", action="store_true", help="per pool/role actuals against the matrix; exit 1 on an uncaused deviation")
    li.set_defaults(func=cmd_list)
