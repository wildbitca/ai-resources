"""OpenClaw usage loader: per-agent DBs, plain and zstd rows, read-only, tolerant of missing data."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_resources import audit_sources

T0 = 1_792_600_000_000


def _event(model="claude-sonnet-5-5", provider="anthropic", resp="msg_1", inp=100, out=10, cr=1000, cw=200, cw1h=0,
           content=None) -> str:
    return json.dumps({"type": "message", "id": "e", "message": {
        "role": "assistant", "provider": provider, "model": model, "responseId": resp,
        "content": content or [],
        "usage": {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw, "cacheWrite1h": cw1h}}})


def _make_db(agents: Path, agent: str, rows: list[tuple]) -> Path:
    d = agents / agent / "agent"
    d.mkdir(parents=True)
    db = d / "openclaw-agent.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table transcript_events (session_id text, seq integer, event_json text, event_zstd blob, "
                "created_at integer, event_utf8_bytes integer, navigation_json text)")
    for r in rows:
        con.execute("insert into transcript_events (session_id, seq, event_json, event_zstd, created_at) values (?,?,?,?,?)", r)
    con.commit()
    con.close()
    return db


def test_plain_rows_are_grouped_by_agent_and_model(tmp_path):
    _make_db(tmp_path, "main", [("s1", 1, _event(resp="a"), None, T0), ("s1", 2, _event(resp="b", model="claude-opus-5-5"), None, T0 + 1)])
    _make_db(tmp_path, "devops", [("s2", 1, _event(resp="c"), None, T0)])
    usages, stats = audit_sources.load_openclaw_events(tmp_path)
    assert stats.databases == 2 and stats.undecoded == 0
    assert sorted((u.agent, u.model) for u in usages) == [
        ("devops", "claude-sonnet-5-5"), ("main", "claude-opus-5-5"), ("main", "claude-sonnet-5-5")]


def test_duplicate_response_ids_count_once(tmp_path):
    _make_db(tmp_path, "main", [("s1", 1, _event(resp="a", out=5), None, T0), ("s1", 2, _event(resp="a", out=9), None, T0 + 1)])
    usages, _ = audit_sources.load_openclaw_events(tmp_path)
    assert [u.output for u in usages] == [9]


def test_zstd_rows_use_the_injected_decoder_and_undecodable_rows_are_counted(tmp_path):
    _make_db(tmp_path, "main", [
        ("s1", 1, None, b"good", T0), ("s1", 2, None, b"bad", T0 + 1), ("s1", 3, _event(resp="plain"), None, T0 + 2)])

    def decoder(blob: bytes):
        return _event(resp="z") if blob == b"good" else None

    usages, stats = audit_sources.load_openclaw_events(tmp_path, decoder=decoder)
    assert len(usages) == 2
    assert stats.undecoded == 1


def test_cache_write_is_split_into_5m_and_1h(tmp_path):
    _make_db(tmp_path, "main", [("s1", 1, _event(cw=300, cw1h=100), None, T0)])
    u = audit_sources.load_openclaw_events(tmp_path)[0][0]
    assert (u.cache_write_5m, u.cache_write_1h) == (200, 100)


def test_provider_filter(tmp_path):
    _make_db(tmp_path, "main", [("s1", 1, _event(resp="a"), None, T0), ("s1", 2, _event(resp="b", provider="agy-cli", model="gemini-4-argon"), None, T0)])
    only_api, _ = audit_sources.load_openclaw_events(tmp_path, providers=("anthropic",))
    assert [u.model for u in only_api] == ["claude-sonnet-5-5"]


def test_missing_agents_dir_is_a_clean_empty_result(tmp_path):
    usages, stats = audit_sources.load_openclaw_events(tmp_path / "nope")
    assert usages == [] and stats.missing is True


def test_the_database_is_opened_read_only(tmp_path, monkeypatch):
    _make_db(tmp_path, "main", [("s1", 1, _event(), None, T0)])
    seen = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        seen.append((str(target), kw.get("uri")))
        return real(target, *a, **kw)

    monkeypatch.setattr(audit_sources.sqlite3, "connect", spy)
    audit_sources.load_openclaw_events(tmp_path)
    assert seen and all("mode=ro" in t and uri for t, uri in seen)


def test_the_shared_state_database_is_never_opened(tmp_path, monkeypatch):
    _make_db(tmp_path, "main", [("s1", 1, _event(), None, T0)])
    state = tmp_path / "state"
    state.mkdir()
    (state / "openclaw.sqlite").write_bytes(b"")
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(audit_sources.sqlite3, "connect", lambda t, *a, **k: (opened.append(str(t)), real(t, *a, **k))[1])
    audit_sources.load_openclaw_events(tmp_path)
    assert not any("state/openclaw.sqlite" in o for o in opened)


def test_poll_turns_are_flagged(tmp_path):
    poll = [{"type": "toolCall", "name": "sessions_yield", "arguments": {}}]
    _make_db(tmp_path, "main", [("s1", 1, _event(content=poll), None, T0)])
    assert audit_sources.load_openclaw_events(tmp_path)[0][0].poll is True


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd CLI not installed")
def test_zstd_cli_decoder_round_trips():
    payload = _event(resp="cli")
    blob = subprocess.run(["zstd", "-c"], input=payload.encode(), capture_output=True, check=True).stdout
    try:
        from compression import zstd  # noqa: F401
        pytest.skip("native decoder present; the CLI fallback is not exercised")
    except ImportError:
        pass
    assert audit_sources.decode_zstd(blob) == payload


def test_events_with_an_all_zero_usage_block_are_not_calls(tmp_path):
    _make_db(tmp_path, "main", [("s1", 1, _event(resp="z", inp=0, out=0, cr=0, cw=0, model="delivery-mirror"), None, T0),
                                ("s1", 2, _event(resp="r"), None, T0 + 1)])
    usages, _ = audit_sources.load_openclaw_events(tmp_path)
    assert [u.msg_id for u in usages] == ["r"]


def test_cli_audit_openclaw_with_no_data_exits_zero_with_a_clean_message(tmp_path, monkeypatch, capsys):
    from ai_resources import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".openclaw").exists() and not (tmp_path / ".claude").exists()
    assert cli.main(["audit", "--openclaw"]) == 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert "No OpenClaw agent databases found" in text
    assert "Traceback" not in text
    # without --openclaw the missing Claude data is still a failure
    assert cli.main(["audit"]) == 1
