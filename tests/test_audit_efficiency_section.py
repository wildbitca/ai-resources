"""ADR-0014 D2 / AC-5: the billing and quota section of the daily Slack report is a pure formatter over the audit JSON.

The formatter lives in templates/ai-usage/billing_section.py (copied unchanged next to the workspace script
audit-efficiency.py); this file loads it by path and also runs the workspace-style flow end to end with a fake kit CLI."""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from ai_resources import audit, billing

REPO = pathlib.Path(__file__).resolve().parents[1]
PATH = REPO / "templates" / "ai-usage" / "billing_section.py"


def _load():
    spec = importlib.util.spec_from_file_location("billing_section", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bs = _load()


def _block(auth="subscription", five=10.0, week=40.0, credit=None, alerts=None):
    return {"auth_mode": auth,
            "quota": {"state": "known", "five_hour": {"used_percentage": five, "resets_at": 1792850447, "expired": False},
                      "seven_day": {"used_percentage": week, "resets_at": 1792850447, "expired": False}},
            "credit": credit or {"state": "unknown"}, "alerts": alerts or []}


def test_the_threshold_equals_the_audits():
    assert bs.POLL_SHARE_ALERT == audit.POLL_SHARE_ALERT == 0.05


def test_a_quiet_day_shows_mode_quota_and_unknown_credit_and_no_alert():
    lines = bs.billing_section(_block(), 0.01)
    text = "\n".join(lines)
    assert lines[0].startswith("💳 *Facturación y cuota")
    assert "Modo: *suscripción*" in text and "Cuota 5 h: 10% usada" in text and "Semanal: 40% usada" in text
    assert "Crédito: desconocido (sin credits.json)" in text and "reinicia 2026-10-24 14:00 UTC" in text
    assert "🚨" not in text


def test_prepaid_and_an_exhausted_weekly_quota_alert(tmp_path):
    snap = {"auth_mode": "prepaid", "quota": billing.quota(_sidecar(tmp_path, 100.0), now=1792600000),
            "credit": billing.credit(tmp_path / "none")}
    block = {**snap, "alerts": [{"code": a.code, "severity": a.severity, "message": a.message}
                                for a in billing.alerts_for(snap)]}
    text = "\n".join(bs.billing_section(block))
    assert "Modo: *prepago (créditos)*" in text
    assert "🚨 Claude Code se factura con créditos prepagados" in text
    assert "🚨 Cuota semanal de Claude agotada — Weekly Claude quota exhausted (100%)" in text


def _sidecar(tmp_path, week):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 0.0, "resets_at": 1792850447},
                                             "seven_day": {"used_percentage": week, "resets_at": 1792850447}}}))
    return p


def test_a_promo_expiring_within_seven_days_alerts_and_a_distant_one_does_not(tmp_path):
    from datetime import date
    def run(expires):
        p = tmp_path / "credits.json"
        p.write_text(json.dumps({"prepaid_usd": 40, "promo_usd": 5, "promo_expires": expires, "updated": "2026-10-22"}))
        p.chmod(0o600)
        snap = {"auth_mode": "subscription", "quota": {}, "credit": billing.credit(p, date(2026, 10, 22))}
        block = {**snap, "alerts": [{"code": a.code, "severity": a.severity, "message": a.message}
                                    for a in billing.alerts_for(snap)]}
        return "\n".join(bs.billing_section(block))
    soon = run("2026-10-27")
    assert "Crédito: prepago $40.00, promo $5.00 hasta 2026-10-27" in soon and "🚨 El crédito promocional vence pronto" in soon
    assert "vence pronto" not in run("2026-12-31")


def test_a_stale_credit_file_is_marked_and_explained():
    block = _block(credit={"state": "stale", "prepaid_usd": 12.5, "promo_usd": None},
                   alerts=[{"code": "credit_stale", "severity": "info", "message": "credits.json is stale"}])
    text = "\n".join(bs.billing_section(block))
    assert "Crédito: prepago $12.50 (desactualizado)" in text and "ℹ️ credits.json está desactualizado" in text


def test_invalid_credit_and_unknown_alert_codes_degrade_to_text():
    block = _block(credit={"state": "invalid"}, alerts=[{"code": "future_code", "severity": "warn", "message": "Something new"}])
    text = "\n".join(bs.billing_section(block))
    assert "formato inválido" in text and "🚨 Something new" in text


@pytest.mark.parametrize("share,alert", [(0.06, True), (0.05, False), (0.0, False), (None, False), ("x", False), (True, False)])
def test_the_poll_share_alert_is_strictly_above_five_percent(share, alert):
    text = "\n".join(bs.billing_section(_block(), share))
    assert ("turnos de sondeo" in text) is alert
    assert bs.poll_alert(share) is alert


@pytest.mark.parametrize("junk", [None, {}, [], "x", {"quota": "x", "credit": 3, "alerts": "no"}, {"alerts": [1, None, {"code": 5}]}])
def test_missing_or_malformed_input_reads_as_unknown_and_never_raises(junk):
    lines = bs.billing_section(junk)
    assert lines[0].startswith("💳") and any("Modo: *desconocido*" in line for line in lines)


def test_unavailable_is_one_bounded_line():
    (line,) = bs.unavailable("boom " * 100)
    assert line.startswith("💳 *Facturación y cuota:* no disponible") and len(line) < 200


def test_no_identifier_can_reach_the_section(tmp_path, monkeypatch):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"oauthAccount": {"billingType": "prepaid", "emailAddress": "who@example.com",
                                               "accountUuid": "uuid-1234"}}))
    monkeypatch.setattr(billing, "claude_json_path", lambda: cj)
    text = "\n".join(bs.billing_section(audit.billing_report()))
    assert "who@example.com" not in text and "uuid-1234" not in text


def test_the_audit_cli_feeds_the_formatter_end_to_end(tmp_path, monkeypatch, capsys):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"oauthAccount": {"billingType": "prepaid"}}))
    sc = _sidecar(tmp_path, 100.0)
    monkeypatch.setattr(billing, "claude_json_path", lambda: cj)
    monkeypatch.setattr(billing, "sidecar_path", lambda: sc)
    audit.cmd_audit(argparse.Namespace(json=True, billing_only=True, days=1, all_projects=False, subagents=False,
                                       openclaw=False, verbose=False, audit_cmd=None))
    doc = json.loads(capsys.readouterr().out)
    codes = {a["code"] for a in doc["billing"]["alerts"]}
    assert "prepaid" in codes
    text = "\n".join(bs.billing_section(doc["billing"], 0.12))
    assert "Modo: *prepago (créditos)*" in text and "turnos de sondeo son 12%" in text


def test_the_template_is_standard_library_only_and_compiles():
    src = PATH.read_text()
    compile(src, str(PATH), "exec")
    imports = [ln.split()[1].split(".")[0] for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert set(imports) <= {"__future__", "datetime"}
