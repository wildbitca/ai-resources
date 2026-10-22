"""ADR-0014 D2: the billing reader is read-only, tolerant of missing or corrupt files, and never prints an identifier."""
from __future__ import annotations

import json
import os
import stat
from datetime import date

import pytest

from ai_resources import billing

TODAY = date(2026, 10, 22)
NOW = 1792600000          # before the resets_at values below


def _claude_json(tmp_path, billing_type="prepaid"):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"oauthAccount": {"billingType": billing_type, "emailAddress": "secret@example.com",
                                              "accountUuid": "1234-uuid"}, "userID": "abc"}))
    return p


def _sidecar(tmp_path, five=0.0, week=100.0, resets=1792850447):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": five, "resets_at": resets},
                                             "seven_day": {"used_percentage": week, "resets_at": resets}}}))
    return p


def _credits(tmp_path, **kw):
    p = tmp_path / "credits.json"
    p.write_text(json.dumps(kw))
    p.chmod(0o600)
    return p


@pytest.mark.parametrize("raw,want", [("prepaid", "prepaid"), ("stripe_subscription", "subscription"),
                                      ("apple_subscription", "subscription"), ("", "unknown")])
def test_auth_mode(tmp_path, raw, want):
    assert billing.auth_mode(_claude_json(tmp_path, raw)) == want


def test_auth_mode_missing_and_corrupt_are_unknown(tmp_path):
    assert billing.auth_mode(tmp_path / "nope.json") == "unknown"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert billing.auth_mode(bad) == "unknown"
    bad.write_text("[]")
    assert billing.auth_mode(bad) == "unknown"


def test_no_identifier_reaches_the_snapshot_or_the_text(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "claude_json_path", lambda: _claude_json(tmp_path))
    snap = billing.snapshot(TODAY, NOW)
    text = json.dumps(snap) + "\n".join(billing.summary_lines(snap)) + json.dumps([a.message for a in billing.alerts_for(snap)])
    assert "secret@example.com" not in text and "1234-uuid" not in text


def test_quota_reads_both_windows(tmp_path):
    q = billing.quota(_sidecar(tmp_path, five=12.5, week=100.0), now=NOW)
    assert q["state"] == "known"
    assert q["five_hour"]["used_percentage"] == 12.5 and q["seven_day"]["used_percentage"] == 100.0
    assert q["seven_day"]["resets_at"] == 1792850447 and q["seven_day"]["expired"] is False


def test_quota_missing_or_corrupt_is_unknown_not_a_crash(tmp_path):
    assert billing.quota(tmp_path / "none.json")["state"] == "unknown"
    bad = tmp_path / "bad.json"
    bad.write_text("garbage")
    assert billing.quota(bad)["state"] == "unknown"
    bad.write_text(json.dumps({"rate_limits": {"five_hour": "x"}}))
    assert billing.quota(bad)["state"] == "unknown"


def test_a_window_that_already_reset_is_flagged_expired_and_raises_no_alert(tmp_path):
    q = billing.quota(_sidecar(tmp_path, week=100.0, resets=1000), now=NOW)
    assert q["seven_day"]["expired"] is True
    snap = {"auth_mode": "subscription", "quota": q, "credit": billing.credit(tmp_path / "none")}
    assert [a.code for a in billing.alerts_for(snap)] == []


def test_credit_unknown_when_absent(tmp_path):
    assert billing.credit(tmp_path / "none.json", TODAY)["state"] == "unknown"


def test_credit_known_stale_invalid(tmp_path):
    known = billing.credit(_credits(tmp_path, prepaid_usd=50, promo_usd=5, promo_expires="2026-12-01", updated="2026-10-20"), TODAY)
    assert known["state"] == "known" and known["prepaid_usd"] == 50 and known["days_to_expiry"] == 40
    stale = billing.credit(_credits(tmp_path, prepaid_usd=50, updated="2026-10-01"), TODAY)
    assert stale["state"] == "stale" and stale["age_days"] == 21
    undated = billing.credit(_credits(tmp_path, prepaid_usd=50), TODAY)
    assert undated["state"] == "stale"
    assert billing.credit(_credits(tmp_path, nothing=1), TODAY)["state"] == "invalid"
    p = tmp_path / "c2.json"
    p.write_text("[1]")
    assert billing.credit(p, TODAY)["state"] == "invalid"


def test_credit_stale_boundary_is_seven_days(tmp_path):
    assert billing.credit(_credits(tmp_path, prepaid_usd=1, updated="2026-10-15"), TODAY)["state"] == "known"
    assert billing.credit(_credits(tmp_path, prepaid_usd=1, updated="2026-10-14"), TODAY)["state"] == "stale"


def test_credit_mode_is_checked(tmp_path):
    p = _credits(tmp_path, prepaid_usd=50, updated="2026-10-22")
    assert billing.credit(p, TODAY)["mode_ok"] is True
    p.chmod(0o644)
    assert billing.credit(p, TODAY)["mode_ok"] is False


