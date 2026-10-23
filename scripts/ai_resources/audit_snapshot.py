"""`ai-resources audit snapshot|compare`: measured before/after token-cost deltas.

A snapshot summarises a time window of Claude Code and OpenClaw usage with metrics that stay
comparable between windows of different load (per active hour, per session, per call, context size
percentiles, share of calls above 200k, poll-turn cost share, cache hit ratio) and records the
config inventory of the keys the lean-defaults work changes. The JSON schema is the one written by
~/.cache/cost-analysis/bench.py, so its `before-*.json` files can be passed to `compare`.

Figures are API-equivalent estimates: subscription use is not billed per token.
"""
from __future__ import annotations

import functools
import glob
import json
import os
import shutil
import statistics as st
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import audit, audit_sources, billing

# How the kit counts a call. bench.py (the operator's script) kept the FIRST record per (message id, requestId); the kit
# keeps the LAST record per message id, because streamed records carry cumulative usage. Counts differ, so a baseline
# taken with bench.py is not comparable: re-take it with `audit snapshot --since/--until` over the same window.
METHOD = "kit-2: dedupe=message.id, last-record-wins; subagents and OpenClaw events included"

METRIC_KEYS = [
    "calls", "sessions", "active_hours", "cost_usd", "cost_per_active_hour", "cost_per_session", "cost_per_call",
    "calls_per_session_median", "first_call_ctx_median", "ctx_p50", "ctx_p90", "share_calls_ctx_gt_200k",
    "share_cache_read_from_ctx_gt_200k", "share_cost_poll_turns", "cache_hit_ratio", "haiku_calls",
    "share_haiku_calls_ctx_gt_100k",
]


def baselines_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-resources" / "cost-baselines"


def _cost(u: audit_sources.Usage) -> float | None:
    if audit._price(u.model) is None:
        return None
    return audit._estimate(u.model, u.input, u.output, u.cache_read, u.cache_write_5m, u.cache_write_1h, on=u.ts.date())


BREAKDOWNS = ("by_origin", "by_provider", "by_role")
BREAKDOWN_FIELDS = ("calls", "input", "output", "usd")


def _breakdown(usages: list[audit_sources.Usage], key: Callable[[audit_sources.Usage], str]) -> dict[str, dict]:
    """{group: {calls, input, output, usd, unpriced_calls}} sorted by cost then name (ADR-0015 S14)."""
    out: dict[str, dict] = {}
    for u in usages:
        name = key(u) or "unknown"
        b = out.setdefault(name, {"calls": 0, "input": 0, "output": 0, "usd": 0.0, "unpriced_calls": 0})
        b["calls"] += 1
        b["input"] += u.input
        b["output"] += u.output
        c = _cost(u)
        if c is None:
            b["unpriced_calls"] += 1
        else:
            b["usd"] += c
    for b in out.values():
        b["usd"] = round(b["usd"], 4)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["usd"], kv[0])))


