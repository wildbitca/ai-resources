"""Billing awareness (ADR-0014 D2): how Claude Code is billed, how much quota is left, what credit is left.

Everything here is read-only and best-effort: a missing, unreadable or corrupt file answers "unknown" and never raises.
Nothing is ever printed that could identify the account: only `oauthAccount.billingType` is read from `~/.claude.json`
(never the email, the tokens or the account ids).

Sources:
- auth mode: `~/.claude.json` `oauthAccount.billingType` (`prepaid`, or a subscription type).
- quota: the statusline rate-limit cache (`~/.cache/ai-resources/claude-rate-limits.json`, or
  `AI_RESOURCES_CLAUDE_SIDECAR`), the same file the lane headroom reader uses.
- credit: a manual file `~/.config/ai-resources/credits.json` (mode 0600, no secrets):
  `{"prepaid_usd": 12.5, "promo_usd": 5, "promo_expires": "YYYY-MM-DD", "updated": "YYYY-MM-DD"}`. No local source holds the
  remaining credit, so absent means "unknown" and an `updated` older than `CREDIT_STALE_DAYS` means "stale".
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

CREDIT_STALE_DAYS = 7
PROMO_WARN_DAYS = 7
WEEKLY_EXHAUSTED_PCT = 100.0
FIVE_HOUR_HIGH_PCT = 90.0
CREDIT_LOW_ENV = "AI_RESOURCES_CREDIT_LOW_USD"
CREDIT_LOW_DEFAULT_USD = 10.0

SUBSCRIPTION = "subscription"
PREPAID = "prepaid"
UNKNOWN = "unknown"


# --- paths (resolved at call time, so a test points them at a scratch HOME) -------------------------------------------

def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def credits_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-resources" / "credits.json"


def sidecar_path() -> Path:
    env = os.environ.get("AI_RESOURCES_CLAUDE_SIDECAR")
    return Path(env) if env else Path.home() / ".cache" / "ai-resources" / "claude-rate-limits.json"


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# --- auth mode ---------------------------------------------------------------------------------------------------------

def auth_mode(path: Path | None = None) -> str:
    """`prepaid`, `subscription` or `unknown`. Reads `oauthAccount.billingType` only."""
    doc = _read_json(path or claude_json_path())
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    raw = acct.get("billingType") if isinstance(acct, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return UNKNOWN
    raw = raw.strip().lower()
    if "prepaid" in raw or raw in ("credit", "credits", "api"):
        return PREPAID
    return SUBSCRIPTION


# --- quota -------------------------------------------------------------------------------------------------------------

def quota(path: Path | None = None, now: float | None = None) -> dict:
    """{"state": "known"|"unknown", "five_hour": {...}|None, "seven_day": {...}|None, "read_at": epoch|None}.

    A window whose `resets_at` already passed is reported with `expired: True` and raises no alert: the cache is older
    than the window it describes."""
    now = time.time() if now is None else now
    p = Path(path) if path else sidecar_path()
    out: dict = {"state": UNKNOWN, "five_hour": None, "seven_day": None, "read_at": None}
    doc = _read_json(p)
    limits = doc.get("rate_limits") if isinstance(doc, dict) else None
    if not isinstance(limits, dict):
        return out
    for name in ("five_hour", "seven_day"):
        w = limits.get(name)
        used = _num(w.get("used_percentage")) if isinstance(w, dict) else None
        if used is None:
            continue
        resets = _num(w.get("resets_at"))
        out[name] = {"used_percentage": used, "resets_at": int(resets) if resets is not None else None,
                     "expired": resets is not None and resets < now}
    if out["five_hour"] or out["seven_day"]:
        out["state"] = "known"
        try:
            out["read_at"] = int(p.stat().st_mtime)
        except OSError:
            pass
    return out


def _reset_text(w: dict | None) -> str:
    if not w or not w.get("resets_at"):
        return ""
    return datetime.fromtimestamp(w["resets_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- credit ------------------------------------------------------------------------------------------------------------

def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def credit(path: Path | None = None, today: date | None = None) -> dict:
    """The manual credits file. state: `unknown` (absent or unreadable), `invalid` (not the documented shape),
    `stale` (`updated` older than CREDIT_STALE_DAYS or missing) or `known`."""
    today = today or date.today()
    p = Path(path) if path else credits_path()
    out: dict = {"state": UNKNOWN, "prepaid_usd": None, "promo_usd": None, "promo_expires": None, "updated": None,
                 "age_days": None, "days_to_expiry": None, "mode_ok": True}
    if not p.is_file():
        return out
    try:
        out["mode_ok"] = (p.stat().st_mode & 0o077) == 0
    except OSError:
        pass
    doc = _read_json(p)
    if not isinstance(doc, dict):
        out["state"] = "invalid"
        return out
    out["prepaid_usd"], out["promo_usd"] = _num(doc.get("prepaid_usd")), _num(doc.get("promo_usd"))
    expires, updated = _date(doc.get("promo_expires")), _date(doc.get("updated"))
    out["promo_expires"] = expires.isoformat() if expires else None
    out["updated"] = updated.isoformat() if updated else None
    if out["prepaid_usd"] is None and out["promo_usd"] is None and expires is None:
        out["state"] = "invalid"
        return out
    if expires:
        out["days_to_expiry"] = (expires - today).days
    if updated:
        out["age_days"] = (today - updated).days
    out["state"] = "stale" if updated is None or (today - updated).days > CREDIT_STALE_DAYS else "known"
    return out


def credit_low_threshold() -> float:
    try:
        return float(os.environ.get(CREDIT_LOW_ENV, CREDIT_LOW_DEFAULT_USD))
    except ValueError:
        return CREDIT_LOW_DEFAULT_USD


# --- alerts ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    code: str
    severity: str          # "warn" | "info"
    message: str


def snapshot(today: date | None = None, now: float | None = None) -> dict:
    """Everything the doctor, the audit and the Slack section show, as plain data (JSON-safe, no identifiers)."""
    return {"auth_mode": auth_mode(), "quota": quota(now=now), "credit": credit(today=today)}


def alerts_for(snap: dict) -> list[Alert]:
    out: list[Alert] = []
    if snap.get("auth_mode") == PREPAID:
        out.append(Alert("prepaid", "warn", "Claude Code is billed from prepaid credits, not a subscription: "
                                            "every token spends money."))
    q = snap.get("quota") or {}
    weekly, five = q.get("seven_day"), q.get("five_hour")
    if weekly and not weekly.get("expired") and weekly["used_percentage"] >= WEEKLY_EXHAUSTED_PCT:
        reset = _reset_text(weekly)
        out.append(Alert("weekly_exhausted", "warn", "Weekly Claude quota exhausted (" + f"{weekly['used_percentage']:.0f}%"
                         + ")" + (f"; resets {reset}" if reset else "") + "."))
    if five and not five.get("expired") and five["used_percentage"] >= FIVE_HOUR_HIGH_PCT:
        reset = _reset_text(five)
        out.append(Alert("five_hour_high", "warn", "Five-hour Claude quota at " + f"{five['used_percentage']:.0f}%"
                         + (f"; resets {reset}" if reset else "") + "."))
    c = snap.get("credit") or {}
    if c.get("state") in ("known", "stale"):
        days = c.get("days_to_expiry")
        if days is not None and days <= PROMO_WARN_DAYS and (c.get("promo_usd") or 0) > 0:
            when = "has expired" if days < 0 else f"expires in {days} day(s)"
            out.append(Alert("promo_expiring", "warn", f"Promo credit of ${c['promo_usd']:.2f} {when} "
                                                       f"({c['promo_expires']})."))
        low = credit_low_threshold()
        if c.get("prepaid_usd") is not None and c["prepaid_usd"] < low:
            out.append(Alert("credit_low", "warn", f"Prepaid balance ${c['prepaid_usd']:.2f} is below ${low:.2f}."))
        if c.get("state") == "stale":
            age = c.get("age_days")
            out.append(Alert("credit_stale", "info", "credits.json is stale"
                             + (f" (updated {age} day(s) ago)" if age is not None else " (no `updated` date)")
                             + "; refresh it to keep the credit alerts accurate."))
    elif c.get("state") == "invalid":
        out.append(Alert("credit_invalid", "info", "credits.json is not in the documented shape; credit is unknown."))
    if c.get("state") != UNKNOWN and not c.get("mode_ok", True):
        out.append(Alert("credit_mode", "info", "credits.json should be mode 0600."))
    return out


def alerts(today: date | None = None, now: float | None = None) -> list[Alert]:
    return alerts_for(snapshot(today, now))


def summary_lines(snap: dict | None = None) -> list[str]:
    """Plain lines for a text report. Never contains an identifier."""
    snap = snap or snapshot()
    q = snap.get("quota") or {}
    lines = [f"auth mode: {snap.get('auth_mode', UNKNOWN)}"]
    for name, label in (("five_hour", "5h quota"), ("seven_day", "weekly quota")):
        w = q.get(name)
        if not w:
            lines.append(f"{label}: unknown")
        else:
            reset = _reset_text(w)
            lines.append(f"{label}: {w['used_percentage']:.0f}% used"
                         + (f", resets {reset}" if reset else "") + (" (window already reset)" if w.get("expired") else ""))
    c = snap.get("credit") or {}
    if c.get("state") in ("known", "stale"):
        parts = []
        if c.get("prepaid_usd") is not None:
            parts.append(f"prepaid ${c['prepaid_usd']:.2f}")
        if c.get("promo_usd") is not None:
            parts.append(f"promo ${c['promo_usd']:.2f}" + (f" until {c['promo_expires']}" if c.get("promo_expires") else ""))
        lines.append("credit: " + ", ".join(parts) + (" (stale)" if c["state"] == "stale" else ""))
    else:
        lines.append("credit: unknown (no ~/.config/ai-resources/credits.json)" if c.get("state") == UNKNOWN
                     else "credit: unknown (credits.json is not in the documented shape)")
    return lines
