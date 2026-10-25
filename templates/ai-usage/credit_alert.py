"""Credit-usage alert of the daily AI usage report (balanced credit policy).

Pure and standard-library only, like billing_section.py: it is tested in the ai-resources repo and copied unchanged next
to the workspace script (`render_compact.py` imports it from its own directory). It never prints an identifier.
"""
from __future__ import annotations

import os

DEFAULT_PCT = 80.0
PCT_ENV = "AI_USAGE_CREDIT_ALERT_PCT"


def threshold_pct(env=None) -> float:
    """Alert threshold in percent (default 80); an unreadable or out-of-range value falls back to the default."""
    raw = (env if env is not None else os.environ).get(PCT_ENV)
    try:
        value = float(raw) if raw not in (None, "") else DEFAULT_PCT
    except ValueError:
        return DEFAULT_PCT
    return value if 0 < value <= 100 else DEFAULT_PCT


def credit_alerts(items, pct: float | None = None) -> list[str]:
    """Spanish alert lines for `items` = [(label, used_usd, total_usd)] whose use reached `pct` percent of the credit.

    A total of 0 or less is skipped (no credit configured). 100% or more reads as exhausted."""
    pct = threshold_pct() if pct is None else pct
    out: list[str] = []
    for label, used, total in items:
        if not total or total <= 0:
            continue
        share = float(used) / float(total) * 100.0
        if share >= 100.0:
            out.append(f"🚨 Crédito {label} agotado: ${used:,.2f} de ${total:,.2f} ({share:.0f}%). Lo siguiente se factura de tu bolsillo.")
        elif share >= pct:
            out.append(f"🚨 Crédito {label} al {share:.0f}% (${used:,.2f} de ${total:,.2f}); alerta desde {pct:.0f}%.")
    return out