def summarize(usages: list[audit_sources.Usage]) -> dict:
    """Same metric definitions as bench.py."""
    if not usages:
        return {"calls": 0}
    costs = [(u, _cost(u)) for u in usages]
    priced = [(u, c) for u, c in costs if c is not None]
    cost = sum(c for _, c in priced)
    by_sess: dict[str, list[audit_sources.Usage]] = defaultdict(list)
    for u in usages:
        by_sess[u.session].append(u)
    firsts = [min(v, key=lambda u: u.ts).ctx for v in by_sess.values()]
    hours = {u.ts.strftime("%Y-%m-%dT%H") for u in usages}
    days = {u.ts.date() for u in usages}
    ctx = sorted(u.ctx for u in usages)
    cr_total = sum(u.cache_read for u in usages) or 1
    inp_total = sum(u.cache_read + u.cache_write + u.input for u in usages) or 1
    q = st.quantiles(ctx, n=10) if len(ctx) > 1 else [ctx[0]] * 9
    agents = {u.agent for u in usages if u.agent}
    haiku = [u for u in usages if "haiku" in u.model]
    return {
        "calls": len(usages), "sessions": len(by_sess), "active_hours": len(hours), "active_days": len(days),
        "cost_usd": round(cost, 2), "unpriced_calls": len(usages) - len(priced),
        "cost_per_active_hour": round(cost / len(hours), 3),
        "cost_per_session": round(cost / len(by_sess), 4), "cost_per_call": round(cost / len(usages), 5),
        "calls_per_session_median": st.median(len(v) for v in by_sess.values()),
        "first_call_ctx_median": int(st.median(firsts)),
        "ctx_p50": int(q[4]), "ctx_p90": int(q[8]),
        "share_calls_ctx_gt_200k": round(sum(1 for x in ctx if x > 200_000) / len(ctx), 3),
        "share_cache_read_from_ctx_gt_200k": round(sum(u.cache_read for u in usages if u.ctx > 200_000) / cr_total, 3),
        "share_cost_poll_turns": round(sum(c or 0 for u, c in costs if u.poll) / (cost or 1), 3),
        "cache_hit_ratio": round(cr_total / inp_total, 3),
        # ADR-0014 D1: a Haiku call above 100k prompt tokens is priced in the higher tier (audit.LONG_CONTEXT).
        "haiku_calls": len(haiku),
        "share_haiku_calls_ctx_gt_100k": round(sum(1 for u in haiku if u.ctx > 100_000) / len(haiku), 3) if haiku else 0.0,
        "by_model": dict(Counter(u.model for u in usages)),
        "by_agent_cost": {k: round(v, 2) for k, v in sorted(
            ((a, sum(c or 0 for u, c in costs if u.agent == a)) for a in agents), key=lambda x: -x[1])},
        # ADR-0015 S14: who spent what. An older snapshot has none of these keys and compares as n/a.
        "by_origin": _breakdown(usages, lambda u: u.origin),
        "by_provider": _breakdown(usages, lambda u: u.provider),
        "by_role": _breakdown(usages, lambda u: u.role),
    }


def _default_run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:  # noqa: BLE001 - inventory is best-effort
        return f"error: {e}"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


KIT_BLOCK_BEGIN = "<!-- BEGIN ai-resources"
KIT_BLOCK_END = "<!-- END ai-resources -->"


def claude_md_tokens(text: str) -> dict:
    """Token estimate (bytes / 4, labelled an estimate) of a global CLAUDE.md, split into the kit block and the rest."""
    begin = text.find(KIT_BLOCK_BEGIN)
    end = text.find(KIT_BLOCK_END, begin) if begin >= 0 else -1
    kit = text[begin:end + len(KIT_BLOCK_END)] if begin >= 0 and end >= 0 else ""
    rest = text.replace(kit, "", 1) if kit else text
    est = lambda s: len(s.encode("utf-8")) // 4          # noqa: E731 - bytes/4: an estimate, not a tokenizer
    return {"total": est(text), "kit_block": est(kit), "rest": est(rest), "basis": "bytes/4 (estimate)"}


def _billing_inventory() -> dict:
    """Auth mode and the two quota percentages (no identifier). Read-only; unknown when a file is missing."""
    try:
        snap = billing.snapshot()
    except Exception:  # noqa: BLE001 - inventory is best-effort
        return {"auth_mode": "unknown"}
    q = snap.get("quota") or {}
    return {"auth_mode": snap.get("auth_mode"),
            "weekly_used_pct": (q.get("seven_day") or {}).get("used_percentage"),
            "five_hour_used_pct": (q.get("five_hour") or {}).get("used_percentage"),
            "credit_state": (snap.get("credit") or {}).get("state")}


