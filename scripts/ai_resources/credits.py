"""Credit ledger: what was declared, what was measured, what is left, per prepaid API pool (ADR-0018 follow-up).

Neither provider exposes a balance API a key can read: Anthropic has none for API keys (the Admin API reports cost
for an organisation, with an admin key the kit never holds) and the Gemini API has none (Google Cloud Billing needs
IAM, not the API key). So the kit keeps its own ledger in `~/.config/ai-resources/credits.json` (0600, no secrets):

    declared   what the operator says the pool was opened with (amount and date), recorded ONCE per pool
    measured   spend computed from the usage the machine itself recorded since that date, priced with the audit's
               list-price table (`audit.PRICES`); an ESTIMATE, not an invoice
    remaining  declared - measured, or None when nothing was declared (`declared: unknown`)

Measured sources:
    anthropic-api   OpenClaw per-agent transcripts, provider `anthropic` (the API-billed agents)
    gemini-api      OpenClaw per-agent transcripts, provider `google`
    claude-max      Claude Code transcripts and the `claude-cli` provider: TOKENS ONLY, no USD (flat subscription)
    agy-*           measured by the Antigravity quota reader (`pool_usage`), not by this ledger

The file is rewritten atomically (temp file in the same directory, fsync, rename), mode 0600. Legacy keys
(`prepaid_usd`, `promo_usd`, `promo_expires`, `updated`) are preserved: `billing.credit()` still reads them.
Measured spend never decreases between runs of the same ledger window: OpenClaw prunes old transcripts, and a pruned
record must not hand credit back.
"""
from __future__ import annotations

import argparse
import json
import os
from . import scratch as _scratch
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .scoring import routing

LEDGER_VERSION = 2
POOLS = (routing.POOL_ANTHROPIC_API, routing.POOL_GEMINI_API)
MAX_POOL = "claude-max"
ENV_DECLARED = {routing.POOL_ANTHROPIC_API: "AI_RESOURCES_CREDIT_ANTHROPIC_USD",
                routing.POOL_GEMINI_API: "AI_RESOURCES_CREDIT_GEMINI_USD"}
OPENCLAW_PROVIDER = {routing.POOL_ANTHROPIC_API: "anthropic", routing.POOL_GEMINI_API: "google"}
ALERT_AT = routing.ALERT_AT
DEFAULT_WINDOW_DAYS = 30           # measured window of a pool that has no declared date
MAX_WINDOW_DAYS = 7                # token window of the Max pool
NO_BALANCE_API = ("no provider balance API is readable with an API key; remaining = declared - measured spend "
                  "(estimated from list prices)")


# --- paths (resolved at call time so a test points them at a scratch HOME) ---------------------------------------

def ledger_path() -> Path:
    from . import billing
    return billing.credits_path()


def agents_dir() -> Path:
    return Path.home() / ".openclaw" / "agents"


def claude_project_roots() -> list[Path]:
    root = Path.home() / ".claude" / "projects"
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


# --- the file ----------------------------------------------------------------------------------------------------

