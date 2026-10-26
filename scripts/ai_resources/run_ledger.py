"""Workflow run ledger: measure every run, recommend a size from the data, report cost against quality.

One JSON line per finished run in `<state home>/ai-resources/run-ledger.jsonl` (never under a volatile root). Pure and
deterministic: no model, no network. The classifier only RECOMMENDS; a forced `inline|light|full` always wins, and a
threshold change is printed as a proposal the owner approves (nothing here edits a rule).

What can be captured without touching OpenClaw core (ADR-0013 rule c: say what is measured and what is not):
- `tokens`, `usd`, `calls`: from the same transcripts `ai-resources audit` reads, for the run's time window (and its
  session ids when known). API-equivalent estimate; the window also catches concurrent work, so it is an upper bound.
- `wall_s`: the run's own start and end.
- `agents`, `steps`, `rework`: reported by the agent that closes the run, or read from an orchestrator `state.json`
  (`--from-run`), where `returns` counts every send-back to an earlier step.
- not measurable today: tokens per step of an inline run (one session), per-spawn fixed prefix of a child that never
  reported a session id, and Engram usage (MCP only, no Python reader).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

MODES = ("inline", "light", "full")
OUTCOMES = ("pass", "gaps", "abandoned")
MIN_SAMPLES = 5            # similar runs needed before history may adjust the heuristic
REWORK_UNDER = 0.4         # share of runs of a mode with rework that marks the mode as too small for the bucket
OVERKILL_LINES = 100       # a full run that changed this many lines or fewer with one unit was probably oversized
LARGE_UNIT_LINES = 100
VOLATILE_ROOTS = ("/tmp", "/dev/shm", "/var/tmp")
SCHEMA = 1


# -- storage ------------------------------------------------------------------------------------------------------
def ledger_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    norm = os.path.realpath(base)
    for volatile in VOLATILE_ROOTS:
        if norm == volatile or norm.startswith(volatile + "/"):
            raise RuntimeError(f"the run ledger never lives under {volatile} (tmpfs, emptied on reboot): {norm}")
    return Path(base) / "ai-resources" / "run-ledger.jsonl"


def problems(rec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if rec.get("mode") not in MODES:
        out.append(f"mode must be one of {', '.join(MODES)}")
    if rec.get("outcome") not in OUTCOMES:
        out.append(f"outcome must be one of {', '.join(OUTCOMES)}")
    for k in ("agents", "steps", "rework"):
        if not isinstance(rec.get(k), int) or rec[k] < 0:
            out.append(f"{k} must be a non-negative integer")
    if rec.get("estimated") is not None and rec["estimated"] not in MODES:
        out.append("estimated must be one of the modes")
    return out


def append(rec: dict[str, Any], path: Path | None = None) -> Path:
    bad = problems(rec)
    if bad:
        raise ValueError("; ".join(bad))
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"schema": SCHEMA, **rec}, sort_keys=True, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return path


def load(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or ledger_path()
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue                      # a torn or hand-edited line never blocks the report
        if isinstance(row, dict) and not problems(row):
            rows.append(row)
    return rows


# -- measurement --------------------------------------------------------------------------------------------------
def measure(usages: Iterable[Any], since: float, until: float, sessions: Iterable[str] = (),
            price=None) -> dict[str, Any]:
    """Tokens, calls and API-equivalent USD of the calls inside [since, until]; with `sessions` (ids or id prefixes)
    only those sessions' calls inside the window count, which removes concurrent work from the figure."""
    if price is None:
        from . import audit_snapshot
        price = audit_snapshot._cost
    sess = set(sessions)
    out = {"calls": 0, "input": 0, "output": 0, "usd": 0.0, "unpriced_calls": 0}
    for u in usages:
        if not since <= u.ts.timestamp() <= until:
            continue
        if sess and not any(u.session.startswith(s) for s in sess):
            continue
        out["calls"] += 1
        out["input"] += u.input + u.cache_read + u.cache_write
        out["output"] += u.output
        c = price(u)
        if c is None:
            out["unpriced_calls"] += 1
        else:
            out["usd"] += c
    out["usd"] = round(out["usd"], 4)
    return out


