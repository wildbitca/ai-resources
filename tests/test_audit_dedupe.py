"""Audit loads each API message once (streamed records are cumulative) and can widen to all projects and subagents."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_resources import audit, audit_sources


def _rec(mid: str, out: int, model: str = "claude-sonnet-5-5", ts: str = "2026-10-20T10:00:00Z", **extra) -> str:
    return json.dumps({"timestamp": ts, "sessionId": "s1", "requestId": "req-" + mid,
                       "message": {"id": mid, "model": model,
                                   "usage": {"input_tokens": 10, "output_tokens": out,
                                             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}},
                       **extra})


def _project(root: Path, name: str, lines: list[str], sub_lines: list[str] | None = None) -> Path:
    proj = root / name
    proj.mkdir(parents=True)
    (proj / "sess1.jsonl").write_text("\n".join(lines) + "\n")
    if sub_lines is not None:
        sd = proj / "sess1" / "subagents"
        sd.mkdir(parents=True)
        (sd / "agent-a.jsonl").write_text("\n".join(sub_lines) + "\n")
    return proj


def test_three_records_with_one_message_id_count_once_and_the_last_wins(tmp_path):
    proj = _project(tmp_path, "p", [_rec("m1", 5), _rec("m1", 20), _rec("m1", 42)])
    usages = audit_sources.load_claude_jsonl([proj])
    assert len(usages) == 1
    assert usages[0].output == 42


def test_request_id_is_the_fallback_key(tmp_path):
    a = json.loads(_rec("x", 5)); a["message"].pop("id")
    b = json.loads(_rec("x", 9)); b["message"].pop("id")
    proj = _project(tmp_path, "p", [json.dumps(a), json.dumps(b)])
    usages = audit_sources.load_claude_jsonl([proj])
    assert [u.output for u in usages] == [9]


def test_parse_totals_use_the_deduplicated_usage(tmp_path):
    proj = _project(tmp_path, "p", [_rec("m1", 5), _rec("m1", 42), _rec("m2", 7)])
    stats, sessions = audit._parse(proj, None)
    assert stats["claude-sonnet-5-5"]["calls"] == 2
    assert stats["claude-sonnet-5-5"]["out"] == 49
    assert len(sessions) == 1


def test_subagents_are_skipped_unless_requested(tmp_path):
    proj = _project(tmp_path, "p", [_rec("m1", 5)], [_rec("sub1", 77)])
    assert len(audit_sources.load_claude_jsonl([proj])) == 1
    both = audit_sources.load_claude_jsonl([proj], include_subagents=True)
    assert sorted(u.output for u in both) == [5, 77]
    assert {u.origin for u in both} == {"claude-code", "claude-code/subagent"}


def test_window_filters_records_by_their_timestamp(tmp_path):
    proj = _project(tmp_path, "p", [_rec("old", 1, ts="2026-10-01T00:00:00Z"), _rec("new", 2, ts="2026-10-21T00:00:00Z")])
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    got = audit_sources.load_claude_jsonl([proj], since=since)
    assert [u.output for u in got] == [2]


def _audit_args(**kw):
    base = dict(days=0, verbose=False, all_projects=False, subagents=False, openclaw=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_all_projects_includes_other_projects_and_subagents_but_default_does_not(tmp_path, monkeypatch, capsys):
    pytest.importorskip("rich")
    root = tmp_path / ".claude" / "projects"
    mine = _project(root, "-work-mine", [_rec("a", 11)], [_rec("sub", 22)])
    _project(root, "-work-other", [_rec("b", 33)])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(audit, "_project_jsonl_dir", lambda: mine)

    assert audit.cmd_audit(_audit_args()) == 0
    default_out = capsys.readouterr().out
    assert "1" in default_out and "API-equivalent" in default_out

    stats_default, _ = audit._parse(mine, None)
    assert stats_default["claude-sonnet-5-5"]["calls"] == 1

    assert audit.cmd_audit(_audit_args(all_projects=True)) == 0
    capsys.readouterr()
    stats_all, _ = audit._parse(audit._all_project_dirs(), None, include_subagents=True)
    assert stats_all["claude-sonnet-5-5"]["calls"] == 3
    assert stats_all["claude-sonnet-5-5"]["out"] == 11 + 22 + 33