def inventory(home: Path | None = None, run: Callable[[list[str]], str] | None = _default_run) -> dict:
    """Static prefix drivers plus the config values the lean-defaults work changes. Read-only."""
    home = home or Path.home()
    run = run or (lambda cmd: "")
    oc = _load_json(home / ".openclaw" / "openclaw.json") or {}
    settings = _load_json(home / ".claude" / "settings.json") or {}

    def g(d, *ks):
        return functools.reduce(lambda a, k: a.get(k, {}) if isinstance(a, dict) else {}, ks, d) or None

    agents = (oc.get("agents") or {}).get("entries") or {}
    cmd_md = home / ".claude" / "CLAUDE.md"
    agy_settings = _load_json(home / ".gemini" / "antigravity-cli" / "settings.json")
    mcp = _load_json(home / ".gemini" / "config" / "mcp_config.json") or {}
    mcp_list = run(["claude", "mcp", "list"])
    return {
        "claude_code": {
            "user_skills": len(glob.glob(str(home / ".claude" / "skills" / "*" / "SKILL.md"))),
            "user_agents": len(glob.glob(str(home / ".claude" / "agents" / "*.md"))),
            "global_claude_md_bytes": cmd_md.stat().st_size if cmd_md.exists() else 0,
            "global_claude_md_tokens_est": claude_md_tokens(cmd_md.read_text(encoding="utf-8", errors="replace"))
            if cmd_md.exists() else {"total": 0, "kit_block": 0, "rest": 0, "basis": "bytes/4 (estimate)"},
            "enabled_plugins": settings.get("enabledPlugins"),
            "env": {k: v for k, v in (settings.get("env") or {}).items()
                    if any(s in k for s in ("COMPACT", "AGENT_TEAMS", "CACHE", "TOOL_SEARCH", "1M"))},
            "mcp_servers": mcp_list.count("\n") + 1 if mcp_list else 0,
        },
        "openclaw": {
            "session.reset": g(oc, "session", "reset"),
            "session.resetByType": g(oc, "session", "resetByType"),
            "contextPruning": g(oc, "agents", "defaults", "contextPruning"),
            "compaction": g(oc, "agents", "defaults", "compaction"),
            "thinkingDefault": g(oc, "agents", "defaults", "thinkingDefault"),
            "subagents": g(oc, "agents", "defaults", "subagents"),
            "utilityModel": g(oc, "agents", "defaults", "utilityModel"),
            "mcp_servers": sorted((oc.get("mcp") or {}).get("servers") or {}),
            "slack_implicitMentions": g(oc, "channels", "slack", "implicitMentions"),
            "slack_allowBots": g(oc, "channels", "slack", "allowBots"),
            "workboard_enabled": g(oc, "plugins", "entries", "workboard", "enabled"),
            "agents": {k: {"primary": g(v, "model", "primary"), "fallbacks": g(v, "model", "fallbacks"),
                           "heartbeat": g(v, "heartbeat"), "tools.profile": g(v, "tools", "profile"),
                           "tools.deny": g(v, "tools", "deny"), "thinking": g(v, "thinkingDefault")}
                       for k, v in agents.items() if isinstance(v, dict)},
        },
        "agy": {"settings": agy_settings, "mcp_config_servers": sorted(mcp.get("mcpServers") or {})},
        "billing": _billing_inventory(),
        "kit_version": run(["ai-resources", "--version"]),
    }


def collect(since: datetime, until: datetime, home: Path | None = None) -> tuple[list[audit_sources.Usage], audit_sources.LoadStats]:
    home = home or Path.home()
    roots = sorted(p for p in (home / ".claude" / "projects").glob("*") if p.is_dir()) \
        if (home / ".claude" / "projects").is_dir() else []
    cc = audit_sources.load_claude_jsonl(roots, since, include_subagents=True, until=until)
    oc, stats = audit_sources.load_openclaw_events(home / ".openclaw" / "agents", since, until, providers=("anthropic",))
    return cc + oc, stats


def build_snapshot(label: str, since: datetime, until: datetime, home: Path | None = None,
                   run: Callable[[list[str]], str] | None = _default_run) -> dict:
    usages, stats = collect(since, until, home)
    groups: dict[str, list[audit_sources.Usage]] = defaultdict(list)
    for u in usages:
        groups[u.origin].append(u)
    doc = {
        "label": label, "taken_at": datetime.now(timezone.utc).isoformat(),
        "window": [since.isoformat(), until.isoformat()],
        "method": METHOD,
        "estimate": "API-equivalent; subscription use is not billed per token",
        "all": summarize(usages),
        "by_origin": {k: summarize(v) for k, v in sorted(groups.items())},
        "inventory": inventory(home, run),
        "undecoded_rows": stats.undecoded,
    }
    return doc