def from_orchestrator(run: dict[str, Any]) -> dict[str, Any]:
    """The structural half of a record from an orchestrator state.json (steps, agents, rework, wall time)."""
    steps = run.get("steps") or {}
    done = [s for s in steps.values() if s.get("state") == "done"]
    start = float(run.get("started_at") or run.get("created_at") or 0)
    end = float(run.get("finished_at") or run.get("updated_at") or 0)
    status = run.get("status")
    return {
        "run": run.get("id"), "workflow": (run.get("handoff") or {}).get("workflow") or run.get("workflow"),
        "steps": len(done), "agents": len({s.get("agent") for s in done if s.get("agent")}),
        "rework": sum(int(v) for v in (run.get("returns") or {}).values()),
        "wall_s": max(0, round(end - start)) if start and end else None,
        "start": start or None, "end": end or None,
        "outcome": "pass" if status == "completed" else ("abandoned" if status in ("cancelled", "failed") else "gaps"),
        "source": "orchestrator",
    }


# -- classifier ---------------------------------------------------------------------------------------------------
def heuristic(features: dict[str, Any]) -> tuple[str, str]:
    """Mode and reason from U/S/R/F/N (skills/kit-orchestration "Task size"). Counts of files never decide alone."""
    units = int(features.get("units", 1) or 1)
    lines = int(features.get("lines", 0) or 0)
    risks = features.get("risks") or []
    separate = bool(features.get("separate_deliverables"))
    if features.get("ambiguous"):
        return "light", "open decision: map once, then re-evaluate"
    if separate or (units >= 2 and lines / max(units, 1) > LARGE_UNIT_LINES):
        return "full", "independent units over ~100 lines each, or separate deliverables"
    if risks or not features.get("resume_pass", True):
        return "light", "risk item or the resume test fails"
    if int(features.get("files", 0) or 0) > 10:
        return "light", "touches many files; check the resume test before going inline"
    return "inline", "understood, contained, resumable, one unit"


def _bucket(row: dict[str, Any]) -> tuple[str, bool]:
    return (row.get("kind") or "", bool(row.get("risks")))


def similar(history: list[dict[str, Any]], features: dict[str, Any]) -> list[dict[str, Any]]:
    key = (features.get("kind") or "", bool(features.get("risks")))
    return [r for r in history if _bucket(r) == key]


def _rework_rate(rows: list[dict[str, Any]]) -> float:
    return sum(1 for r in rows if r["rework"] > 0 or r["outcome"] != "pass") / len(rows) if rows else 0.0


def suggest(features: dict[str, Any], history: list[dict[str, Any]], force: str | None = None) -> dict[str, Any]:
    """Recommend a mode. With fewer than MIN_SAMPLES similar runs the heuristic rules; history only moves it one step
    when a mode has a clear record in this bucket. A forced mode is returned as is."""
    base, why = heuristic(features)
    if force:
        if force not in MODES:
            raise ValueError(f"force must be one of {', '.join(MODES)}")
        return {"mode": force, "basis": "forced", "heuristic": base, "n": 0, "reason": "the owner forced the mode",
                "recommendation": True}
    rows = similar(history, features)
    out = {"mode": base, "basis": "heuristic", "heuristic": base, "n": len(rows), "reason": why, "recommendation": True}
    at_base = [r for r in rows if r["mode"] == base]
    if len(at_base) >= MIN_SAMPLES and _rework_rate(at_base) >= REWORK_UNDER and base != "full":
        up = MODES[MODES.index(base) + 1]
        out.update(mode=up, basis="history",
                   reason=f"{base} needed rework in {_rework_rate(at_base):.0%} of {len(at_base)} similar runs")
        return out
    if base != "inline":
        down = MODES[MODES.index(base) - 1]
        lower = [r for r in rows if r["mode"] == down]
        if len(lower) >= MIN_SAMPLES and _rework_rate(lower) < REWORK_UNDER / 2:
            out.update(mode=down, basis="history",
                       reason=f"{down} passed with little rework in {len(lower)} similar runs")
    return out


