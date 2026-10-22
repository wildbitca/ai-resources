"""ADR-0014 S5 / AC-8: the usage index counts MCP, skill, plugin and subagent calls per transcript over a window, cached
by per-transcript mtime so a second build reads only what changed."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ai_resources import usage_index

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _use(tool_id, name, ts, inp=None):
    return json.dumps({"timestamp": ts.isoformat(), "sessionId": "S", "message": {
        "id": "m" + tool_id, "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp or {}}],
        "usage": {"input_tokens": 1, "output_tokens": 1}}})


def _days_ago(n):
    return NOW - timedelta(days=n)


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "-work-a" / "S1" / "subagents").mkdir(parents=True)
    (root / "-work-b").mkdir(parents=True)
    return root


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n")
    return path


def _build(projects, cache, **kw):
    msgs = []
    idx = usage_index.build(roots=[projects], cache_path=cache, say=msgs.append, **kw)
    return idx, msgs


def test_counts_mcp_servers_skills_and_subagents_in_the_window(projects, tmp_path):
    _write(projects / "-work-a" / "S1.jsonl", [
        _use("t1", "mcp__grafana__query_prometheus", _days_ago(2)),
        _use("t2", "mcp__grafana__search_dashboards", _days_ago(3)),
        _use("t3", "mcp__plugin_engram_engram__mem_save", _days_ago(1)),
        _use("t4", "Skill", _days_ago(1), {"skill": "kit-orchestration"}),
        _use("t5", "Skill", _days_ago(1), {"skill": "stripe:best-practices"}),
        _use("t6", "Task", _days_ago(1), {"subagent_type": "code-reviewer"}),
        _use("t7", "Bash", _days_ago(1), {"command": "ls"}),
    ])
    _write(projects / "-work-a" / "S1" / "subagents" / "agent-x.jsonl", [_use("t8", "mcp__supabase-admin__list_tables", _days_ago(5))])
    idx, _ = _build(projects, tmp_path / "idx.json")

    assert idx.by_server(30, NOW) == {"grafana": 2, "plugin_engram_engram": 1, "supabase-admin": 1}
    assert idx.by_skill(30, NOW) == {"kit-orchestration": 1, "stripe:best-practices": 1}
    assert idx.by_agent(30, NOW) == {"code-reviewer": 1}
    assert idx.by_plugin(30, NOW) == {"engram": 1, "stripe": 1}


def test_a_server_with_no_calls_counts_zero_and_is_unused(projects, tmp_path):
    _write(projects / "-work-b" / "S.jsonl", [_use("t1", "mcp__github__list_issues", _days_ago(2))])
    idx, _ = _build(projects, tmp_path / "idx.json")
    counts = idx.by_server(30, NOW)
    assert counts["grafana"] == 0                       # never called: a count of zero, not a KeyError
    assert "grafana" not in counts
    assert idx.unused("server", "grafana", 30, NOW) is True
    assert idx.unused("server", "github", 30, NOW) is False


def test_used_only_31_days_ago_is_unused_in_30_days(projects, tmp_path):
    _write(projects / "-work-b" / "S.jsonl", [_use("t1", "mcp__figma__get_file", _days_ago(31)),
                                              _use("t2", "mcp__sentry__list", _days_ago(29))])
    idx, _ = _build(projects, tmp_path / "idx.json")
    assert idx.unused("server", "figma", 30, NOW) is True
    assert idx.by_server(30, NOW)["figma"] == 0
    assert idx.unused("server", "sentry", 30, NOW) is False
    assert idx.by_server(60, NOW)["figma"] == 1          # still known to the wider window


def test_a_streamed_duplicate_tool_use_is_counted_once(projects, tmp_path):
    line = _use("dup", "mcp__grafana__q", _days_ago(1))
    _write(projects / "-work-b" / "S.jsonl", [line, line, line])
    idx, _ = _build(projects, tmp_path / "idx.json")
    assert idx.by_server(30, NOW)["grafana"] == 1


def test_the_second_build_reads_only_changed_files_and_counts_are_equal(projects, tmp_path, monkeypatch):
    a = _write(projects / "-work-a" / "A.jsonl", [_use("t1", "mcp__grafana__q", _days_ago(1))])
    b = _write(projects / "-work-b" / "B.jsonl", [_use("t2", "mcp__github__q", _days_ago(1))])
    cache = tmp_path / "idx.json"
    first, first_msgs = _build(projects, cache)
    assert first.stats["read"] == 2 and first.stats["cached"] == 0
    first_counts = first.by_server(30, NOW)

    second, _ = _build(projects, cache)
    assert second.stats["read"] == 0 and second.stats["cached"] == 2
    assert second.by_server(30, NOW) == first_counts

    _write(a, [_use("t1", "mcp__grafana__q", _days_ago(1)), _use("t9", "mcp__grafana__r", _days_ago(1))])
    os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 10))
    third, _ = _build(projects, cache)
    assert third.stats["read"] == 1 and third.stats["cached"] == 1
    assert third.by_server(30, NOW) == {"grafana": 2, "github": 1}


def test_a_deleted_transcript_leaves_the_index(projects, tmp_path):
    f = _write(projects / "-work-b" / "B.jsonl", [_use("t1", "mcp__github__q", _days_ago(1))])
    cache = tmp_path / "idx.json"
    _build(projects, cache)
    f.unlink()
    idx, _ = _build(projects, cache)
    assert idx.by_server(30, NOW) == {} and idx.stats["dropped"] == 1


def test_a_progress_message_is_printed_on_the_first_heavy_build_only(projects, tmp_path, monkeypatch):
    for i in range(3):
        _write(projects / "-work-b" / f"S{i}.jsonl", [_use(f"t{i}", "mcp__x__q", _days_ago(1))])
    monkeypatch.setattr(usage_index, "HEAVY_FILES", 2)
    cache = tmp_path / "idx.json"
    _, msgs = _build(projects, cache)
    assert msgs and "first build" in msgs[0].lower() and "3" in msgs[0]
    _, again = _build(projects, cache)
    assert again == []                                     # nothing to read: silent


def test_a_small_first_build_is_silent(projects, tmp_path):
    _write(projects / "-work-b" / "S.jsonl", [_use("t", "mcp__x__q", _days_ago(1))])
    _, msgs = _build(projects, tmp_path / "idx.json")
    assert msgs == []


def test_a_corrupt_cache_is_rebuilt_not_trusted(projects, tmp_path):
    _write(projects / "-work-b" / "S.jsonl", [_use("t", "mcp__x__q", _days_ago(1))])
    cache = tmp_path / "idx.json"
    cache.write_text("{broken")
    idx, _ = _build(projects, cache)
    assert idx.by_server(30, NOW)["x"] == 1 and idx.stats["read"] == 1
    cache.write_text(json.dumps({"version": 999, "files": {"/nope": {"s": 1, "m": 1, "mcp": {"z": {"2026-10-21": 99}}}}}))
    idx2, _ = _build(projects, cache)
    assert "z" not in idx2.by_server(30, NOW)


def test_malformed_lines_are_skipped(projects, tmp_path):
    _write(projects / "-work-b" / "S.jsonl", ["not json tool_use", '{"message": 5, "tool_use": 1}',
                                              _use("t", "mcp__ok__q", _days_ago(1))])
    idx, _ = _build(projects, tmp_path / "idx.json")
    assert idx.by_server(30, NOW) == {"ok": 1}


def test_usage_by_server_is_the_single_interface(projects, tmp_path, monkeypatch):
    _write(projects / "-work-b" / "S.jsonl", [_use("t", "mcp__ok__q", _days_ago(1))])
    monkeypatch.setattr(usage_index, "default_roots", lambda: [projects])
    monkeypatch.setattr(usage_index, "cache_path", lambda: tmp_path / "idx.json")
    assert usage_index.usage_by_server(30, now=NOW, say=lambda m: None) == {"ok": 1}


def test_the_default_cache_path_is_under_the_isolated_cache_dir():
    from pathlib import Path
    p = usage_index.cache_path()
    assert p.name == "usage-index.json" and p.parent.name == "ai-resources"
    assert Path.home() / ".cache" != p.parent.parent or "pytest" in str(p)
