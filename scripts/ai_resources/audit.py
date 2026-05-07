"""ai-resources audit — cost report from Claude Code session data.

Reads Claude Code's local JSONL session files for the current project and
tallies token usage and estimated cost per model.

Data source: ~/.claude/projects/<path-slug>/*.jsonl
These files contain every API call made by the cockpit and its subagents,
including model name and full token usage breakdown (input, output, cache_read,
cache_creation).

LiteLLM note: calls routed via LiteLLM to Gemini/OpenAI show the model name
that LiteLLM returns in the response body. In practice, Claude Code's cockpit
calls always show the Claude model; subagent calls show whatever model is in
the subagent's frontmatter (as returned by the gateway).
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .setup import ui


# ---------------------------------------------------------------------------
# Public list prices per million tokens (approximate, 2026-05).
# Format: (input, output, cache_read, cache_write)
# ---------------------------------------------------------------------------
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-7":        (15.00, 75.00,  1.50, 18.75),
    "claude-opus-4-6":        (15.00, 75.00,  1.50, 18.75),
    "claude-opus-4-5":        (15.00, 75.00,  1.50, 18.75),
    "claude-sonnet-4-6":      ( 3.00, 15.00,  0.30,  3.75),
    "claude-sonnet-4-5":      ( 3.00, 15.00,  0.30,  3.75),
    "claude-haiku-4-5":       ( 0.80,  4.00,  0.08,  1.00),
    "gemini-2.5-pro":         ( 1.25, 10.00,  0.00,  0.00),
    "gemini-2.5-flash":       ( 0.075, 0.30,  0.00,  0.00),
    "gemini-2.5-flash-lite":  ( 0.015, 0.075, 0.00,  0.00),
    "gpt-4o":                 ( 2.50, 10.00,  0.00,  0.00),
    "gpt-4o-mini":            ( 0.15,  0.60,  0.00,  0.00),
}


def _price(model: str) -> tuple[float, float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    # prefix match (e.g. "claude-sonnet-4-6-20251101" → "claude-sonnet-4-6")
    for key, p in PRICES.items():
        if model.startswith(key):
            return p
    return None


def _estimate(model: str, inp: int, out: int, cr: int, cw: int) -> float:
    p = _price(model)
    if p is None:
        return 0.0
    return (inp * p[0] + out * p[1] + cr * p[2] + cw * p[3]) / 1_000_000


def _project_jsonl_dir(cwd: str | None = None) -> Path | None:
    path = cwd or os.getcwd()
    slug = path.replace("/", "-")
    d = Path.home() / ".claude" / "projects" / slug
    return d if d.exists() else None


def _parse(jsonl_dir: Path, since: datetime | None) -> tuple[dict, list]:
    model_stats: dict = defaultdict(
        lambda: {"inp": 0, "out": 0, "cr": 0, "cw": 0, "calls": 0, "cost": 0.0}
    )
    sessions: list[dict] = []

    files = sorted(jsonl_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
    for f in files:
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if since and mtime < since:
            continue

        sess: dict = {
            "date": mtime.strftime("%Y-%m-%d %H:%M"),
            "sid": f.stem[:8],
            "models": set(),
            "calls": 0, "out": 0, "cr": 0, "cost": 0.0,
        }
        try:
            with open(f) as fp:
                for line in fp:
                    try:
                        inner = json.loads(line).get("message", {})
                        usage = inner.get("usage", {})
                        i = usage.get("input_tokens", 0)
                        o = usage.get("output_tokens", 0)
                        if i + o == 0:
                            continue
                        cr = usage.get("cache_read_input_tokens", 0)
                        cw = usage.get("cache_creation_input_tokens", 0)
                        model = inner.get("model", "unknown")
                        cost = _estimate(model, i, o, cr, cw)
                        ms = model_stats[model]
                        ms["inp"] += i; ms["out"] += o
                        ms["cr"] += cr; ms["cw"] += cw
                        ms["calls"] += 1; ms["cost"] += cost
                        sess["calls"] += 1; sess["out"] += o
                        sess["cr"] += cr; sess["cost"] += cost
                        sess["models"].add(model)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
        except OSError:
            pass

        if sess["calls"] > 0:
            sess["models"] = sorted(sess["models"])
            sessions.append(sess)

    return dict(model_stats), sessions


def cmd_audit(args: argparse.Namespace) -> int:
    ui.require_deps()
    con = ui.console()

    jsonl_dir = _project_jsonl_dir()
    if not jsonl_dir:
        ui.warn("No Claude Code session data found for this project directory.")
        ui.detail(f"Expected: ~/.claude/projects/{os.getcwd().replace('/', '-')}/")
        return 1

    since: datetime | None = None
    if args.days > 0:
        since = datetime.now(tz=timezone.utc) - timedelta(days=args.days)

    model_stats, sessions = _parse(jsonl_dir, since)
    if not model_stats:
        ui.warn("No usage data found.")
        ui.detail(f"Scanned: {jsonl_dir}")
        return 1

    period = f"last {args.days} days" if args.days > 0 else "all time"
    ui.banner("Cost report (Claude Code sessions)", subtitle=f"{len(sessions)} sessions · {period}")

    # ── By model ──────────────────────────────────────────────────────────
    from rich.table import Table  # type: ignore[import]

    t = Table(
        title="By model",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        show_footer=True,
    )
    t.add_column("Model", style="bold", footer="TOTAL")
    t.add_column("API calls", justify="right")
    t.add_column("Output tokens", justify="right")
    t.add_column("Cache reads", justify="right", style="dim")
    t.add_column("Cache writes", justify="right", style="dim")
    t.add_column("Cost (est.)", justify="right", style="green")

    rows = sorted(model_stats.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    total_calls = total_out = total_cr = total_cw = 0
    total_cost = 0.0
    for model, v in rows:
        known = _price(model) is not None
        cost_str = f"${v['cost']:.2f}" if known else "n/a"
        t.add_row(
            model,
            f"{v['calls']:,}",
            f"{v['out']:,}",
            f"{v['cr']/1e6:.1f}M",
            f"{v['cw']/1e6:.1f}M",
            cost_str,
        )
        total_calls += v["calls"]
        total_out += v["out"]
        total_cr += v["cr"]
        total_cw += v["cw"]
        total_cost += v["cost"]

    t.columns[1].footer = f"{total_calls:,}"
    t.columns[2].footer = f"{total_out:,}"
    t.columns[3].footer = f"{total_cr/1e9:.2f}B"
    t.columns[4].footer = f"{total_cw/1e6:.0f}M"
    t.columns[5].footer = f"${total_cost:.2f}"
    con.print(t)

    # ── Sessions (verbose) ────────────────────────────────────────────────
    if args.verbose and sessions:
        con.print()
        st = Table(
            title="By session (recent first)",
            show_header=True,
            header_style="bold",
            border_style="dim",
        )
        st.add_column("Session", style="dim")
        st.add_column("Date")
        st.add_column("Models", style="dim")
        st.add_column("Calls", justify="right")
        st.add_column("Output", justify="right")
        st.add_column("Cache R", justify="right", style="dim")
        st.add_column("Cost", justify="right", style="green")
        for s in reversed(sessions[-20:]):
            st.add_row(
                s["sid"],
                s["date"],
                ", ".join(s["models"]),
                f"{s['calls']:,}",
                f"{s['out']:,}",
                f"{s['cr']/1e6:.0f}M",
                f"${s['cost']:.2f}",
            )
        con.print(st)

    # ── Notes ─────────────────────────────────────────────────────────────
    con.print()
    ui.detail("Prices are public Anthropic/Google list prices — actual cost depends on your plan.")
    ui.detail("Gemini calls via LiteLLM show their provider model name when the gateway returns it.")
    if total_cr > 0:
        cache_pct = 100.0 * (total_cr / (total_cr + total_out + sum(v["inp"] for v in model_stats.values()) + 1))
        ui.detail(f"Cache reads are {total_cr/1e9:.2f}B tokens ({cache_pct:.0f}% of all tokens) — cache reduces cost vs fresh input.")

    return 0


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "audit",
        help="Show cost report from Claude Code session data",
    )
    p.add_argument(
        "--days", type=int, default=0,
        help="Limit to last N days (default: all time)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show per-session breakdown",
    )
    p.set_defaults(func=cmd_audit)