def test_prepaid_alert(tmp_path):
    snap = {"auth_mode": "prepaid", "quota": billing.quota(tmp_path / "x"), "credit": billing.credit(tmp_path / "x")}
    assert "prepaid" in [a.code for a in billing.alerts_for(snap)]
    assert any("prepaid" in a.message.lower() for a in billing.alerts_for(snap))


def test_weekly_exhausted_alert_at_100_and_not_at_99(tmp_path):
    def codes(week):
        snap = {"auth_mode": "subscription", "quota": billing.quota(_sidecar(tmp_path, week=week), now=NOW),
                "credit": billing.credit(tmp_path / "x")}
        return [a.code for a in billing.alerts_for(snap)]
    assert "weekly_exhausted" in codes(100.0)
    assert "weekly_exhausted" not in codes(99.0)


def test_five_hour_alert_at_90(tmp_path):
    def codes(five):
        snap = {"auth_mode": "subscription", "quota": billing.quota(_sidecar(tmp_path, five=five, week=1), now=NOW),
                "credit": billing.credit(tmp_path / "x")}
        return [a.code for a in billing.alerts_for(snap)]
    assert "five_hour_high" in codes(90.0)
    assert "five_hour_high" not in codes(89.9)


def test_promo_expiring_within_seven_days(tmp_path):
    def codes(expires):
        c = billing.credit(_credits(tmp_path, prepaid_usd=100, promo_usd=5, promo_expires=expires, updated="2026-10-22"), TODAY)
        return [a.code for a in billing.alerts_for({"auth_mode": "subscription", "quota": {}, "credit": c})]
    assert "promo_expiring" in codes("2026-10-29")
    assert "promo_expiring" not in codes("2026-10-30")
    assert "promo_expiring" in codes("2026-10-01")          # already expired: still worth saying


def test_credit_low_threshold_default_and_env(tmp_path, monkeypatch):
    c = billing.credit(_credits(tmp_path, prepaid_usd=8, updated="2026-10-22"), TODAY)
    snap = {"auth_mode": "subscription", "quota": {}, "credit": c}
    assert "credit_low" in [a.code for a in billing.alerts_for(snap)]
    monkeypatch.setenv(billing.CREDIT_LOW_ENV, "5")
    assert "credit_low" not in [a.code for a in billing.alerts_for(snap)]
    monkeypatch.setenv(billing.CREDIT_LOW_ENV, "junk")
    assert billing.credit_low_threshold() == billing.CREDIT_LOW_DEFAULT_USD


def test_stale_credit_alerts_as_info(tmp_path):
    c = billing.credit(_credits(tmp_path, prepaid_usd=500, updated="2026-09-01"), TODAY)
    found = [a for a in billing.alerts_for({"auth_mode": "subscription", "quota": {}, "credit": c}) if a.code == "credit_stale"]
    assert found and found[0].severity == "info"


def test_summary_lines_say_unknown_for_missing_everything():
    lines = billing.summary_lines({"auth_mode": "unknown", "quota": {"state": "unknown"}, "credit": {"state": "unknown"}})
    assert lines[0] == "auth mode: unknown"
    assert "5h quota: unknown" in lines and "weekly quota: unknown" in lines
    assert any(l.startswith("credit: unknown") for l in lines)


def test_the_isolated_paths_never_point_at_the_real_home():
    from pathlib import Path
    real = Path.home()
    for p in (billing.claude_json_path(), billing.credits_path(), billing.sidecar_path()):
        assert real not in p.parents or "pytest" in str(p)


def test_the_sidecar_is_the_freshest_candidate_and_the_env_override_wins(tmp_path, monkeypatch):
    import os
    old, new = tmp_path / "kit.json", tmp_path / "workspace.json"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.undo()                                     # the conftest isolation: this test wants the real function
    monkeypatch.setattr(billing, "sidecar_candidates", lambda: [old, new])
    monkeypatch.delenv("AI_RESOURCES_CLAUDE_SIDECAR", raising=False)
    assert billing.sidecar_path() == new
    os.utime(old, (3000, 3000))
    assert billing.sidecar_path() == old
    monkeypatch.setattr(billing, "sidecar_candidates", lambda: [tmp_path / "none-a", tmp_path / "none-b"])
    assert billing.sidecar_path() == tmp_path / "none-a"             # nothing exists: the kit's own path
    monkeypatch.setenv("AI_RESOURCES_CLAUDE_SIDECAR", str(new))
    assert billing.sidecar_path() == new


def test_the_workspace_cache_is_one_of_the_candidates(monkeypatch):
    monkeypatch.undo()                                     # the conftest isolation replaces the list
    names = [str(p) for p in billing.sidecar_candidates()]
    assert any(n.endswith("ai-usage/rate-limits-cache.json") for n in names)
