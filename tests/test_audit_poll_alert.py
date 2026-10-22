"""ADR-0014 D7: the audit alerts when poll turns are strictly more than 5% of the cost (threshold overridable by env)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest

from ai_resources import audit, audit_snapshot, audit_sources

UTC = timezone.utc


def _u(msg, poll, out=1_000_000, model="claude-sonnet-5-5", hour=10):
    # claude-sonnet-5-5 output costs $10 per MTok: out=1_000_000 -> $10 per call.
    return audit_sources.Usage(ts=datetime(2026, 10, 20, hour, 0, tzinfo=UTC), source="claude-code", project="p", agent="",
                               model=model, input=0, output=out, cache_read=0, cache_write_5m=0, cache_write_1h=0,
                               msg_id=msg, session="s", origin="claude-code", poll=poll)


def _mix(poll_cost_units, total_units):
    """`total_units` calls of $10 each, `poll_cost_units` of them poll turns."""
    return [_u(f"m{i}", i < poll_cost_units) for i in range(total_units)]


def test_threshold_default_and_strictly_above():
    assert audit.POLL_SHARE_ALERT == 0.05 and audit.poll_share_threshold() == 0.05
    assert audit.poll_alert(0.06) is True
    assert audit.poll_alert(0.05) is False
    assert audit.poll_alert(0.0) is False
    assert audit.poll_alert(None) is False


def test_share_of_the_cost_not_of_the_calls():
    usages = [_u("a", True, out=1_000_000), _u("b", False, out=9_000_000)]       # $10 poll of $100
    assert audit.poll_cost_share(usages) == pytest.approx(0.10)


def test_share_is_zero_without_cost():
    assert audit.poll_cost_share([]) == 0.0
    assert audit.poll_cost_share([_u("a", True, model="unpriced-model-x")]) == 0.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_POLL_SHARE_ALERT", "0.20")
    assert audit.poll_share_threshold() == 0.20
    assert audit.poll_alert(0.15) is False and audit.poll_alert(0.21) is True
    monkeypatch.setenv("AI_RESOURCES_POLL_SHARE_ALERT", "junk")
    assert audit.poll_share_threshold() == 0.05
    monkeypatch.setenv("AI_RESOURCES_POLL_SHARE_ALERT", "-1")
    assert audit.poll_share_threshold() == 0.05


def _project(tmp_path, poll_calls, total_calls):
    d = tmp_path / ".claude" / "projects" / "-work-p"
    d.mkdir(parents=True)
    poll = [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 60"}}]
    lines = []
    for i in range(total_calls):
        lines.append(json.dumps({"timestamp": f"2026-10-20T10:{i:02d}:00Z", "sessionId": "S", "requestId": f"r{i}",
                                 "message": {"id": f"m{i}", "model": "claude-sonnet-5-5",
                                             "content": poll if i < poll_calls else [],
                                             "usage": {"input_tokens": 0, "output_tokens": 1_000_000}}}))
    (d / "S.jsonl").write_text("\n".join(lines) + "\n")
    return d


def _json_report(monkeypatch, capsys, d):
    monkeypatch.setattr(audit, "_project_jsonl_dir", lambda cwd=None: d)
    rc = audit.cmd_audit(argparse.Namespace(json=True, days=0, all_projects=False, subagents=False, openclaw=False,
                                            verbose=False, audit_cmd=None))
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_json_alerts_at_six_percent(tmp_path, monkeypatch, capsys):
    doc = _json_report(monkeypatch, capsys, _project(tmp_path, 3, 50))       # 3 of 50 equal calls = 0.06
    assert doc["poll"]["share"] == pytest.approx(0.06)
    assert doc["poll"]["alert"] is True and doc["poll_alert"] is True
    assert doc["poll"]["threshold"] == 0.05


def test_json_does_not_alert_at_exactly_five_percent(tmp_path, monkeypatch, capsys):
    doc = _json_report(monkeypatch, capsys, _project(tmp_path, 5, 100))      # exactly 0.05
    assert doc["poll"]["share"] == pytest.approx(0.05)
    assert doc["poll"]["alert"] is False and doc["poll_alert"] is False


def test_json_env_override_changes_the_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_RESOURCES_POLL_SHARE_ALERT", "0.10")
    doc = _json_report(monkeypatch, capsys, _project(tmp_path, 3, 50))
    assert doc["poll"]["alert"] is False and doc["poll"]["threshold"] == 0.10


def test_text_report_prints_the_alert(tmp_path, monkeypatch):
    from ai_resources.setup import ui
    d = _project(tmp_path, 3, 50)
    monkeypatch.setattr(audit, "_project_jsonl_dir", lambda cwd=None: d)
    seen = []
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "warn", lambda m: seen.append(m))
    pytest.importorskip("rich")
    from rich.console import Console
    monkeypatch.setattr(ui, "console", lambda: Console(file=open("/dev/null", "w")))
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    audit.cmd_audit(argparse.Namespace(json=False, days=0, all_projects=False, subagents=False, openclaw=False,
                                       verbose=False, audit_cmd=None))
    assert any("Poll turns" in m for m in seen)


def test_compare_flags_a_poll_share_above_the_threshold():
    def snap(label, share):
        return {"label": label, "method": audit_snapshot.METHOD, "all": {"share_cost_poll_turns": share}, "by_origin": {}}
    out = audit_snapshot.format_compare(snap("before", 0.12), snap("after", 0.02))
    assert "ALERT" in out and "before" in out and "12%" in out
    quiet = audit_snapshot.format_compare(snap("a", 0.05), snap("b", 0.01))
    assert "ALERT" not in quiet


def test_latest_snapshot_reads_the_newest_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = audit_snapshot.baselines_dir()
    assert audit_snapshot.latest_snapshot() is None
    d.mkdir(parents=True)
    (d / "old.json").write_text(json.dumps({"all": {"share_cost_poll_turns": 0.5}}))
    new = d / "new.json"
    new.write_text(json.dumps({"all": {"share_cost_poll_turns": 0.01}}))
    import os
    os.utime(d / "old.json", (1, 1))
    assert audit_snapshot.latest_snapshot() == ("new", 0.01)
    (d / "junk.json").write_text("{nope")
    os.utime(d / "junk.json", (9999999999, 9999999999))
    assert audit_snapshot.latest_snapshot() == ("new", 0.01)          # an unreadable file is skipped


def test_doctor_warns_from_the_latest_snapshot(monkeypatch):
    from ai_resources import doctor
    from ai_resources.setup import ui
    warns, oks, details = [], [], []
    monkeypatch.setattr(ui, "warn", warns.append)
    monkeypatch.setattr(ui, "ok", oks.append)
    monkeypatch.setattr(ui, "detail", details.append)
    monkeypatch.setattr(audit_snapshot, "latest_snapshot", lambda: ("w1", 0.12))
    assert doctor._check_poll_share() == 0
    assert warns and "12%" in warns[0] and "sleep" in warns[0]
    warns.clear()
    monkeypatch.setattr(audit_snapshot, "latest_snapshot", lambda: ("w2", 0.01))
    doctor._check_poll_share()
    assert warns == [] and oks
    monkeypatch.setattr(audit_snapshot, "latest_snapshot", lambda: None)
    doctor._check_poll_share()
    assert any("No cost snapshot" in d for d in details)
