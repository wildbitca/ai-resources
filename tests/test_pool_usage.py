"""Pool usage readings for doctor (ADR-0018). Simulated files only: no network, no real host state."""
from __future__ import annotations

import json

from ai_resources import pool_usage
from ai_resources.scoring import routing


def _home(tmp_path, *, sidecar=None, counters=None, spend=None):
    cache = tmp_path / ".cache" / "ai-resources"
    cache.mkdir(parents=True)
    if sidecar is not None:
        (cache / "claude-rate-limits.json").write_text(json.dumps(sidecar))
    if counters is not None:
        (cache / "usage-counters.json").write_text(json.dumps(counters))
    if spend is not None:
        (cache / "api-spend.json").write_text(json.dumps(spend))
    return tmp_path


def test_missing_data_is_unknown_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_API_SPEND", raising=False)
    snap = pool_usage.snapshot(now_epoch=1_000, env={}, home=tmp_path)
    assert set(snap) == set(routing.POOLS)
    assert all(v["used_fraction"] is None for v in snap.values())
    assert "unknown" in snap[routing.POOL_MAX]["detail"]


def test_known_readings_and_credit_math(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_API_SPEND", raising=False)
    home = _home(tmp_path,
                 sidecar={"capturedAt": "x", "rate_limits": {"five_hour": {"used_percentage": 40.0, "resets_at": 5_000},
                                                              "seven_day": {"used_percentage": 20.0, "resets_at": 9_000}}},
                 counters={routing.POOL_AGY_GEMINI: {"used": 30, "limit": 100}},
                 spend={routing.POOL_ANTHROPIC_API: {"used_usd": 170.0}, routing.POOL_GEMINI_API: {"used_usd": 2.5}})
    snap = pool_usage.snapshot(now_epoch=1_000, env={"GEMINI_API_KEY": "SECRET-VALUE-123"}, home=home)
    assert snap[routing.POOL_AGY_GEMINI]["used_fraction"] == 0.3
    assert snap[routing.POOL_ANTHROPIC_API]["used_fraction"] == 0.85
    assert snap[routing.POOL_GEMINI_API]["used_fraction"] == 0.25
    assert snap[routing.POOL_GEMINI_API]["key_present"] is True
    text = "\n".join(pool_usage.lines(snap))
    assert "at or above 80%" in text            # anthropic credit alert
    assert "SECRET-VALUE-123" not in text and "SECRET-VALUE-123" not in json.dumps(snap)   # never prints a key
    assert "route now chat " in text


def test_credit_env_total_and_zero_credit(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_API_SPEND", raising=False)
    home = _home(tmp_path, spend={routing.POOL_GEMINI_API: {"used_usd": 5}})
    snap = pool_usage.snapshot(env={"GOOGLE_API_CREDITS_USD": "20"}, home=home)
    assert snap[routing.POOL_GEMINI_API]["used_fraction"] == 0.25
    snap = pool_usage.snapshot(env={"GOOGLE_API_CREDITS_USD": "0"}, home=home)
    assert snap[routing.POOL_GEMINI_API]["used_fraction"] is None


def test_corrupt_feed_is_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_API_SPEND", raising=False)
    home = _home(tmp_path)
    (home / ".cache" / "ai-resources" / "api-spend.json").write_text("{not json")
    assert pool_usage.snapshot(env={}, home=home)[routing.POOL_ANTHROPIC_API]["used_fraction"] is None


def test_routing_follows_live_usage(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_API_SPEND", raising=False)
    home = _home(tmp_path, counters={routing.POOL_AGY_GEMINI: {"used": 95, "limit": 100}})
    snap = pool_usage.snapshot(env={}, home=home)
    assert "route now team-read" in "\n".join(pool_usage.lines(snap))
    pool, _ = routing.choose_pool("team-read", pool_usage.usage_map(snap))
    assert pool == routing.POOL_MAX
