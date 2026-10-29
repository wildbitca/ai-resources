"""Metering gaps (Track 4 item 8): archived transcripts, Google in the snapshot, Max double count, zero-usage CLI turns,
acceptance route probes out of the live ledger, the credits window line. Fixtures only; no tool is called."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ai_resources import acceptance as acc
from ai_resources import audit_snapshot, audit_sources, credits, dispatch_ledger

T0 = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
MS = int(T0.timestamp() * 1000)


def _event(resp, provider="anthropic", model="claude-sonnet-5-5", inp=100, out=10, ts="2026-10-20T12:00:00.000Z") -> str:
    return json.dumps({"type": "message", "id": "e", "timestamp": ts, "message": {
        "role": "assistant", "provider": provider, "model": model, "responseId": resp, "content": [],
        "usage": {"input": inp, "output": out, "cacheRead": 0, "cacheWrite": 0, "cacheWrite1h": 0}}})


def _db(agents: Path, agent: str, live=(), archives=(), with_archive_table=True) -> None:
    d = agents / agent / "agent"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "openclaw-agent.sqlite")
    con.execute("create table transcript_events (session_id text, seq integer, event_json text, event_zstd blob, "
                "created_at integer, event_utf8_bytes integer, navigation_json text)")
    if with_archive_table:
        con.execute("create table session_transcript_archives (session_id text, generation text, session_key text, "
                    "reason text, encoding text, archive_blob blob, archive_sha256 text, archive_name text, "
                    "created_at integer, published_at integer)")
    for i, ev in enumerate(live):
        con.execute("insert into transcript_events (session_id, seq, event_json, created_at) values (?,?,?,?)", ("live", i, ev, MS))
    for i, (enc, blob) in enumerate(archives):
        con.execute("insert into session_transcript_archives (session_id, generation, session_key, reason, encoding, "
                    "archive_blob, archive_sha256, archive_name, created_at) values (?,?,?,?,?,?,?,?,?)",
                    (f"arch{i}", "g", "k", "deleted", enc, blob, "0" * 64, f"a{i}.zst", MS + 1000))
    con.commit()
    con.close()


def _jsonl(*events: str) -> bytes:
    return ("\n".join(events) + "\n").encode()


# --- archives ----------------------------------------------------------------------------------------------------

def test_archived_sessions_are_counted_identity_and_zstd(tmp_path):
    _db(tmp_path, "devops", archives=[("identity", _jsonl(_event("i1"))), ("zstd", b"Z")])
    decoder = lambda blob: _jsonl(_event("z1", inp=7)).decode() if blob == b"Z" else None  # noqa: E731
    usages, stats = audit_sources.load_openclaw_events(tmp_path, decoder=decoder)
    assert sorted(u.msg_id for u in usages) == ["i1", "z1"] and stats.undecoded == 0
    assert {u.ts for u in usages} == {T0}                       # the event's own timestamp, not the archive time


def test_an_undecodable_archive_is_counted_not_dropped_silently(tmp_path):
    _db(tmp_path, "devops", archives=[("zstd", b"bad")])
    usages, stats = audit_sources.load_openclaw_events(tmp_path, decoder=lambda b: None)
    assert usages == [] and stats.undecoded == 1


def test_a_response_in_both_live_and_archive_counts_once_and_live_wins(tmp_path):
    _db(tmp_path, "main", live=[_event("same", out=99)], archives=[("identity", _jsonl(_event("same", out=1)))])
    usages, _ = audit_sources.load_openclaw_events(tmp_path)
    assert [u.output for u in usages] == [99]


def test_archive_events_obey_the_window_and_provider_filter(tmp_path):
    old = _event("old", ts="2026-09-01T00:00:00.000Z")
    g = _event("g", provider="google", model="gemini-3.8-flash")
    _db(tmp_path, "main", archives=[("identity", _jsonl(old, g, _event("a")))])
    usages, _ = audit_sources.load_openclaw_events(tmp_path, since=datetime(2026, 10, 1, tzinfo=timezone.utc), providers=("google",))
    assert [u.msg_id for u in usages] == ["g"]


def test_a_db_without_the_archive_table_still_loads(tmp_path):
    _db(tmp_path, "main", live=[_event("a")], with_archive_table=False)
    usages, stats = audit_sources.load_openclaw_events(tmp_path)
    assert len(usages) == 1 and not stats.notes


# --- zero-usage CLI turns ----------------------------------------------------------------------------------------

def _zero(provider) -> str:
    return _event("z", provider=provider, inp=0, out=0)


def test_zero_usage_claude_cli_turns_are_counted_as_undecoded(tmp_path):
    _db(tmp_path, "main", live=[_zero("claude-cli"), _zero("claude-kit")])
    usages, stats = audit_sources.load_openclaw_events(tmp_path)
    assert usages == [] and stats.undecoded == 2


def test_zero_usage_marker_of_another_provider_is_not_counted(tmp_path):      # PRESERVE: mirrors/markers stay silent
    _db(tmp_path, "main", live=[_zero("anthropic"), _zero("openclaw")])
    usages, stats = audit_sources.load_openclaw_events(tmp_path)
    assert usages == [] and stats.undecoded == 0


# --- snapshot providers ------------------------------------------------------------------------------------------

def test_snapshot_collect_includes_google_but_not_claude_cli(tmp_path):
    _db(tmp_path / ".openclaw" / "agents", "main", live=[
        _event("a"), _event("g", provider="google", model="gemini-3.8-flash"), _event("c", provider="claude-cli")])
    usages, _ = audit_snapshot.collect(datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc), tmp_path)
    assert sorted(u.provider for u in usages) == ["anthropic", "google"]


# --- Max double count --------------------------------------------------------------------------------------------

def _use(model, ts, origin="openclaw-cli"):
    return audit_sources.Usage(ts=ts, source="x", project="", agent="", model=model, input=1, output=1, cache_read=0,
                               cache_write_5m=0, cache_write_1h=0, msg_id="m", origin=origin)


def test_cli_events_covered_by_claude_code_records_are_dropped_uncovered_kept():
    jsonl = [_use("claude-sonnet-5-5", T0)]
    near = _use("claude-sonnet-5-5", T0.replace(minute=2))            # 2 min after the Claude Code record: covered
    far = _use("claude-sonnet-5-5", T0.replace(hour=14))              # no Claude Code record near it: counts
    other = _use("claude-haiku-5-5", T0)                              # different model: counts
    kept = credits.drop_covered_cli_events([near, far, other], jsonl)
    assert kept == [far, other]


def test_an_interactive_claude_code_session_does_not_cover_a_cli_event():
    interactive = [_use("claude-sonnet-5-5", T0, origin="claude-code")]
    event = _use("claude-sonnet-5-5", T0.replace(minute=1))
    assert credits.drop_covered_cli_events([event], interactive) == [event]


def test_a_date_suffixed_model_id_still_dedupes_against_the_plain_id():
    jsonl = [_use("claude-sonnet-5-5-20261001", T0)]
    event = _use("anthropic/claude-sonnet-5-5", T0.replace(minute=1))
    assert credits.drop_covered_cli_events([event], jsonl) == []


def test_max_pool_reports_how_many_events_the_dedupe_dropped(tmp_path):
    proj = tmp_path / "proj" / "-p"
    proj.mkdir(parents=True)
    line = {"timestamp": "2026-10-20T12:00:00Z", "sessionId": "s", "entrypoint": "sdk-cli",
            "message": {"id": "msg_1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 1000, "output_tokens": 100}}}
    (proj / "s.jsonl").write_text(json.dumps(line) + "\n")
    _db(tmp_path / "agents", "main", live=[_event("cli-assistant:u", provider="claude-cli", inp=1000, out=100)])
    out = credits.measure({}, now=datetime(2026, 10, 21, tzinfo=timezone.utc), agents=tmp_path / "agents", claude_roots=[proj])
    assert out["claude-max"]["dedupe_dropped"] == 1
    ledger = tmp_path / "credits.json"
    ledger.write_text(json.dumps({"pools": {"claude-max": {"tokens": {"input": 5}, "since": "2026-10-14", "dedupe_dropped": 1}}}))
    assert any("1 claude-cli event(s)" in l for l in credits.lines(ledger))


def test_max_pool_does_not_double_count_a_turn_present_in_both_sources(tmp_path):
    proj = tmp_path / "proj" / "-p"
    proj.mkdir(parents=True)
    line = {"timestamp": "2026-10-20T12:00:00Z", "sessionId": "s", "entrypoint": "sdk-cli",
            "message": {"id": "msg_1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 1000, "output_tokens": 100}}}
    (proj / "s.jsonl").write_text(json.dumps(line) + "\n")
    _db(tmp_path / "agents", "main", live=[_event("cli-assistant:u", provider="claude-cli", inp=1000, out=100)])
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    out = credits.measure({}, now=now, agents=tmp_path / "agents", claude_roots=[proj])
    assert out["claude-max"]["tokens"]["input"] == 1000 and out["claude-max"]["calls"] == 1


# --- credits output ----------------------------------------------------------------------------------------------

def test_credits_lines_print_the_window_start(tmp_path):
    ledger = tmp_path / "credits.json"
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    credits.declare("anthropic-api", 50, on=datetime(2026, 10, 1).date(), path=ledger)
    credits.collect(path=ledger, now=now, agents=tmp_path / "none", claude_roots=[])
    text = "\n".join(credits.lines(ledger))
    assert "window since 2026-10-01" in text and "claude-max" in text and "window since 2026-10-20" in text


def test_credits_lines_print_the_advisory_daily_cap_next_to_the_anthropic_pool(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_RESOURCES_ANTHROPIC_DAILY_CAP_USD", raising=False)
    ledger = tmp_path / "credits.json"
    credits.declare("anthropic-api", 50, on=datetime(2026, 10, 1).date(), path=ledger)
    credits.collect(path=ledger, now=datetime(2026, 10, 27, tzinfo=timezone.utc), agents=tmp_path / "none", claude_roots=[])
    assert "anthropic-api: advisory daily cap $10.00" in "\n".join(credits.lines(ledger))
    monkeypatch.setenv("AI_RESOURCES_ANTHROPIC_DAILY_CAP_USD", "25")
    assert "advisory daily cap $25.00" in "\n".join(credits.lines(ledger))


def test_credits_alert_threshold_is_the_routing_constant():
    from ai_resources.scoring import routing
    assert credits.ALERT_AT == routing.ALERT_AT


# --- acceptance route probe vs the live ledger -------------------------------------------------------------------

def test_route_probe_rows_never_reach_the_live_dispatch_ledger(tmp_path, monkeypatch):
    live_state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(live_state))
    seen = {}

    def probe_dispatch(route, role, env):
        seen["state"] = os.environ["XDG_STATE_HOME"]
        return {"kind": "claude", "models": [f"claude-{acc.routing.claude_alias(route.cell)}-x"],
                "model": route.cell, "pool": route.pool, "tokens_in": 1, "tokens_out": 1}

    monkeypatch.setattr(acc, "_dispatch_once", probe_dispatch)
    monkeypatch.setattr(acc, "_usage", lambda env: {})
    env = acc.Env(cache=tmp_path / "cache", offline=False)
    assert acc._route_case("planner")(env).status == acc.PASS
    assert seen["state"] != str(live_state)                              # the probe ran against a private state dir
    assert os.environ["XDG_STATE_HOME"] == str(live_state)               # and the caller's value is restored
    assert not (live_state / "ai-resources" / "dispatches.jsonl").exists() and dispatch_ledger.load() == []
    assert [r["role"] for r in acc.route_rows(env)] == ["planner"]       # the row is kept, in the private ledger
