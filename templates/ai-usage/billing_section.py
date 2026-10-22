"""Billing and quota section of the daily AI usage report (ai-resources ADR-0014 D2).

A pure formatter: it takes the `billing` block of `ai-resources audit --json --billing-only` (and, optionally, the poll-turn
cost share of the day's snapshot) and returns Slack-ready lines in Spanish, the language of the report. No I/O, no
subprocess, standard library only, so it is tested in the ai-resources repo and copied unchanged next to the workspace
script `audit-efficiency.py` (which imports it from its own directory). It never prints an identifier: the block it reads
holds none.

Alert codes come from `ai_resources.billing` (prepaid, weekly_exhausted, five_hour_high, promo_expiring, credit_low,
credit_stale, credit_invalid, credit_mode) plus `poll_share` added here. An unknown code falls back to its English message.
"""
from __future__ import annotations

from datetime import datetime, timezone

POLL_SHARE_ALERT = 0.05            # keep equal to ai_resources.audit.POLL_SHARE_ALERT (a test compares them)

MODE_LABEL = {"prepaid": "prepago (créditos)", "subscription": "suscripción", "unknown": "desconocido"}

ALERT_ES = {
    "prepaid": "Claude Code se factura con créditos prepagados: cada token gasta dinero.",
    "weekly_exhausted": "Cuota semanal de Claude agotada",
    "five_hour_high": "Cuota de 5 h casi agotada",
    "promo_expiring": "El crédito promocional vence pronto",
    "credit_low": "Saldo prepagado por debajo del umbral",
    "credit_stale": "credits.json está desactualizado; actualízalo para que las alertas de crédito sean fiables.",
    "credit_invalid": "credits.json no tiene el formato documentado; el crédito es desconocido.",
    "credit_mode": "credits.json debería tener permisos 0600.",
}


def _when(w) -> str:
    if not isinstance(w, dict) or not w.get("resets_at"):
        return ""
    return datetime.fromtimestamp(int(w["resets_at"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _quota_line(label: str, w) -> str:
    if not isinstance(w, dict) or w.get("used_percentage") is None:
        return f"{label}: desconocida"
    text = f"{label}: {float(w['used_percentage']):.0f}% usada"
    when = _when(w)
    if when:
        text += f", reinicia {when}"
    if w.get("expired"):
        text += " (la ventana ya se reinició)"
    return text


def _credit_line(c) -> str:
    c = c if isinstance(c, dict) else {}
    if c.get("state") in ("known", "stale"):
        parts = []
        if c.get("prepaid_usd") is not None:
            parts.append(f"prepago ${float(c['prepaid_usd']):,.2f}")
        if c.get("promo_usd") is not None:
            promo = f"promo ${float(c['promo_usd']):,.2f}"
            if c.get("promo_expires"):
                promo += f" hasta {c['promo_expires']}"
            parts.append(promo)
        text = "Crédito: " + (", ".join(parts) or "sin importes")
        return text + (" (desactualizado)" if c.get("state") == "stale" else "")
    if c.get("state") == "invalid":
        return "Crédito: desconocido (credits.json con formato inválido)"
    return "Crédito: desconocido (sin credits.json)"


def _alert_text(alert: dict) -> str:
    code = str(alert.get("code") or "")
    base = ALERT_ES.get(code)
    if base is None:
        return str(alert.get("message") or code)
    if code in ("weekly_exhausted", "five_hour_high", "promo_expiring", "credit_low"):
        # these carry numbers: keep the specifics of the English message after the Spanish lead
        return f"{base} — {alert.get('message', '')}".rstrip(" —")
    return base


def poll_alert(share) -> bool:
    return isinstance(share, (int, float)) and not isinstance(share, bool) and share > POLL_SHARE_ALERT


def billing_section(billing: dict | None, poll_share: float | None = None) -> list[str]:
    """Lines of the section. `billing` is the `billing` block of the audit JSON; anything missing reads as unknown."""
    b = billing if isinstance(billing, dict) else {}
    quota = b.get("quota") if isinstance(b.get("quota"), dict) else {}
    alerts = [a for a in (b.get("alerts") or []) if isinstance(a, dict)]
    out = ["💳 *Facturación y cuota (ai-resources)*",
           f"• Modo: *{MODE_LABEL.get(b.get('auth_mode'), MODE_LABEL['unknown'])}*",
           "• " + _quota_line("Cuota 5 h", quota.get("five_hour")) + "  │  " + _quota_line("Semanal", quota.get("seven_day")),
           "• " + _credit_line(b.get("credit"))]
    for a in alerts:
        out.append(("🚨 " if a.get("severity") == "warn" else "ℹ️ ") + _alert_text(a))
    if poll_alert(poll_share):
        out.append(f"🚨 Los turnos de sondeo son {poll_share:.0%} del costo (alerta sobre {POLL_SHARE_ALERT:.0%}): "
                   "espera con una sola llamada bloqueante o run_in_background, no con bucles de sleep.")
    return out


def unavailable(reason: str) -> list[str]:
    """The one line used when `ai-resources audit --json` cannot be read; never an exception into the report."""
    return [f"💳 *Facturación y cuota:* no disponible ({reason[:120]})."]
