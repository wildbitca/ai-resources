"""ai-resources audit — cost report from Claude Code session data.

Reads Claude Code's local JSONL session files for the current project (or every project with
--all-projects) and, with --openclaw, the OpenClaw native-runtime transcript events, and tallies
token usage and the API-equivalent estimated cost per model. Streamed messages are deduplicated by
message id (see audit_sources.py). `audit snapshot` / `audit compare` measure before/after deltas.

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
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import audit_sources, billing, model_pins
from .setup import ui


# ---------------------------------------------------------------------------
# Public list prices per million tokens, verified 2026-10-22 (operator-approved source) against
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://ai.google.dev/gemini-api/docs/pricing (paid tier, prompts <= 200k)
#   https://cloud.google.com/vertex-ai/generative-ai/pricing
# Format: (input, output, cache_read, cache_write_5m, cache_write_1h)
# Claude cache writes are 1.25x (5m) / 2x (1h) input. Gemini context-cache storage
# (per token-hour) is not modeled. Update this table whenever list prices change.
# ---------------------------------------------------------------------------
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1":       (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-mythos-5-1":      (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-fable-5":         (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-mythos-5":        (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-opus-5-5":        ( 4.00, 20.00, 0.20,  5.00,  8.00),
    "claude-opus-5":          ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-8":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-7":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-6":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-5":        ( 5.00, 25.00, 0.50,  6.25, 10.00),
    "claude-opus-4-1":        (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-opus-4":          (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-sonnet-5-5":      ( 2.00, 10.00, 0.10,  2.50,  4.00),
    "claude-sonnet-5":        ( 2.00, 10.00, 0.20,  2.50,  4.00),
    "claude-sonnet-4-6":      ( 3.00, 15.00, 0.30,  3.75,  6.00),
    "claude-sonnet-4-5":      ( 3.00, 15.00, 0.30,  3.75,  6.00),
    "claude-sonnet-4":        ( 3.00, 15.00, 0.30,  3.75,  6.00),
    "claude-haiku-5-5":       ( 0.10,  0.50, 0.01,  0.125, 0.20),  # prompts <= 100k; see LONG_CONTEXT
    "claude-haiku-4-5":       ( 1.00,  5.00, 0.10,  1.25,  2.00),
    "claude-3-5-haiku":       ( 0.80,  4.00, 0.08,  1.00,  1.60),
    "gemini-4-argon":         ( 2.00, 10.00, 0.10,  0.00,  0.00),  # through 2026-11-30; see SCHEDULED
    "gemini-3.8-flash":       ( 0.75,  3.75, 0.075, 0.00,  0.00),  # through 2026-12-31; see SCHEDULED
    "gemini-3.5-flash":       ( 1.50,  9.00, 0.15,  0.00,  0.00),
    "gemini-2.5-pro":         ( 1.25, 10.00, 0.125, 0.00,  0.00),
    "gemini-2.5-flash":       ( 0.30,  2.50, 0.03,  0.00,  0.00),
    "gemini-2.5-flash-lite":  ( 0.10,  0.40, 0.01,  0.00,  0.00),
}

# Prompt-size tier: models whose price changes when the prompt exceeds a threshold.
# claude-haiku-5-5: prompts > 100k tokens cost (in, out, cache read, write 5m, write 1h) below.
LONG_CONTEXT: dict[str, tuple[int, tuple[float, float, float, float, float]]] = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05, 0.625, 1.00)),
}

# Date-bounded rows: the PRICES row holds the rate that applies through `until` (inclusive); calls
# after that date use the new rate. The Gemini pages list in/out only; the cache-read rate follows the
# same 2x step as the listed rows.
SCHEDULED: dict[str, tuple[date, tuple[float, float, float, float, float]]] = {
    "gemini-4-argon":   (date(2026, 11, 30), (4.00, 20.00, 0.20, 0.00, 0.00)),
    "gemini-3.8-flash": (date(2026, 12, 31), (1.50, 7.50, 0.15, 0.00, 0.00)),
}

# ADR-0014 D7: poll turns (waits, yields, sleeps) re-read the whole context. The audit alerts when their share of the
# estimated cost is strictly above this fraction; AI_RESOURCES_POLL_SHARE_ALERT overrides it (a fraction, 0 < x <= 1).
POLL_SHARE_ALERT = 0.05
POLL_SHARE_ENV = "AI_RESOURCES_POLL_SHARE_ALERT"

# Claude Code aliases that can appear as the model name; they resolve to the newest model.
ALIASES: dict[str, str] = dict(model_pins.effective())


_SNAPSHOT_SUFFIX = re.compile(r"^(?:[-@]\d{8})?(?:\[[^\]]*\])?$")


def _price(model: str, ctx: int = 0, on: date | None = None) -> tuple[float, float, float, float, float] | None:
    """List price row for a model; `ctx` selects a prompt-size tier and `on` a date-bounded row."""
    row = _base_price(model)
    if row is None:
        return None
    key = _row_key(model)
    if key in LONG_CONTEXT and ctx > LONG_CONTEXT[key][0]:
        return LONG_CONTEXT[key][1]
    if on is not None and key in SCHEDULED and on > SCHEDULED[key][0]:
        return SCHEDULED[key][1]
    return row


def _row_key(model: str) -> str | None:
    base = model.split("[", 1)[0]
    model = ALIASES.get(base, model)
    if model in PRICES:
        return model
    matches = [key for key in PRICES
               if model.startswith(key) and _SNAPSHOT_SUFFIX.match(model[len(key):])]
    return max(matches, key=len) if matches else None


def _base_price(model: str) -> tuple[float, float, float, float, float] | None:
    # Exact key first, then the longest prefix ("claude-fable-5-1-20260101" -> "claude-fable-5-1", not
    # "claude-fable-5"), but only when the rest is a dated snapshot and/or a context suffix. A newer minor
    # without its own row stays unpriced: it is never priced as its major.
    key = _row_key(model)
    return PRICES[key] if key else None


def _estimate(model: str, inp: int, out: int, cr: int, cw5: int, cw1h: int, on: date | None = None) -> float:
    p = _price(model, ctx=inp + cr + cw5 + cw1h, on=on)
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


def poll_share_threshold() -> float:
    raw = os.environ.get(POLL_SHARE_ENV)
    if raw:
        try:
            value = float(raw)
            if 0 < value <= 1:
                return value
        except ValueError:
            pass
    return POLL_SHARE_ALERT


def poll_alert(share) -> bool:
    """True only when the poll-turn cost share is strictly above the threshold."""
    return isinstance(share, (int, float)) and share > poll_share_threshold()


def poll_cost_share(usages) -> float:
    """Share of the estimated cost spent on poll turns (0.0 when nothing is priced)."""
    total = poll = 0.0
    for u in usages:
        c = _estimate(u.model, u.input, u.output, u.cache_read, u.cache_write_5m, u.cache_write_1h, on=u.ts.date())
        total += c
        if u.poll:
            poll += c
    return poll / total if total else 0.0


def haiku_share_above_100k(usages) -> tuple[int, float]:
    """(Haiku calls, share of them whose prompt exceeds the Haiku price-tier threshold of 100k tokens)."""
    haiku = [u for u in usages if "haiku" in u.model]
    if not haiku:
        return 0, 0.0
    return len(haiku), sum(1 for u in haiku if u.ctx > LONG_CONTEXT["claude-haiku-5-5"][0]) / len(haiku)


def claude_md_report(home: Path | None = None) -> dict:
    """The global ~/.claude/CLAUDE.md in tokens (estimate), split into the kit block and the rest."""
    from . import audit_snapshot
    path = (home or Path.home()) / ".claude" / "CLAUDE.md"
    try:
        return audit_snapshot.claude_md_tokens(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {"total": 0, "kit_block": 0, "rest": 0, "basis": "bytes/4 (estimate)"}


def poll_alert_message(share: float) -> str:
    return (f"Poll turns are {share:.0%} of the estimated cost (alert above {poll_share_threshold():.0%}): wait with one "
            "blocking call or run_in_background, not sleep loops; the kit sleep guard refuses a sleep over 30 s.")


def _project_jsonl_dir(cwd: str | None = None) -> Path | None:
    path = cwd or os.getcwd()
    slug = path.replace("/", "-")
    d = Path.home() / ".claude" / "projects" / slug
    return d if d.exists() else None


def _all_project_dirs() -> list[Path]:
    root = Path.home() / ".claude" / "projects"
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _tally(usages: list) -> tuple[dict, list]:
    model_stats: dict = defaultdict(
        lambda: {"inp": 0, "out": 0, "cr": 0, "cw": 0, "calls": 0, "cost": 0.0}
    )
    by_session: dict[str, dict] = {}
    for u in sorted(usages, key=lambda x: x.ts):
        cost = _estimate(u.model, u.input, u.output, u.cache_read, u.cache_write_5m, u.cache_write_1h, on=u.ts.date())
        ms = model_stats[u.model]
        ms["inp"] += u.input; ms["out"] += u.output
        ms["cr"] += u.cache_read; ms["cw"] += u.cache_write
        ms["calls"] += 1; ms["cost"] += cost
        sess = by_session.setdefault(u.session, {
            "date": "", "sid": Path(u.session).stem[:8] if u.session else "?", "models": set(),
            "calls": 0, "out": 0, "cr": 0, "cost": 0.0})
        sess["date"] = u.ts.strftime("%Y-%m-%d %H:%M")
        sess["calls"] += 1; sess["out"] += u.output; sess["cr"] += u.cache_read; sess["cost"] += cost
        sess["models"].add(u.model)
    sessions = sorted(by_session.values(), key=lambda s: s["date"])
    for sess in sessions:
        sess["models"] = sorted(sess["models"])
    return dict(model_stats), sessions


def _parse(jsonl_dir: Path | list, since: datetime | None, include_subagents: bool = False) -> tuple[dict, list]:
    """Per-model stats and per-session rows for one project dir (or a list of dirs), deduplicated by message id."""
    roots = list(jsonl_dir) if isinstance(jsonl_dir, (list, tuple)) else [jsonl_dir]
    return _tally(audit_sources.load_claude_jsonl(roots, since, include_subagents))


def _count_unpriced(models) -> int:
    """How many of the given model ids have no price row (they add nothing to the cost total)."""
    return sum(1 for m in models if _price(m) is None)


def _total_label(unpriced: int) -> str:
    """Footer label of the By-model table; says so when unpriced models add nothing to the cost total."""
    if unpriced <= 0:
        return "TOTAL"
    return f"TOTAL (excludes {unpriced} unpriced model(s))"


def _print_openclaw(con, usages: list, stats) -> None:
    from rich.table import Table  # type: ignore[import]

    con.print()
    if stats is not None and stats.missing:
        ui.detail("OpenClaw: no per-agent transcript databases found; nothing to report.")
        return
    t = Table(title="OpenClaw by agent and model", show_header=True, header_style="bold cyan",
              border_style="dim", show_footer=True)
    t.add_column("Source", style="dim")
    t.add_column("Agent", style="bold")
    t.add_column("Model")
    t.add_column("Calls", justify="right")
    t.add_column("Output", justify="right")
    t.add_column("Cache R", justify="right", style="dim")
    t.add_column("Est. cost", justify="right", style="green")
    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"calls": 0, "out": 0, "cr": 0, "cost": 0.0})
    unpriced = set()
    for u in usages:
        g = groups[(u.agent, u.model)]
        g["calls"] += 1; g["out"] += u.output; g["cr"] += u.cache_read
        g["cost"] += _estimate(u.model, u.input, u.output, u.cache_read, u.cache_write_5m, u.cache_write_1h, on=u.ts.date())
        if _price(u.model) is None:
            unpriced.add(u.model)
    total = 0.0
    for (agent, model), g in sorted(groups.items(), key=lambda kv: kv[1]["cost"], reverse=True):
        known = model not in unpriced
        t.add_row("openclaw", agent, model, f"{g['calls']:,}", f"{g['out']:,}", f"{g['cr']/1e6:.1f}M",
                  f"${g['cost']:.2f}" if known else "n/a")
        total += g["cost"]
    t.columns[0].footer = _total_label(len(unpriced))
    t.columns[6].footer = f"${total:.2f}"
    con.print(t)
    if stats is not None and stats.undecoded:
        ui.warn(f"undecoded rows: {stats.undecoded} (no zstd decoder available; their usage is not counted)")
    for note in (stats.notes if stats is not None else []):
        ui.detail(note)


def billing_report() -> dict:
    """Billing mode, quota, credit and the alerts they raise, as plain JSON-safe data (no identifiers)."""
    snap = billing.snapshot()
    return {**snap, "alerts": [{"code": a.code, "severity": a.severity, "message": a.message}
                               for a in billing.alerts_for(snap)]}


def _print_billing(con) -> None:
    """The billing block of the text report: mode, quota, credit and the alerts."""
    con.print()
    ui.detail("Billing and quota (read-only):")
    snap = billing.snapshot()
    for line in billing.summary_lines(snap):
        ui.detail("  " + line)
    for a in billing.alerts_for(snap):
        (ui.warn if a.severity == "warn" else ui.info)(a.message)


def _load_usages(args: argparse.Namespace) -> tuple[list, "audit_sources.LoadStats | None"]:
    """(usages, OpenClaw load stats) for the arguments of the report; used by the JSON path."""
    all_projects = bool(getattr(args, "all_projects", False))
    include_sub = all_projects or bool(getattr(args, "subagents", False))
    roots = _all_project_dirs() if all_projects else [d for d in (_project_jsonl_dir(),) if d]
    since = datetime.now(tz=timezone.utc) - timedelta(days=args.days) if args.days > 0 else None
    usages = audit_sources.load_claude_jsonl(roots, since, include_sub) if roots else []
    oc_stats = None
    if getattr(args, "openclaw", False):
        oc, oc_stats = audit_sources.load_openclaw_events(Path.home() / ".openclaw" / "agents", since)
        usages = usages + oc
    return usages, oc_stats


def cmd_audit_json(args: argparse.Namespace) -> int:
    """`ai-resources audit --json`: one JSON document on stdout for tools (the daily Slack section). Read-only.

    It never prints an identifier and does not need rich."""
    from . import audit_snapshot
    if getattr(args, "billing_only", False):
        # Billing, quota and credit only: no transcript is read (the daily Slack section already has the snapshot).
        print(json.dumps({"schema": 1, "billing": billing_report()}, indent=1, default=str))
        return 0
    usages, oc_stats = _load_usages(args)
    doc = {
        "schema": 1,
        "window_days": args.days,
        "estimate": "API-equivalent; subscription use is not billed per token",
        "billing": billing_report(),
        "all": audit_snapshot.summarize(usages),
    }
    share = poll_cost_share(usages)
    doc["poll"] = {"share": round(share, 4), "threshold": poll_share_threshold(), "alert": poll_alert(share)}
    doc["poll_alert"] = doc["poll"]["alert"]
    calls, hshare = haiku_share_above_100k(usages)
    doc["haiku"] = {"calls": calls, "share_above_100k": round(hshare, 3)}
    doc["global_claude_md_tokens_est"] = claude_md_report()
    if oc_stats is not None and oc_stats.undecoded:
        doc["undecoded_rows"] = oc_stats.undecoded
    print(json.dumps(doc, indent=1, default=str))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    if getattr(args, "json", False) and not getattr(args, "audit_cmd", None):
        return cmd_audit_json(args)
    ui.require_deps()
    con = ui.console()

    all_projects = bool(getattr(args, "all_projects", False))
    with_openclaw = bool(getattr(args, "openclaw", False))
    include_sub = all_projects or bool(getattr(args, "subagents", False))
    if all_projects:
        jsonl_dir = _all_project_dirs() or None
    else:
        jsonl_dir = _project_jsonl_dir()
    if not jsonl_dir and not with_openclaw:
        ui.warn("No Claude Code session data found for this project directory.")
        ui.detail(f"Expected: ~/.claude/projects/{os.getcwd().replace('/', '-')}/")
        return 1

    since: datetime | None = None
    if args.days > 0:
        since = datetime.now(tz=timezone.utc) - timedelta(days=args.days)

    cc_usages = audit_sources.load_claude_jsonl(
        list(jsonl_dir) if isinstance(jsonl_dir, (list, tuple)) else [jsonl_dir], since, include_sub) if jsonl_dir else []
    model_stats, sessions = _tally(cc_usages) if cc_usages else ({}, [])
    oc_usages: list = []
    oc_stats = None
    if with_openclaw:
        oc_usages, oc_stats = audit_sources.load_openclaw_events(Path.home() / ".openclaw" / "agents", since)
    if not model_stats and not oc_usages:
        ui.warn("No usage data found.")
        ui.detail(f"Scanned: {jsonl_dir}")
        if oc_stats is not None and oc_stats.missing:
            ui.detail("No OpenClaw agent databases found under ~/.openclaw/agents.")
        # An absent OpenClaw install is not a failure of an --openclaw report: nothing to measure, exit 0.
        return 0 if with_openclaw else 1

    period = f"last {args.days} days" if args.days > 0 else "all time"
    ui.banner("Cost report (Claude Code sessions)",
              subtitle=f"{len(sessions)} sessions · {period} · API-equivalent estimate")

    # ── By model ──────────────────────────────────────────────────────────
    from rich.table import Table  # type: ignore[import]

    t = Table(
        title="By model",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        show_footer=True,
    )
    t.add_column("Model", style="bold", footer="TOTAL", min_width=20)
    t.add_column("Calls", justify="right")
    t.add_column("Output", justify="right")
    t.add_column("Cache R", justify="right", style="dim")
    t.add_column("Cache W", justify="right", style="dim")
    t.add_column("Est. cost", justify="right", style="green")

    rows = sorted(model_stats.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    total_calls = total_out = total_cr = total_cw = 0
    total_cost = 0.0
    unpriced = _count_unpriced(m for m, _ in rows)
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

    t.columns[0].footer = _total_label(unpriced)
    t.columns[1].footer = f"{total_calls:,}"
    t.columns[2].footer = f"{total_out:,}"
    t.columns[3].footer = f"{total_cr/1e9:.2f}B"
    t.columns[4].footer = f"{total_cw/1e6:.0f}M"
    t.columns[5].footer = f"${total_cost:.2f}"
    con.print(t)

    if with_openclaw:
        _print_openclaw(con, oc_usages, oc_stats)

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
    if model_stats and total_cr > 0:
        cache_pct = 100.0 * (total_cr / (total_cr + total_out + sum(v["inp"] for v in model_stats.values()) + 1))
        ui.detail(f"Cache reads are {total_cr/1e9:.2f}B tokens ({cache_pct:.0f}% of all tokens) — cache reduces cost vs fresh input.")

    all_usages = cc_usages + oc_usages
    share = poll_cost_share(all_usages)
    if poll_alert(share):
        ui.warn(poll_alert_message(share))
    calls, hshare = haiku_share_above_100k(all_usages)
    if calls:
        ui.detail(f"Haiku calls above the 100k prompt tier: {hshare:.1%} of {calls:,} calls "
                  "(those are priced at the higher tier).")
    md = claude_md_report()
    ui.detail(f"global CLAUDE.md ~{md['total']:,} tokens (estimate): kit block ~{md['kit_block']:,}, "
              f"your own content ~{md['rest']:,}.")
    _print_billing(con)
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from . import audit_snapshot
    return audit_snapshot.cmd_snapshot(args)


def _cmd_compare(args: argparse.Namespace) -> int:
    from . import audit_snapshot
    return audit_snapshot.cmd_compare(args)


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser(
        "audit",
        help="Show cost report from Claude Code session data (and snapshot/compare for before/after)",
    )
    p.add_argument(
        "--days", type=int, default=0,
        help="Limit to last N days (default: all time)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show per-session breakdown",
    )
    p.add_argument("--all-projects", action="store_true",
                   help="Scan every project under ~/.claude/projects, including subagent transcripts")
    p.add_argument("--subagents", action="store_true",
                   help="Include subagent transcripts of the current project")
    p.add_argument("--openclaw", action="store_true",
                   help="Also report OpenClaw native-runtime usage per agent and model (read-only)")
    p.add_argument("--json", action="store_true",
                   help="Print one JSON document (billing, quota, credit, alerts and the summary metrics) and exit")
    p.add_argument("--billing-only", dest="billing_only", action="store_true",
                   help="With --json: print only the billing block and read no transcript")
    p.set_defaults(func=cmd_audit)

    asub = p.add_subparsers(dest="audit_cmd")
    sp = asub.add_parser("snapshot", help="Record cost metrics and config inventory for a time window")
    sp.add_argument("label", help="Snapshot name (stored under $XDG_STATE_HOME/ai-resources/cost-baselines/)")
    sp.add_argument("--days", type=int, default=7, help="Window length in days (default 7)")
    sp.add_argument("--since", help="Window start (ISO 8601); overrides --days")
    sp.add_argument("--until", help="Window end (ISO 8601); default now")
    sp.add_argument("--probe", action="store_true",
                    help="Also run the two fixed probes (two real model calls; cost is printed first)")
    sp.set_defaults(func=_cmd_snapshot)
    cp = asub.add_parser("compare", help="Print old, new and relative delta for each metric of two snapshots")
    cp.add_argument("a", help="Snapshot label or path (before)")
    cp.add_argument("b", help="Snapshot label or path (after)")
    cp.set_defaults(func=_cmd_compare)
