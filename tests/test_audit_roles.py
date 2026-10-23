"""Audit by origin, provider and role, and the effort validation table (ADR-0015 S14, acceptance 17)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_resources import audit, audit_snapshot, audit_sources

T0 = 1_792_600_000_000


def jsonl_line(model="claude-sonnet-5-5", msg="m1", inp=100, out=50, ts="2026-10-20T10:00:00Z"):
    return json.dumps({"timestamp": ts, "sessionId": "sess", "message": {
        "id": msg, "model": model, "role": "assistant",
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "content": []}}) + "\n"


def make_claude_project(root: Path) -> Path:
    proj = root / "-home-u-repo"
    sub = proj / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    (proj / "sess-1.jsonl").write_text(jsonl_line(msg="main1", out=10))
    (sub / "agent-aaa.jsonl").write_text(jsonl_line(msg="pl1", out=900) + jsonl_line(msg="pl2", out=700))
    (sub / "agent-aaa.meta.json").write_text(json.dumps({"agentType": "planner", "description": "plan it"}))
    (sub / "agent-bbb.jsonl").write_text(jsonl_line(msg="gp1", out=40))
    (sub / "agent-bbb.meta.json").write_text(json.dumps({"agentType": "general-purpose"}))
    (sub / "agent-ccc.jsonl").write_text(jsonl_line(msg="nometa", out=5))                  # no meta file at all
    (sub / "agent-ddd.jsonl").write_text(jsonl_line(msg="badmeta", out=6))
    (sub / "agent-ddd.meta.json").write_text("{ not json")
    return proj


def event(model="claude-sonnet-5-5", provider="anthropic", resp="r", out=300):
    return json.dumps({"type": "message", "message": {"role": "assistant", "provider": provider, "model": model,
                                                         "responseId": resp, "content": [],
                                                         "usage": {"input": 100, "output": out, "cacheRead": 0, "cacheWrite": 0}}})


def make_openclaw(agents: Path):
    for agent, rows in {"kit-role-verifier": [event(resp="v1", out=800)],
                        "kit-role-architect": [event(resp="a1", out=1200)],
                        "main": [event(resp="m1", out=20, provider="openai", model="gpt-5.5")]}.items():
        d = agents / agent / "agent"
        d.mkdir(parents=True)
        con = sqlite3.connect(d / "openclaw-agent.sqlite")
        con.execute("create table transcript_events (session_id text, seq integer, event_json text, event_zstd blob, "
                    "created_at integer, event_utf8_bytes integer, navigation_json text)")
        for i, ej in enumerate(rows):
            con.execute("insert into transcript_events (session_id, seq, event_json, event_zstd, created_at) values (?,?,?,?,?)",
                        (f"s-{agent}", i, ej, None, T0))
        con.commit()
        con.close()


def test_claude_subagent_roles_come_from_the_meta_file(tmp_path):
    proj = make_claude_project(tmp_path)
    usages = audit_sources.load_claude_jsonl([proj], include_subagents=True)
    by_msg = {u.msg_id.replace("id:", ""): u for u in usages}
    assert by_msg["main1"].role == "main" and by_msg["main1"].agent == "" and by_msg["main1"].origin == "claude-code"
    assert by_msg["pl1"].role == "planner" and by_msg["pl1"].agent == "planner" and by_msg["pl1"].origin == "claude-code/subagent"
    assert by_msg["gp1"].role == "generalPurpose"
    assert by_msg["nometa"].role == "subagent" and by_msg["badmeta"].role == "subagent"
    assert {u.provider for u in usages} == {"anthropic"}


def test_openclaw_roles_and_providers_come_from_the_agent_id_and_the_event(tmp_path):
    make_openclaw(tmp_path)
    usages, _ = audit_sources.load_openclaw_events(tmp_path)
    got = {u.agent: (u.role, u.provider) for u in usages}
    assert got == {"kit-role-verifier": ("verifier", "anthropic"), "kit-role-architect": ("software-architect", "anthropic"),
                   "main": ("main", "openai")}


def test_role_and_provider_helpers():
    assert audit_sources.normalise_role("Explore") == "explore" and audit_sources.normalise_role("") == ""
    assert audit_sources.openclaw_role("kit-role-reviewer-symfony") == "code-reviewer"
    assert audit_sources.openclaw_role("kit-role-general") == "generalPurpose"
    assert audit_sources.openclaw_role("claude") == "claude"
    assert audit_sources.provider_of_model("gemini-4-argon") == "google" and audit_sources.provider_of_model("???", "x") == "x"
    assert audit_sources.provider_of_model("claude-cli/claude-opus-5-5") == "anthropic"


def test_the_snapshot_lists_both_providers_roles_and_origins(tmp_path):
    proj = make_claude_project(tmp_path / "claude")
    make_openclaw(tmp_path / "oc")
    usages = audit_sources.load_claude_jsonl([proj], include_subagents=True)
    oc, _ = audit_sources.load_openclaw_events(tmp_path / "oc")
    s = audit_snapshot.summarize(usages + oc)
    assert set(s["by_provider"]) == {"anthropic", "openai"}
    assert {"planner", "verifier", "software-architect", "main", "generalPurpose", "subagent"} <= set(s["by_role"])
    assert s["by_role"]["planner"]["calls"] == 2 and s["by_role"]["planner"]["output"] == 1600
    assert set(s["by_origin"]) >= {"claude-code", "claude-code/subagent", "openclaw-api", "openclaw-openai"}
    for table in ("by_provider", "by_role", "by_origin"):
        for b in s[table].values():
            assert set(b) == {"calls", "input", "output", "usd", "unpriced_calls"}
    assert s["by_provider"]["openai"]["unpriced_calls"] >= 0


def test_an_old_snapshot_compares_as_n_a_and_a_new_one_as_numbers():
    new_a = {"label": "new", "all": {"calls": 2, "by_role": {"planner": {"calls": 2, "input": 5, "output": 9, "usd": 0.5}}},
             "by_origin": {}, "method": audit_snapshot.METHOD}
    new_b = {"label": "newer", "all": {"calls": 3, "by_role": {"planner": {"calls": 3, "input": 6, "output": 12, "usd": 0.75}}},
             "by_origin": {}, "method": audit_snapshot.METHOD}
    old = {"label": "old", "all": {"calls": 1}, "by_origin": {}, "method": audit_snapshot.METHOD}
    rows = {(scope, k): (u, v, d) for scope, k, u, v, d in audit_snapshot.compare_rows(new_a, new_b)}
    assert rows[("all", "by_role.planner.usd")] == (0.5, 0.75, "+50%")
    vs_old = {(scope, k): (u, v, d) for scope, k, u, v, d in audit_snapshot.compare_rows(old, new_b)}
    assert vs_old[("all", "by_role.planner.usd")] == ("n/a", 0.75, "")
    assert vs_old[("all", "calls")] == (1, 3, "+200%")
    text = audit_snapshot.format_compare(old, new_b)
    assert "by_role.planner.usd" in text and "n/a" in text
    both_old = audit_snapshot.compare_rows(old, old)
    assert not [r for r in both_old if r[1].startswith("by_")]


def test_the_bench_py_shaped_baseline_still_compares(tmp_path):
    bench = {"label": "before", "all": {"calls": 10, "cost_usd": 5.0}, "by_origin": {"claude-code": {"calls": 10}}}
    new = {"label": "after", "all": {"calls": 8, "cost_usd": 3.0, "by_role": {}}, "by_origin": {"claude-code": {"calls": 8}}}
    rows = audit_snapshot.compare_rows(bench, new)
    assert any(r[1] == "cost_usd" and r[4] == "-40%" for r in rows)


def test_effort_validation_lists_effort_calls_and_output_per_call():
    usages = []
    for role, outs in (("planner", [900, 700]), ("explore", [40, 60, 50]), ("verifier", [500])):
        for i, o in enumerate(outs):
            usages.append(audit_sources.Usage(ts=datetime.now(timezone.utc), source="claude-code", project="", agent=role,
                                              model="claude-sonnet-5-5", input=1, output=o, cache_read=0, cache_write_5m=0,
                                              cache_write_1h=0, msg_id=f"{role}{i}", role=role))
    usages.append(audit_sources.Usage(ts=datetime.now(timezone.utc), source="claude-code", project="", agent="", model="m",
                                      input=1, output=1, cache_read=0, cache_write_5m=0, cache_write_1h=0, msg_id="x",
                                      role="main"))
    rows = audit.effort_validation(usages, {"planner": "high", "explore": "low", "verifier": "high"})
    assert rows == [
        {"role": "explore", "effort": "low", "calls": 3, "output_tokens": 150, "output_per_call": 50.0},
        {"role": "planner", "effort": "high", "calls": 2, "output_tokens": 1600, "output_per_call": 800.0},
        {"role": "verifier", "effort": "high", "calls": 1, "output_tokens": 500, "output_per_call": 500.0}]
    assert audit.effort_validation(usages, None)[0]["effort"] == "n/a" and audit.effort_validation([], {}) == []


def test_the_audit_json_carries_roles_providers_and_the_effort_table(tmp_path, monkeypatch, capsys):
    import argparse
    proj = make_claude_project(tmp_path / "claude")
    monkeypatch.setattr(audit, "_project_jsonl_dir", lambda cwd=None: proj)
    monkeypatch.setattr(audit, "billing_report", lambda: {})
    monkeypatch.setattr(audit, "_effort_table", lambda: {"planner": "high"})
    args = argparse.Namespace(json=True, days=0, all_projects=False, subagents=True, openclaw=False, billing_only=False,
                              audit_cmd=None)
    assert audit.cmd_audit_json(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "planner" in doc["all"]["by_role"] and doc["all"]["by_provider"]["anthropic"]["calls"] >= 4
    assert any(r["role"] == "planner" and r["effort"] == "high" for r in doc["effort_validation"])