PROBE_NOTICE = (
    "--probe runs REAL model calls (one fixed 'Reply with exactly OK' prompt each): "
    "`claude -p` from $HOME (~39k prompt tokens on the 2026-10-22 baseline) and "
    "`openclaw agent --agent main` on a fresh bench session (~24k prompt tokens), and, when "
    "agents.defaults.utilityModel is set, one tiny `openclaw infer model run --local` turn on it. "
    "Each is billed like any other call on its account."
)


def run_probes(label: str, run: Callable[[list[str]], str] = _default_run, say: Callable[[str], None] = print,
               home: Path | None = None) -> dict:
    """Opt-in: the two fixed probes. Never called by tests (they inject `run`)."""
    say(PROBE_NOTICE)
    out: dict = {"taken_at": datetime.now(timezone.utc).isoformat()}
    raw = run(["claude", "-p", "Reply with exactly OK", "--output-format", "json"])
    out["claude_p_hi"] = _probe_usage(raw)
    raw = run(["openclaw", "agent", "--agent", "main", "--session-key", f"agent:main:bench-{label}",
               "-m", "Reply with exactly OK", "--json"])
    out["openclaw_main_fresh"] = _probe_usage(raw)
    out["utility_model"] = _utility_probe(run, home)
    return out


def openclaw_json_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".openclaw" / "openclaw.json"


def _utility_probe(run: Callable[[list[str]], str], home: Path | None = None) -> dict:
    """The configured utilityModel through the local CLI transport (ADR-0014 D5); `{"status": "unset"}` when none."""
    from . import openclaw_utility_probe as up
    doc = _load_json(openclaw_json_path(home)) or {}
    ref = (((doc.get("agents") or {}).get("defaults") or {}).get("utilityModel"))
    if not isinstance(ref, str) or not ref:
        return {"status": "unset"}
    raw = run(["openclaw", *up.command(ref)])
    res = up.classify(ref, 0 if raw else 1, raw)
    return {"ref": ref, "status": res.status, "detail": res.detail}


def _probe_usage(raw: str) -> dict:
    try:
        d = json.loads(raw)
    except ValueError:
        return {"error": (raw or "no output")[:200]}
    u = d.get("usage") or (d.get("result") or {}).get("usage") or {}
    return {"usage": u, "cost_usd": d.get("total_cost_usd") or d.get("cost_usd")}


def resolve(ref: str) -> Path:
    p = Path(ref)
    if p.exists():
        return p
    cand = baselines_dir() / (ref if ref.endswith(".json") else ref + ".json")
    return cand


def _delta(u, v) -> str:
    num = isinstance(u, (int, float)) and isinstance(v, (int, float)) and not isinstance(u, bool)
    return f"{(v - u) / u:+.0%}" if num and u else ""


def compare_rows(a: dict, b: dict) -> list[tuple[str, str, object, object, str]]:
    rows = []
    scopes = ["all"] + sorted(set(a.get("by_origin") or {}) | set(b.get("by_origin") or {}))
    for scope in scopes:
        x = a["all"] if scope == "all" else (a.get("by_origin") or {}).get(scope, {})
        y = b["all"] if scope == "all" else (b.get("by_origin") or {}).get(scope, {})
        for k in METRIC_KEYS:
            u, v = x.get(k), y.get(k)
            rows.append((scope, k, u, v, _delta(u, v)))
    # ADR-0015 S14: the breakdowns of `all` by origin, provider and role. A snapshot taken before them has none: n/a.
    for table in BREAKDOWNS:
        xa, xb = (a.get("all") or {}).get(table), (b.get("all") or {}).get(table)
        for group in sorted(set(xa or {}) | set(xb or {})):
            for field_ in BREAKDOWN_FIELDS:
                u = (xa or {}).get(group, {}).get(field_) if xa is not None else "n/a"
                v = (xb or {}).get(group, {}).get(field_) if xb is not None else "n/a"
                rows.append(("all", f"{table}.{group}.{field_}", u, v, _delta(u, v)))
    return rows


