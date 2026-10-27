"""Pool usage readings for doctor (ADR-0018). Simulated files only: no network, no real host state."""
from __future__ import annotations

import json

import pytest

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


def _sidecar(tmp_path, doc):
    p = tmp_path / "sidecar.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return p


def test_sidecar_takes_the_tighter_window(tmp_path):
    p = _sidecar(tmp_path, {"rate_limits": {"five_hour": {"used_percentage": 20}, "seven_day": {"used_percentage": 65.5}}})
    assert pool_usage.read_sidecar(p) == (pytest.approx(0.655), "statusline-sidecar")
    p = _sidecar(tmp_path, {"rate_limits": {"five_hour": {"used_percentage": 40}}})
    assert pool_usage.read_sidecar(p)[0] == pytest.approx(0.4)


@pytest.mark.parametrize("content", [{}, {"rate_limits": {}}, {"rate_limits": {"five_hour": {}}}, {"rate_limits": 3},
                                     "not json {", []])
def test_a_sidecar_without_a_window_or_unparseable_is_unknown(tmp_path, content):
    assert pool_usage.read_sidecar(_sidecar(tmp_path, content))[0] is None
    assert pool_usage.read_sidecar(tmp_path / "absent.json")[0] is None and pool_usage.read_sidecar(None)[0] is None


def test_counters_absent_or_incomplete_are_unknown_and_the_reader_never_writes(tmp_path):
    p = tmp_path / "usage-counters.json"
    assert pool_usage.read_counters(p, "agy-gemini") == (None, "kit-counters:absent")
    p.write_text(json.dumps({"agy-gemini": {"used": 3}}))
    assert pool_usage.read_counters(p, "agy-gemini") == (None, "kit-counters:incomplete")
    p.write_text(json.dumps({"agy-gemini": {"used": 3, "limit": 12}}))
    before = p.read_bytes()
    assert pool_usage.read_counters(p, "agy-gemini") == (0.25, "kit-counters")
    assert p.read_bytes() == before and [x.name for x in tmp_path.iterdir()] == ["usage-counters.json"]


def test_sync_caches_writes_what_snapshot_reads(tmp_path, monkeypatch):
    usage = {"claude": {"result": "Current session: 40% used\nCurrent week (all models): 10% used"},
             "agy": {"command": {"data": {"groups": [
                 {"name": "Gemini Models", "buckets": [{"remaining_fraction": 0.7}]},
                 {"name": "Gemini 4 Argon", "buckets": [{"remaining_fraction": 0.2}]},
                 {"name": "Claude and GPT models", "buckets": [{"remaining_fraction": 0.9}]}]}}}}
    monkeypatch.setattr(pool_usage.shutil, "which", lambda n: n)
    monkeypatch.setattr(pool_usage, "_probe", lambda argv, timeout: usage[argv[0]])
    out = pool_usage.sync_caches(tmp_path)
    assert out["errors"] == {} and set(out["updated"]) == {routing.POOL_MAX, routing.POOL_AGY_GEMINI, routing.POOL_AGY_CLAUDE}
    snap = pool_usage.snapshot(env={}, home=tmp_path)
    assert snap[routing.POOL_MAX]["used_fraction"] == pytest.approx(0.4)
    assert snap[routing.POOL_AGY_GEMINI]["used_fraction"] == pytest.approx(0.8)      # the tightest group of the pool
    assert snap[routing.POOL_AGY_CLAUDE]["used_fraction"] == pytest.approx(0.1)


def test_sync_caches_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_usage.shutil, "which", lambda n: n)

    def boom(argv, timeout):
        raise TimeoutError("slow")
    monkeypatch.setattr(pool_usage, "_probe", boom)
    out = pool_usage.sync_caches(tmp_path)
    assert out["updated"] == [] and set(out["errors"]) == {"claude", "agy"}
