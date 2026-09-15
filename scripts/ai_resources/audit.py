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
# Public list prices per million tokens, verified 2026-09-15 against
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://ai.google.dev/gemini-api/docs/pricing (paid tier, prompts <= 200k)
# Format: (input, output, cache_read, cache_write_5m, cache_write_1h)
# Claude cache writes are 1.25x (5m) / 2x (1h) input. Gemini context-cache storage
# (per token-hour) is not modeled. Update this table whenever list prices change.
# ---------------------------------------------------------------------------
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1":       (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-mythos-5-1":      (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-fable-5":         (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-mythos-5":        (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-opus-5":          ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-8":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-7":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-6":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-5":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-1":        (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-opus-4":          (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-sonnet-5":        ( 2.00, 10.00, 0.20,  2.50,  4.00),
    "claude-sonnet-4-6":      ( 3.00, 15.00, 0.30,  3.75,  6.00),
    "claude-sonnet-4-5":      ( 3.00, 15.00, 0.30,  3.75,  6.00),
    "claude-sonnet-4":        ( 3.00, 15.00, 0.30,  3.75,  6.00),
    "claude-haiku-4-5":       ( 1.00,  5.00, 0.10,  1.25,  2.00),
    "claude-3-5-haiku":       ( 0.80,  4.00, 0.08,  1.00,  1.60),
    "gemini-3.8-flash":       ( 0.75,  3.75, 0.075, 0.00,  0.00),  # through 2026-12-31
    "gemini-3.5-flash":       ( 1.50,  9.00, 0.15,  0.00,  0.00),
    "gemini-2.5-pro":         ( 1.25, 10.00, 0.125, 0.00,  0.00),
    "gemini-2.5-flash":       ( 0.30,  2.50, 0.03,  0.00,  0.00),
    "gemini-2.5-flash-lite":  ( 0.10,  0.40, 0.01,  0.00,  0.00),
}

# Claude Code aliases that can appear as the model name; they resolve to the newest model.
ALIASES: dict[str, str] = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def _price(model: str) -> tuple[float, float, float, float, float] | None:
    base = model.split("[", 1)[0]  # "opus[1m]" → "opus"; context suffixes don't change price
    model = ALIASES.get(base, model)
    if model in PRICES:
        return PRICES[model]
    # Longest prefix wins ("claude-fable-5-1[1m]" → "claude-fable-5-1", not "claude-fable-5").
    matches = [key for key in PRICES if model.startswith(key)]
    return PRICES[max(matches, key=len)] if matches else None


def _estimate(model: str, inp: int, out: int, cr: int, cw5: int, cw1h: int) -> float:
    p = _price(model)
    if p is None:
        return 0.0
    return (inp * p[0] + out * p[1] + cr * p[2] + cw5 * p[3] + cw1h * p[4]) / 1_000_000


def _cache_writes(usage: dict) -> tuple[int, int]:
    """Split cache-creation tokens into (5-minute, 1-hour) writes; unknown TTL counts as 5m."""
    total = usage.get("cache_creation_input_tokens") or 0
    detail = usage.get("cache_creation")
    if isinstance(detail, dict):
        five_min = detail.get("ephemeral_5m_input_tokens") or 0
        one_hour = detail.get("ephemeral_1h_input_tokens") or 0
        if five_min or one_hour:
            return five_min, one_hour
    return total, 0


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
                        cw5, cw1h = _cache_writes(usage)
                        cw = cw5 + cw1h
                        model = inner.get("model", "unknown")
                        cost = _estimate(model, i, o, cr, cw5, cw1h)
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
    ui.detail("Prices are public Anthropic/Google list prices — actual cost depends on your plan; "
              "on a Pro/Max subscription usage is not billed per token.")
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
