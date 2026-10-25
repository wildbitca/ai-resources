"""80% credit alert of the usage report (templates/ai-usage/credit_alert.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "templates" / "ai-usage" / "credit_alert.py"
spec = importlib.util.spec_from_file_location("credit_alert", PATH)
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)


def test_below_threshold_is_silent():
    assert ca.credit_alerts([("Anthropic", 159.0, 200.0)], 80) == []


def test_at_threshold_alerts_with_percent_and_amounts():
    out = ca.credit_alerts([("Anthropic", 160.0, 200.0)], 80)
    assert len(out) == 1 and "80%" in out[0] and "$160.00" in out[0] and "$200.00" in out[0]


def test_exhausted_is_a_distinct_message():
    out = ca.credit_alerts([("Google Cloud", 12.0, 10.0)], 80)
    assert "agotado" in out[0]


def test_zero_or_missing_total_is_skipped():
    assert ca.credit_alerts([("Google Cloud", 5.0, 0), ("X", 1.0, None)], 80) == []


def test_threshold_env_default_and_bad_values():
    assert ca.threshold_pct({}) == 80.0
    assert ca.threshold_pct({"AI_USAGE_CREDIT_ALERT_PCT": "90"}) == 90.0
    for bad in ("abc", "0", "150", "-5"):
        assert ca.threshold_pct({"AI_USAGE_CREDIT_ALERT_PCT": bad}) == 80.0


def test_env_threshold_drives_default(monkeypatch):
    monkeypatch.setenv("AI_USAGE_CREDIT_ALERT_PCT", "50")
    assert ca.credit_alerts([("A", 100.0, 200.0)])
