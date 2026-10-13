"""Headroom readers: fake fetchers only, no network; a reader never raises (AC-27)."""
from __future__ import annotations

import json
import socket

import pytest

from ai_resources.scoring import db as scores_db
from ai_resources.scoring import headroom as hr
from ai_resources.scoring import headroom_sources as hs

POOLS = {p["id"]: p for p in scores_db.load_shipped()["pools"]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a headroom reader touched the network")
    monkeypatch.setattr(socket, "socket", refuse)


def write(tmp_path, doc, name="sidecar.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
    return p


# --- Claude status-line sidecar -------------------------------------------------------------------
def test_sidecar_takes_the_tighter_window(tmp_path):
    p = write(tmp_path, {"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": 1000},
                                         "seven_day": {"used_percentage": 65.5, "resets_at": 5000}}})
    r = hs.read_claude_sidecar(p, now_epoch=10)
    assert r.state == hr.KNOWN and r.used_fraction == pytest.approx(0.655) and r.resets_at == 5000


def test_sidecar_with_one_window_missing_still_reads_the_other(tmp_path):
    p = write(tmp_path, {"rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": 77}}})
    assert hs.read_claude_sidecar(p).used_fraction == pytest.approx(0.4)


@pytest.mark.parametrize("content", [{}, {"rate_limits": {}}, {"rate_limits": {"five_hour": {}}}, {"rate_limits": 3},
                                     "not json {", []])
def test_sidecar_without_rate_limits_or_unparseable_is_unknown(tmp_path, content):
    r = hs.read_claude_sidecar(write(tmp_path, content))
    assert r.state == hr.UNKNOWN and r.used_fraction is None


def test_missing_sidecar_or_no_path_is_unknown(tmp_path):
    assert hs.read_claude_sidecar(tmp_path / "absent.json").state == hr.UNKNOWN
    assert hs.read_claude_sidecar(None).state == hr.UNKNOWN


def test_the_reader_never_writes(tmp_path):
    p = write(tmp_path, {"rate_limits": {"five_hour": {"used_percentage": 1}}})
    before = p.read_bytes()
    hs.read_claude_sidecar(p)
    assert p.read_bytes() == before and sorted(x.name for x in tmp_path.iterdir()) == ["sidecar.json"]


# --- OpenRouter key -------------------------------------------------------------------------------
def test_openrouter_reads_used_fraction_from_limit_and_remaining():
    r = hs.read_openrouter_key(lambda: {"data": {"limit": 10.0, "limit_remaining": 2.5}})
    assert r.state == hr.KNOWN and r.used_fraction == pytest.approx(0.75)


def test_openrouter_unlimited_key_is_unknown():
    assert hs.read_openrouter_key(lambda: {"data": {"limit": None, "limit_remaining": None}}).state == hr.UNKNOWN


@pytest.mark.parametrize("payload", [{}, {"data": "x"}, {"data": {"limit": 5}}, "text", None, {"data": {"limit": 0, "limit_remaining": 0}}])
def test_openrouter_missing_fields_are_unknown(payload):
    assert hs.read_openrouter_key(lambda: payload).state == hr.UNKNOWN


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionError("down"), ValueError("bad"), KeyError("k")])
def test_openrouter_errors_map_to_unknown_with_the_error_class(exc):
    def boom():
        raise exc
    r = hs.read_openrouter_key(boom)
    assert r.state == hr.UNKNOWN and r.source == f"error:{type(exc).__name__}"


def test_openrouter_without_a_fetcher_is_unknown():
    assert hs.read_openrouter_key(None).state == hr.UNKNOWN


# --- Vertex quotas --------------------------------------------------------------------------------
def test_vertex_quota_reads_usage_over_limit_and_says_it_may_be_inaccurate():
    r = hs.read_vertex_quota(lambda project, model: {"limit": 2000, "usage": 500}, "proj", "anthropic-claude-opus")
    assert r.state == hr.KNOWN and r.used_fraction == pytest.approx(0.25) and "inaccurate" in r.source


def test_vertex_quota_errors_and_gaps_are_unknown():
    def boom(project, model):
        raise TimeoutError
    assert hs.read_vertex_quota(boom, "p", "m").source == "error:TimeoutError"
    assert hs.read_vertex_quota(lambda p, m: {"limit": 10}, "p", "m").state == hr.UNKNOWN
    assert hs.read_vertex_quota(lambda p, m: {"limit": 10, "usage": 1}, "", "m").state == hr.UNKNOWN


# --- kit counters ---------------------------------------------------------------------------------
def test_kit_counters_absent_or_incomplete_is_unknown(tmp_path):
    assert hs.read_kit_counters(tmp_path / "usage-counters.json", "codex-subscription").state == hr.UNKNOWN
    p = write(tmp_path, {"codex-subscription": {"used": 3}}, "usage-counters.json")
    assert hs.read_kit_counters(p, "codex-subscription").state == hr.UNKNOWN
    p = write(tmp_path, {"codex-subscription": {"used": 3, "limit": 12}}, "usage-counters.json")
    assert hs.read_kit_counters(p, "codex-subscription").used_fraction == pytest.approx(0.25)


# --- readers run only for pools the operator opted into --------------------------------------------
def test_nothing_runs_without_opt_in():
    calls = []
    r = hs.read_for_pool(POOLS["openrouter"], False, {"openrouter_fetch": lambda: calls.append(1) or {}})
    assert r.state == hr.UNKNOWN and r.source == "not-requested" and calls == []


def test_opted_in_pool_calls_its_reader_once():
    calls = []
    fetch = lambda: calls.append(1) or {"data": {"limit": 4, "limit_remaining": 1}}
    r = hs.read_for_pool(POOLS["openrouter"], True, {"openrouter_fetch": fetch})
    assert r.state == hr.KNOWN and calls == [1]


@pytest.mark.parametrize("pool_id", ["agy-gemini", "agy-claude-gpt", "codex-subscription", "gemini-api",
                                     "anthropic-api", "deepseek-api", "openai-api"])
def test_pools_without_a_reader_are_always_unknown(pool_id, tmp_path):
    deps = {"kit_counters_path": write(tmp_path, {pool_id: {"used": 1, "limit": 2}}, "c.json"),
            "openrouter_fetch": lambda: {"data": {"limit": 4, "limit_remaining": 1}}}
    r = hs.read_for_pool(POOLS[pool_id], True, deps)
    assert r.state == hr.UNKNOWN


def test_claude_pool_reads_the_sidecar_through_deps(tmp_path):
    p = write(tmp_path, {"rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": 9}}})
    r = hs.read_for_pool(POOLS["claude-subscription"], True, {"claude_sidecar_path": p})
    assert r.state == hr.KNOWN and r.pool_id == "claude-subscription"
