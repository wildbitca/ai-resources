"""Check the `## Sizing` block that the planner writes at the top of a plan or `tasks.md`.

Pure and deterministic: no model, no network. The block is the contract between `/kit-plan`, `/kit-implement` and the
verifier (skills/kit-orchestration "Task size"), so a plan that contradicts its own mode is reported before work starts.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MODES = ("inline", "light", "full")
REQUIRED = ("mode", "resume_test", "risk_items", "forecast", "route", "verification", "spawn_budget")
_HEADING = re.compile(r"^##[ \t]+Sizing[ \t]*$", re.M)
_NEXT_HEADING = re.compile(r"^##[ \t]", re.M)
_KEY = re.compile(r"^([a-z_]+):[ \t]*(.*)$")


def sizing_fields(text: str) -> dict[str, str] | None:
    """The `key: value` pairs of the first `## Sizing` block (`|` separates pairs on one line), or None without one."""
    m = _HEADING.search(text)
    if not m:
        return None
    rest = text[m.end():]
    nxt = _NEXT_HEADING.search(rest)
    body = rest[:nxt.start()] if nxt else rest
    fields: dict[str, str] = {}
    for line in body.splitlines():
        line = re.sub(r"\s#\s.*$", "", line).strip().lstrip("-").strip()
        for part in line.split("|"):
            k = _KEY.match(part.strip())
            if k:
                fields.setdefault(k.group(1), k.group(2).strip())
    return fields


def _first(value: str) -> str:
    return re.split(r"[\s(]", value.strip(), maxsplit=1)[0].lower() if value.strip() else ""


def problems(text: str) -> list[str]:
    """Why the Sizing block is missing or inconsistent; an empty list means it is usable."""
    f = sizing_fields(text)
    if f is None:
        return ["no `## Sizing` block"]
    out = [f"Sizing: missing `{k}`" for k in REQUIRED if k not in f]
    mode = _first(f.get("mode", ""))
    if "mode" in f and mode not in MODES:
        out.append(f"Sizing: mode `{f['mode']}` is not one of {', '.join(MODES)}")
    risks = _first(f.get("risk_items", "")) not in ("", "none", "[]")
    verification = _first(f.get("verification", ""))
    route = _first(f.get("route", ""))
    budget = f.get("spawn_budget", "")
    if budget and not budget.split()[0].isdigit():
        out.append(f"Sizing: spawn_budget `{budget}` is not a number")
    if mode == "inline":
        if risks:
            out.append("Sizing: a risk item takes a task out of inline (use light)")
        if budget and budget.split()[0].isdigit() and int(budget.split()[0]) != 0:
            out.append("Sizing: inline means spawn_budget 0")
        if route and route != "inline":
            out.append("Sizing: inline work has route inline")
    if risks and verification and verification != "verifier":
        out.append("Sizing: a risk item needs `verification: verifier(item N)`")
    if mode == "light" and route == "writers":
        out.append("Sizing: writers need two or more independent units over about 100 lines each (full)")
    return out


def main(argv: list[str] | None = None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print("usage: plan-check <plan-or-tasks.md> [...]", file=sys.stderr)
        return 2
    status = 0
    for p in paths:
        try:
            found = problems(Path(p).read_text(encoding="utf-8"))
        except OSError as e:
            found = [f"cannot read: {e.strerror or e}"]
        for line in found:
            print(f"{p}: {line}", file=sys.stderr)
        status = 1 if found else status
        if not found:
            print(f"{p}: sizing OK")
    return status