def reassess(mode: str, signals: dict[str, Any]) -> dict[str, Any] | None:
    """Mid-run advice. Each signal moves the mode one step; there is no floor and no ceiling. None means keep going."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    step, reasons = 0, []
    if signals.get("verifier_fail"):
        step += 1
        reasons.append("the verifier found gaps")
    est, act = signals.get("files_estimated"), signals.get("files_actual")
    if est and act and act > 2 * est:
        step += 1
        reasons.append(f"{act} files touched against an estimate of {est}")
    if signals.get("sensitive_area") and mode == "inline":
        step += 1
        reasons.append("a risk item (sensitive area) appeared")
    if signals.get("trivial") and not step:
        step -= 1
        reasons.append("the task turned out trivial (one unit, a few lines, no risk)")
    if not step:
        return None
    i = max(0, min(len(MODES) - 1, MODES.index(mode) + step))
    if MODES[i] == mode:
        return None
    return {"from": mode, "to": MODES[i], "reason": "; ".join(reasons), "recommendation": True}


# -- calibration --------------------------------------------------------------------------------------------------
def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def calibrate(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost against quality per mode, with the sample size next to every number, plus proposals (never applied)."""
    modes: dict[str, dict[str, Any]] = {}
    for m in MODES:
        rows = [r for r in history if r["mode"] == m]
        tok = [r["input"] + r["output"] for r in rows if r.get("input") is not None and r.get("output") is not None]
        wall = [r["wall_s"] for r in rows if r.get("wall_s") is not None]
        modes[m] = {
            "n": len(rows), "agents_median": statistics.median([r["agents"] for r in rows]) if rows else None,
            "tokens_median": statistics.median(tok) if tok else None, "tokens_p80": _pct(tok, 0.8),
            "wall_s_median": statistics.median(wall) if wall else None,
            "rework_rate": round(_rework_rate(rows), 2) if rows else None,
            "pass_rate": round(sum(1 for r in rows if r["outcome"] == "pass") / len(rows), 2) if rows else None,
        }
    proposals: list[str] = []
    for m in ("inline", "light"):
        s = modes[m]
        if s["n"] >= MIN_SAMPLES and s["rework_rate"] >= REWORK_UNDER:
            proposals.append(f"{m}: {s['rework_rate']:.0%} of {s['n']} runs needed rework; propose widening the "
                             f"conditions that send a task one step up (owner approves)")
    over = [r for r in history if r["mode"] == "full" and r["rework"] == 0 and r["outcome"] == "pass"
            and r.get("lines") is not None and r["lines"] <= OVERKILL_LINES and (r.get("units") or 1) <= 1]
    if len(over) >= MIN_SAMPLES:
        proposals.append(f"full: {len(over)} full runs changed {OVERKILL_LINES} lines or fewer in one unit with no "
                         f"rework; propose sending that shape to light (owner approves)")
    short = [m for m in MODES if modes[m]["n"] < MIN_SAMPLES]
    notes = [f"{m}: n={modes[m]['n']}, below {MIN_SAMPLES}; no threshold proposal" for m in short]
    return {"total": len(history), "modes": modes, "proposals": proposals, "notes": notes}


def format_report(rep: dict[str, Any]) -> str:
    lines = [f"Run ledger calibration ({rep['total']} runs). Tokens are API-equivalent input+output of the run window.",
             f"{'mode':<7}{'n':>4}{'agents':>8}{'tok med':>11}{'tok p80':>11}{'wall s':>8}{'rework':>8}{'pass':>6}"]
    for m, s in rep["modes"].items():
        f = lambda v, w: f"{'-' if v is None else (int(v) if isinstance(v, float) and v == int(v) else v):>{w}}"
        lines.append(f"{m:<7}{s['n']:>4}{f(s['agents_median'], 8)}{f(s['tokens_median'], 11)}"
                     f"{f(s['tokens_p80'], 11)}{f(s['wall_s_median'], 8)}{f(s['rework_rate'], 8)}{f(s['pass_rate'], 6)}")
    lines += ["", "Proposals (nothing is applied; the owner approves any threshold change):"]
    lines += [f"- {p}" for p in rep["proposals"]] or ["- none"]
    lines += [f"- note: {n}" for n in rep["notes"]]
    return "\n".join(lines)


# -- CLI ----------------------------------------------------------------------------------------------------------
def _features(args: argparse.Namespace) -> dict[str, Any]:
    return {"kind": args.kind, "files": args.files, "lines": args.lines, "units": args.units,
            "risks": [r for r in (args.risk or "").split(",") if r], "ambiguous": args.ambiguous,
            "resume_pass": not args.resume_fail, "separate_deliverables": args.separate}


def cmd_suggest(args: argparse.Namespace) -> int:
    res = suggest(_features(args), load(), force=args.force)
    print(json.dumps(res, indent=1))
    return 0