def _flatten(d, prefix=""):
    if isinstance(d, dict) and d:
        for k, v in d.items():
            yield from _flatten(v, f"{prefix}{k}.")
    else:
        yield prefix.rstrip("."), d


def inventory_diff(a: dict, b: dict) -> list[tuple[str, object, object]]:
    fa, fb = dict(_flatten(a.get("inventory") or {})), dict(_flatten(b.get("inventory") or {}))
    return [(k, fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]


def latest_snapshot() -> tuple[str, float] | None:
    """(label, poll-turn cost share) of the newest readable snapshot file, or None. Reads files only."""
    d = baselines_dir()
    if not d.is_dir():
        return None
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        doc = _load_json(f)
        share = ((doc or {}).get("all") or {}).get("share_cost_poll_turns") if isinstance(doc, dict) else None
        if isinstance(share, (int, float)):
            return f.stem, float(share)
    return None


def format_compare(a: dict, b: dict) -> str:
    lines = []
    last = None
    for scope, k, u, v, d in compare_rows(a, b):
        if scope != last:
            lines.append(f"\n## {scope}   ({a.get('label')} -> {b.get('label')})")
            last = scope
        lines.append(f"  {k:36} {str(u):>12} -> {str(v):>12} {d}")
    for doc in (a, b):
        share = (doc.get("all") or {}).get("share_cost_poll_turns")
        if audit.poll_alert(share):
            lines.append(f"\nALERT: poll turns are {share:.0%} of the cost in {doc.get('label')!r} "
                         f"(alert above {audit.poll_share_threshold():.0%}).")
    if a.get("method") != b.get("method") or not a.get("method"):
        lines.append("\nWARNING: the snapshots were not counted with the same method "
                     f"(a: {a.get('method') or 'bench.py or unknown'}; b: {b.get('method') or 'bench.py or unknown'}). "
                     "Re-take the older one with `ai-resources audit snapshot <label> --since ISO --until ISO` over the "
                     "same window before reading the delta.")
    diff = inventory_diff(a, b)
    if diff:
        lines.append("\n## config inventory changes")
        for k, u, v in diff:
            lines.append(f"  {k}: {u!r} -> {v!r}")
    lines.append("\nAPI-equivalent estimates; subscription use is not billed per token.")
    return "\n".join(lines)


def cmd_snapshot(args) -> int:
    until = datetime.fromisoformat(args.until) if getattr(args, "until", None) else datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if getattr(args, "since", None):
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        since = until - timedelta(days=args.days or 7)
    doc = build_snapshot(args.label, since, until)
    if getattr(args, "probe", False):
        doc["probes"] = run_probes(args.label)
    out_dir = baselines_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{args.label}.json"
    path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    print(path)
    keys = ("calls", "cost_usd", "cost_per_active_hour", "first_call_ctx_median", "ctx_p50",
            "share_calls_ctx_gt_200k", "share_cost_poll_turns")
    print(json.dumps({"all": {k: doc["all"].get(k) for k in keys},
                      "by_origin": {o: {k: v.get(k) for k in keys} for o, v in doc["by_origin"].items() if v.get("calls")}},
                     indent=1, default=str))
    if doc["undecoded_rows"]:
        print(f"undecoded rows: {doc['undecoded_rows']} (no zstd decoder available)")
    return 0


def cmd_compare(args) -> int:
    pa, pb = resolve(args.a), resolve(args.b)
    docs = []
    for p in (pa, pb):
        d = _load_json(p)
        if not isinstance(d, dict) or "all" not in d:
            print(f"cannot read snapshot: {p}")
            return 1
        docs.append(d)
    print(format_compare(*docs))
    return 0
