"""Antigravity's two weekly quota pools: parse `agy -p "/usage"` and grade the result.

Antigravity (agy) serves two **independent** weekly quota pools — one for Gemini models,
one shared by Claude and GPT models. Nothing else in the kit knows this; this leaf module
is the one place that does, so the engine prompt (S2/S3), the voice prompt (S5) and
`doctor.py` (S4) render identical wording from one tested function.

Captured verbatim on 2026-09-17 (see `tests/fixtures/agy_usage.txt`), `agy -p "/usage"`
prints one tab-separated row per pool:

    Gemini Models\tWeekly Limit Remaining\t0%\tYYYY-MM-DDTHH:MM:SSZ
    Claude and GPT models\tWeekly Limit Remaining\t81%\tYYYY-MM-DDTHH:MM:SSZ

**The percent column is REMAINING, not consumed.** Inverting it would make a healthy
81%-remaining pool look nearly exhausted — the single most likely logic bug in this
feature. `parse_usage` never inverts it; `report`'s severity thresholds read it as-is.

This module never imports `openclaw.py` (the cockpit): `read_usage`'s default runner is a
plain `subprocess` call, not `openclaw._agy(...)`, so the leaf stays independent of the
cockpit that calls it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

POOL_GEMINI = "Gemini Models"
POOL_CLAUDE_GPT = "Claude and GPT models"


def pool_for_model(model_id: str) -> str:
    """Prefix rule: `gemini-*` -> Gemini pool, `claude-*`/`gpt-*` -> Claude/GPT pool.

    Prefix-based, not a lookup table, so a variant agy adds tomorrow (a new
    `gemini-3.9-*` or `claude-sonnet-5-*` id) still classifies without a code change.
    Unknown prefixes return "" — never guessed.
    """
    if model_id.startswith("gemini-"):
        return POOL_GEMINI
    if model_id.startswith("claude-") or model_id.startswith("gpt-"):
        return POOL_CLAUDE_GPT
    return ""


@dataclass(frozen=True)
class Pool:
    name: str
    remaining_pct: int | None
    resets_at: str


def parse_usage(text: str) -> list[Pool]:
    """Parse `agy -p "/usage"` stdout into `Pool`s. Never raises.

    Tolerant of extra/short columns, non-numeric percents, blank lines and agy log noise
    on stdout (agy sometimes prints a banner line or a trailing newline before the table).
    A row whose pool label is not one of the two known constants is still returned
    verbatim (never dropped, never a crash) — agy renaming or adding a pool must show up
    as a new/odd label in the doctor output, not silently disappear.
    """
    pools: list[Pool] = []
    for line in (text or "").splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 3:
            # Not a pool row (banner/log noise) — skip, don't crash.
            continue
        name = cols[0].strip()
        pct_col = cols[2].strip()
        pct: int | None = None
        if pct_col.endswith("%"):
            digits = pct_col[:-1].strip()
            if digits.lstrip("-").isdigit():
                pct = int(digits)
        resets_at = cols[3].strip() if len(cols) > 3 else ""
        if not name:
            continue
        pools.append(Pool(name=name, remaining_pct=pct, resets_at=resets_at))
    return pools


def read_usage(
    run: Callable[[list[str]], tuple[int, str]] | None = None,
    timeout: int = 30,
    agy_bin: str = "agy",
) -> tuple[list[Pool], str]:
    """Run `agy -p "/usage"` and parse it. Never raises.

    `run` is an injected runner callable `(args) -> (returncode, combined_output)` so
    tests exercise the real parser with zero subprocess monkeypatching. The default
    runner is a plain `subprocess` call — deliberately **not** `openclaw._agy(...)`, which
    would make this leaf module import the cockpit it exists to stay independent of.

    Returns `([], reason)` on a missing binary, non-zero exit, timeout or output with no
    parseable pool rows. Always a 2-tuple; never raises.
    """
    if run is None:
        def run(args: list[str]) -> tuple[int, str]:
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                )
            except FileNotFoundError:
                return 127, "agy binary not found"
            except subprocess.TimeoutExpired:
                return 124, "agy -p /usage timed out"
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    try:
        rc, output = run([agy_bin, "-p", "/usage"])
    except Exception as exc:  # belt-and-suspenders: read_usage must never raise
        return [], f"agy -p /usage failed: {exc}"

    if rc != 0:
        return [], f"agy -p /usage exited {rc}: {output.strip()[:200]}"

    pools = parse_usage(output)
    if not pools:
        return [], "agy -p /usage returned no parseable pool rows"
    return pools, ""


def report(
    pools: list[Pool], applied_model: str = "", low_pct: int = 10,
) -> list[tuple[str, str]]:
    """Render `(severity, message)` pairs — one function, so the doctor block and the
    interactive annotation say exactly the same thing for exactly the same input.

    Severity is scoped to the pool backing `applied_model`:
      - `error` when that pool is at 0% remaining.
      - `warn` when that pool has `1..low_pct` percent remaining.
      - `ok` otherwise, and for every pool the applied model is not on (regardless of
        their percent — warning about a pool the user does not use trains people to
        ignore the check).
    """
    applied_pool = pool_for_model(applied_model) if applied_model else ""
    lines: list[tuple[str, str]] = []
    for pool in pools:
        pct_text = f"{pool.remaining_pct}%" if pool.remaining_pct is not None else "unknown%"
        resets = f" (resets {pool.resets_at})" if pool.resets_at else ""
        message = f"{pool.name}: {pct_text} remaining{resets}"
        if pool.name == applied_pool and pool.remaining_pct is not None:
            if pool.remaining_pct <= 0:
                lines.append(("error", message))
                continue
            if pool.remaining_pct <= low_pct:
                lines.append(("warn", message))
                continue
        lines.append(("ok", message))
    return lines


def remedy(pools: list[Pool], exhausted_pool: str, low_pct: int = 10) -> str:
    """The one sentence telling a user where to go when `exhausted_pool` is spent.

    It never points at a pool that is itself spent. The two weekly pools are
    independent and both can sit at 0% at once — a Workspace account on the FREE
    tier burns Gemini in hours — so a blind "pick the other pool" would send the
    user to an equally dead bucket. Only pools actually reported by /usage are
    offered, so an unrecognised third pool can never be invented by inversion.
    """
    others = [p for p in pools if p.name and p.name != exhausted_pool]
    usable = [p for p in others if p.remaining_pct is None or p.remaining_pct > 0]
    if not usable:
        if others:
            detail = ", ".join(
                f'"{p.name}" at {p.remaining_pct}%' if p.remaining_pct is not None
                else f'"{p.name}"'
                for p in others
            )
            return (f"Every other pool is spent too ({detail}) — wait for the weekly "
                    f"reset, or move this agent off agy.")
        return ("No other pool was reported — wait for the weekly reset, or move this "
                "agent off agy.")
    names = " or ".join(f'"{p.name}"' for p in usable)
    nearly = all(
        p.remaining_pct is not None and p.remaining_pct <= low_pct for p in usable
    )
    tail = " (also nearly spent)" if nearly else ""
    return f"Re-run `ai-resources setup` and pick a model from the {names} pool{tail}."