def read_ledger(path: Path | None = None) -> dict:
    """The ledger document; {} when absent or not a JSON object. Never raises."""
    try:
        doc = json.loads(Path(path or ledger_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_ledger(doc: Mapping[str, Any], path: Path | None = None) -> Path:
    """Atomic 0600 write: a reader sees the old file or the new one, never half."""
    p = Path(path or ledger_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _scratch.atomic_tmp(prefix=".credits.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def _pools(doc: Mapping[str, Any]) -> dict[str, dict]:
    pools = doc.get("pools")
    return {k: dict(v) for k, v in pools.items() if isinstance(v, dict)} if isinstance(pools, dict) else {}


def _usd(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


# --- (1) declaring the opening credit ----------------------------------------------------------------------------

def declare(pool: str, usd: float, *, on: date | None = None, path: Path | None = None,
            today: date | None = None) -> dict:
    """Set a pool's opening credit (the explicit `credits set`). Re-declaring starts a new measured window at `on`."""
    if pool not in POOLS:
        raise ValueError(f"unknown pool {pool!r}; choose one of: {', '.join(POOLS)}")
    if _usd(usd) is None:
        raise ValueError("the amount must be a non-negative number of USD")
    today = today or date.today()
    doc = read_ledger(path)
    pools = _pools(doc)
    entry = pools.get(pool, {})
    entry.update(declared_usd=float(usd), declared_on=(on or today).isoformat(), measured_usd=0.0,
                 since=(on or today).isoformat())
    entry.pop("remaining_usd", None)
    pools[pool] = entry
    doc.update(version=LEDGER_VERSION, pools=pools)
    write_ledger(doc, path)
    return entry


def declared_from_env(env: Mapping[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pool, name in ENV_DECLARED.items():
        try:
            v = _usd(float(env[name])) if env.get(name, "").strip() else None
        except ValueError:
            v = None
        if v is not None:
            out[pool] = v
    return out


def record_opening(*, env: Mapping[str, str] | None = None, flags: Mapping[str, float] | None = None,
                   auto: bool = True, ask=None, path: Path | None = None, today: date | None = None) -> dict[str, str]:
    """Setup's one-time step: for each pool WITHOUT a declared amount, record one. Order of authority: explicit flag,
    env var, the amount already in the file (legacy `prepaid_usd` for the Anthropic pool), the operator's answer
    (`ask(pool) -> float | None`, never called with `auto`), else `declared: unknown`. Returns {pool: what happened}.

    A pool that already has a declared amount is left alone (once means once); change it with `credits set`."""
    env = os.environ if env is None else env
    today = today or date.today()
    doc = read_ledger(path)
    pools = _pools(doc)
    from_env = declared_from_env(env)
    report: dict[str, str] = {}
    changed = False
    for pool in POOLS:
        entry = pools.get(pool, {})
        if _usd(entry.get("declared_usd")) is not None:
            report[pool] = "already declared"
            continue
        value, source = None, ""
        if flags and _usd(flags.get(pool)) is not None:
            value, source = float(flags[pool]), "flag"
        elif pool in from_env:
            value, source = from_env[pool], "env"
        elif pool == routing.POOL_ANTHROPIC_API and _usd(doc.get("prepaid_usd")) is not None:
            value, source = float(doc["prepaid_usd"]), "credits.json"
        elif not auto and ask is not None:
            answer = ask(pool)
            if _usd(answer) is not None:
                value, source = float(answer), "operator"
        if value is None:
            if entry.get("declared_usd") is None and "declared_usd" in entry:
                report[pool] = "declared: unknown"
                continue
            entry.update(declared_usd=None, declared_on=None)
            pools[pool] = entry
            report[pool] = "declared: unknown"
            changed = True
            continue
        entry.update(declared_usd=value, declared_on=today.isoformat(), since=today.isoformat(), measured_usd=0.0,
                     declared_source=source)
        pools[pool] = entry
        report[pool] = f"declared ${value:,.2f} ({source})"
        changed = True
    if changed:
        doc.update(version=LEDGER_VERSION, pools=pools)
        write_ledger(doc, path)
    return report


# --- (2) measuring spend ----------------------------------------------------------------------------------------

def _since(entry: Mapping[str, Any], now: datetime) -> datetime:
    d = _date(entry.get("since")) or _date(entry.get("declared_on"))
    if d:
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return now - timedelta(days=DEFAULT_WINDOW_DAYS)


COVER_BEFORE_S = 300.0     # an OpenClaw claude-cli event summarises work Claude Code logged up to this long before it
COVER_AFTER_S = 60.0


def drop_covered_cli_events(events: list, claude_records: list) -> list:
    """The OpenClaw `claude-cli` events whose spend Claude Code's own transcripts already hold.

    The claude-cli provider runs `claude -p` (entrypoint sdk-cli), so every such turn is written TWICE: per API message
    in ~/.claude/projects/*.jsonl and once per turn in the OpenClaw DB. The ids never match (OpenClaw's responseId is
    `cli-assistant:<uuid>`, Claude Code's is the API `msg_...`), so a shared-id dedupe is impossible. An event is covered
    when a Claude Code record of the same model sits in [event - 300 s, event + 60 s]; on the 2026-10-29 host copy all 279
    events in 7 days were covered. An uncovered event (no Claude Code transcript, e.g. pruned) still counts."""
    import bisect
    by_model: dict[str, list[float]] = {}
    for r in claude_records:
        by_model.setdefault(r.model, []).append(r.ts.timestamp())
    for stamps in by_model.values():
        stamps.sort()
    kept = []
    for e in events:
        stamps, t = by_model.get(e.model, []), e.ts.timestamp()
        i = bisect.bisect_left(stamps, t - COVER_BEFORE_S)
        if not (i < len(stamps) and stamps[i] <= t + COVER_AFTER_S):
            kept.append(e)
    return kept


def measure(doc: Mapping[str, Any], *, now: datetime | None = None, agents: Path | None = None,
            claude_roots: list[Path] | None = None) -> dict[str, dict]:
    """Per pool: {measured_usd, calls, tokens, unpriced, source} from the machine's own usage records. Read-only."""
    from . import audit, audit_sources
    now = now or datetime.now(timezone.utc)
    pools = _pools(doc)
    out: dict[str, dict] = {}
    adir = Path(agents) if agents is not None else agents_dir()
    for pool in POOLS:
        since = _since(pools.get(pool, {}), now)
        recs, stats = audit_sources.load_openclaw_events(adir, since=since, until=now + timedelta(days=1),
                                                         providers=(OPENCLAW_PROVIDER[pool],))
        usd, unpriced, tok = 0.0, set(), {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        for r in recs:
            if audit._price(r.model, ctx=r.input + r.cache_read + r.cache_write, on=r.ts.date()) is None:
                unpriced.add(r.model)
                continue
            usd += audit._estimate(r.model, r.input, r.output, r.cache_read, r.cache_write_5m, r.cache_write_1h,
                                   on=r.ts.date())
            tok["input"] += r.input
            tok["output"] += r.output
            tok["cache_read"] += r.cache_read
            tok["cache_write"] += r.cache_write
        out[pool] = {"measured_usd": round(usd, 4), "calls": len(recs), "tokens": tok, "since": since.date().isoformat(),
                     "unpriced_models": sorted(unpriced), "source": "openclaw-transcripts",
                     "undecoded": stats.undecoded, "readable": not stats.missing}
    # Max subscription: tokens only (flat fee, no per-token USD exists).
    since = now - timedelta(days=MAX_WINDOW_DAYS)
    roots = claude_roots if claude_roots is not None else claude_project_roots()
    recs = audit_sources.load_claude_jsonl(roots, since=since, include_subagents=True, until=now + timedelta(days=1)) if roots else []
    cli, cli_stats = audit_sources.load_openclaw_events(adir, since=since, until=now + timedelta(days=1),
                                                        providers=("claude-cli", "claude-kit"))
    cli = drop_covered_cli_events(cli, recs)
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for r in [*recs, *cli]:                  # Claude Code transcripts and the OpenClaw claude-cli provider: both Max
        tok["input"] += r.input
        tok["output"] += r.output
        tok["cache_read"] += r.cache_read
        tok["cache_write"] += r.cache_write
    out[MAX_POOL] = {"measured_usd": None, "calls": len(recs) + len(cli), "tokens": tok, "since": since.date().isoformat(),
                     "source": "claude-code-transcripts+claude-cli", "unpriced_models": [],
                     "undecoded": cli_stats.undecoded}
    return out


def collect(*, path: Path | None = None, now: datetime | None = None, agents: Path | None = None,
            claude_roots: list[Path] | None = None, write: bool = True) -> dict:
    """Measure, compute remaining = declared - measured, write the ledger atomically. Returns the document."""
    now = now or datetime.now(timezone.utc)
    doc = read_ledger(path)
    pools = _pools(doc)
    measured = measure(doc, now=now, agents=agents, claude_roots=claude_roots)
    for pool in POOLS:
        entry, m = pools.get(pool, {}), measured[pool]
        prev = _usd(entry.get("measured_usd")) if entry.get("since") == m["since"] else None
        usd = max(m["measured_usd"], prev or 0.0)            # pruned transcripts never hand credit back
        declared = _usd(entry.get("declared_usd"))
        entry.update(measured_usd=round(usd, 4), since=m["since"], calls=m["calls"], tokens=m["tokens"],
                     unpriced_models=m["unpriced_models"], source=m["source"], balance_api="none",
                     remaining_usd=round(declared - usd, 4) if declared is not None else None)
        entry.setdefault("declared_usd", None)
        entry.setdefault("declared_on", None)
        pools[pool] = entry
    pools[MAX_POOL] = {"measured_usd": None, "usd": None, "tokens": measured[MAX_POOL]["tokens"],
                       "calls": measured[MAX_POOL]["calls"], "since": measured[MAX_POOL]["since"],
                       "source": measured[MAX_POOL]["source"], "note": "flat subscription: tokens only, no USD"}
    doc.update(version=LEDGER_VERSION, pools=pools, updated=now.date().isoformat(),
               updated_at=now.replace(microsecond=0).isoformat(), note=NO_BALANCE_API)
    if write:
        write_ledger(doc, path)
    return doc


# --- (3) reading it back: summary lines and the 80% alert ------------------------------------------------------

def used_fraction(entry: Mapping[str, Any]) -> float | None:
    declared, used = _usd(entry.get("declared_usd")), _usd(entry.get("measured_usd"))
    if declared is None or used is None or declared <= 0:
        return None
    return min(used / declared, 1.0)


def pool_summary(path: Path | None = None) -> dict[str, dict]:
    """{pool: entry} for the two credit pools, from the file; {} when there is no ledger yet."""
    pools = _pools(read_ledger(path))
    return {p: pools[p] for p in POOLS if p in pools}


def lines(path: Path | None = None) -> list[str]:
    doc = read_ledger(path)
    pools = _pools(doc)
    if not pools:
        return ["credit ledger: none yet (`ai-resources credits set <pool> <usd>`, then `ai-resources credits collect`)"]
    out = []
    for pool in POOLS:
        e = pools.get(pool)
        if e is None:
            continue
        declared, used, rem = _usd(e.get("declared_usd")), _usd(e.get("measured_usd")), e.get("remaining_usd")
        if declared is None:
            out.append(f"{pool}: declared: unknown; measured ${used or 0:,.2f} since {e.get('since') or 'n/a'} "
                       f"(estimate; set the opening credit with `ai-resources credits set {pool} <usd>`)")
        else:
            out.append(f"{pool}: declared ${declared:,.2f} on {e.get('declared_on')}, measured ${used or 0:,.2f} "
                       f"(window since {e.get('since') or 'n/a'}), "
                       f"remaining ${rem if rem is not None else declared - (used or 0):,.2f} (estimate)")
        if pool == routing.POOL_ANTHROPIC_API:       # fallback pool: advisory ceiling only, never enforced
            out.append(f"{pool}: advisory daily cap ${routing.anthropic_daily_cap_usd():,.2f}")
        if e.get("unpriced_models"):
            out.append(f"{pool}: unpriced models (not counted): {', '.join(e['unpriced_models'])}")
    mx = pools.get(MAX_POOL)
    if mx:
        t = mx.get("tokens") or {}
        out.append(f"{MAX_POOL}: {sum(t.values()):,} tokens in the last {MAX_WINDOW_DAYS} days, window since {mx.get('since') or 'n/a'} (subscription: tokens only, no USD)")
    out.append(f"ledger updated: {doc.get('updated', 'n/a')}; {NO_BALANCE_API}")
    return out


def alerts_from(pools: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """(code, severity, message) for each pool at or above 80% of its declared credit."""
    found = []
    for pool, e in (pools or {}).items():
        frac = used_fraction(e)
        if frac is None:
            continue
        if frac >= 1.0:
            found.append(("credit_pool_exhausted", "warn",
                          f"{pool}: the declared credit ${e['declared_usd']:,.2f} is spent (measured ${e['measured_usd']:,.2f})."))
        elif frac >= ALERT_AT:
            found.append(("credit_pool_80", "warn",
                          f"{pool}: {frac * 100:.0f}% of the declared ${e['declared_usd']:,.2f} credit is spent "
                          f"(${e['measured_usd']:,.2f} measured, ${e['declared_usd'] - e['measured_usd']:,.2f} left)."))
    return found


def alerts(path: Path | None = None) -> list[tuple[str, str, str]]:
    return alerts_from(pool_summary(path))


# --- CLI ---------------------------------------------------------------------------------------------------------

def cmd_credits(args: argparse.Namespace) -> int:
    action = getattr(args, "credits_action", None) or "show"
    if action == "set":
        try:
            entry = declare(args.pool, float(args.usd), on=_date(args.on) if args.on else None)
        except ValueError as e:
            print(f"credits: {e}")
            return 2
        print(f"{args.pool}: declared ${entry['declared_usd']:,.2f} on {entry['declared_on']}; "
              "run `ai-resources credits collect` to refresh the measured spend")
        return 0
    if action == "collect":
        doc = collect()
        if not getattr(args, "quiet", False):
            print("\n".join(lines()))
        found = alerts()
        for _code, _sev, msg in found:
            print(f"ALERT {msg}")
        return 3 if (getattr(args, "check", False) and found) else 0
    print("\n".join(lines()))
    for _code, _sev, msg in alerts():
        print(f"ALERT {msg}")
    return 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("credits", help="Credit ledger for the prepaid API pools: declare, collect measured spend, show")
    p.set_defaults(func=cmd_credits, credits_action="show")
    s = p.add_subparsers(dest="credits_action", metavar="ACTION")
    s.add_parser("show", help="Print the ledger and any 80% alert")
    c = s.add_parser("collect", help="Measure spend from local usage records and rewrite the ledger")
    c.add_argument("--check", action="store_true", help="Exit 3 when a pool is at or above 80%% of its declared credit")
    c.add_argument("--quiet", action="store_true", help="Print alerts only")
    st = s.add_parser("set", help="Declare a pool's opening credit (starts a new measured window)")
    st.add_argument("pool", choices=POOLS)
    st.add_argument("usd", type=float, help="Opening credit in USD")
    st.add_argument("--on", metavar="YYYY-MM-DD", help="Date the credit was opened (default: today)")