def cmd_reassess(args: argparse.Namespace) -> int:
    res = reassess(args.mode, {"verifier_fail": args.verifier_fail, "files_estimated": args.files_estimated,
                               "files_actual": args.files_actual, "sensitive_area": args.sensitive,
                               "trivial": args.trivial})
    print(json.dumps(res or {"keep": args.mode, "recommendation": True}, indent=1))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    rec: dict[str, Any] = {}
    if args.from_run:
        from .orchestrator.state import RunStore
        rec.update(from_orchestrator(RunStore().load(args.from_run)))
    now = time.time()
    start = args.start or rec.get("start")
    end = args.end or rec.get("end") or now
    over = {"mode": args.mode, "estimated": args.estimated, "kind": args.kind, "forced": args.forced,
            "agents": args.agents, "steps": args.steps, "rework": args.rework, "outcome": args.outcome,
            "lines": args.lines, "units": args.units, "files": args.files,
            "risks": [r for r in (args.risk or "").split(",") if r] or None,
            "escalations": [e for e in (args.escalations or "").split(",") if e] or None, "run": args.run}
    rec.update({k: v for k, v in over.items() if v is not None})
    rec.setdefault("risks", [])
    rec.setdefault("escalations", [])
    rec.setdefault("forced", False)
    rec.setdefault("source", "manual")
    rec["ts"] = datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="seconds")
    if start:
        rec["wall_s"] = max(0, round(end - start))
    rec.setdefault("input", None)
    rec.setdefault("output", None)
    if start and not args.no_measure:
        try:
            from . import audit_snapshot
            usages, _ = audit_snapshot.collect(datetime.fromtimestamp(start, timezone.utc) - timedelta(minutes=1),
                                               datetime.fromtimestamp(end, timezone.utc) + timedelta(minutes=1))
            m = measure(usages, start, end, args.session or ())
            rec.update(input=m["input"], output=m["output"], usd=m["usd"], calls=m["calls"],
                       measured="audit transcripts, run window (upper bound)")
        except Exception as e:                  # measurement is best effort; the structural record still lands
            rec["measured"] = f"unavailable: {e}"
    try:
        p = append(rec)
    except ValueError as e:
        print(f"run-ledger: {e}", file=sys.stderr)
        return 2
    print(f"recorded in {p}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for r in load()[-args.last:]:
        print(json.dumps(r, sort_keys=True))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rep = calibrate(load())
    print(json.dumps(rep, indent=1) if args.json else format_report(rep))
    return 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("runs", help="Workflow run ledger: record, suggest a size, reassess, calibration report")
    v = p.add_subparsers(dest="runs_command", required=True)

    s = v.add_parser("suggest", help="Recommend inline|light|full from the task features and past runs")
    s.add_argument("--kind", default="")
    s.add_argument("--files", type=int, default=0)
    s.add_argument("--lines", type=int, default=0)
    s.add_argument("--units", type=int, default=1)
    s.add_argument("--risk", default="", help="comma list of risk item numbers")
    s.add_argument("--ambiguous", action="store_true")
    s.add_argument("--resume-fail", action="store_true")
    s.add_argument("--separate", action="store_true", help="the owner wants separate deliverables")
    s.add_argument("--force", choices=MODES, help="the owner's override (--inline/--light/--full)")
    s.set_defaults(func=cmd_suggest)

    r = v.add_parser("reassess", help="Mid-run advice from signals (a recommendation, never an order)")
    r.add_argument("mode", choices=MODES)
    r.add_argument("--verifier-fail", action="store_true")
    r.add_argument("--files-estimated", type=int)
    r.add_argument("--files-actual", type=int)
    r.add_argument("--sensitive", action="store_true")
    r.add_argument("--trivial", action="store_true")
    r.set_defaults(func=cmd_reassess)

    c = v.add_parser("record", help="Append one finished run to the ledger")
    c.add_argument("--mode", required=True, choices=MODES, help="the size the run ended in")
    c.add_argument("--estimated", choices=MODES, help="the size recommended at the start")
    c.add_argument("--outcome", required=True, choices=OUTCOMES)
    c.add_argument("--agents", type=int, help="distinct spawned agents (0 for inline)")
    c.add_argument("--steps", type=int)
    c.add_argument("--rework", type=int, help="times the verifier sent work back")
    c.add_argument("--kind", default="")
    c.add_argument("--forced", action="store_true", default=None)
    c.add_argument("--lines", type=int, help="authored changed lines (additions plus deletions)")
    c.add_argument("--units", type=int)
    c.add_argument("--files", type=int)
    c.add_argument("--risk", default="")
    c.add_argument("--escalations", default="", help="comma list such as inline>light,light>full")
    c.add_argument("--start", type=float, help="epoch seconds")
    c.add_argument("--end", type=float, help="epoch seconds")
    c.add_argument("--session", action="append", help="transcript session id to price (repeatable)")
    c.add_argument("--run", help="free label or orchestrator run id")
    c.add_argument("--from-run", help="read steps, agents, rework and wall time from an orchestrator run id")
    c.add_argument("--no-measure", action="store_true", help="skip the audit lookup")
    c.set_defaults(func=cmd_record)

    li = v.add_parser("list", help="Print the last records")
    li.add_argument("--last", type=int, default=20)
    li.set_defaults(func=cmd_list)

    rp = v.add_parser("report", help="Calibration: cost against quality per mode, with threshold proposals")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(func=cmd_report)
