"""audit snapshot/compare (AC-20): fixture transcripts reproduce the bench.py numbers; compare prints old, new, delta."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_resources import audit_snapshot

SINCE = datetime(2026, 10, 20, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _line(mid, ts, sess, model="claude-sonnet-5-5", inp=1000, out=100, cr=0, cw=0, content=None, entry=None):
    d = {"timestamp": ts, "sessionId": sess, "requestId": "r" + mid,
         "message": {"id": mid, "model": model, "content": content or [],
                     "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw}}}
    if entry:
        d["entrypoint"] = entry
    return json.dumps(d)


@pytest.fixture
def home(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-work-p"
    proj.mkdir(parents=True)
    poll = [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 30"}}]
    lines = [
        # session A: first call ctx 1000+0+0 = 1000, second 1000+9000+0 = 10000
        _line("a1", "2026-10-20T10:05:00Z", "A", inp=1000, out=100),
        _line("a2", "2026-10-20T10:20:00Z", "A", inp=1000, out=100, cr=9000),
        # session B in another hour: one big call (ctx 300000), a poll turn
        _line("b1", "2026-10-20T11:05:00Z", "B", inp=1000, out=200, cr=299000, content=poll),
        # duplicate streamed record of b1 must not double count
        _line("b1", "2026-10-20T11:05:01Z", "B", inp=1000, out=200, cr=299000, content=poll),
    ]
    (proj / "A.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path


FIXTURE = Path(__file__).parent / "fixtures" / "audit_snapshot"
RATE = {"in": 2.0, "out": 10.0, "cr": 0.10, "cw": 2.50}      # claude-sonnet-5-5 per MTok (official pricing, 2026-10-22)


def _reference(since, until):
    """Independent re-statement of the metric definitions, computed from the raw fixture lines (not from kit code).

    Definitions: one call per distinct message id (LAST record wins), kept when its timestamp is in [since, until);
    ctx = input + cache read + cache write; active hours = distinct UTC hours; a poll turn calls Bash with `sleep`."""
    calls: dict[str, dict] = {}
    for f in sorted((FIXTURE / "projects").rglob("*.jsonl")):
        sess_of_file = str(f) if "subagents" in f.parts else None
        for raw in f.read_text().splitlines():
            d = json.loads(raw)
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
            if not (since <= ts < until):
                continue
            m, u = d["message"], d["message"]["usage"]
            calls[m["id"]] = {
                "ts": ts, "session": sess_of_file or d["sessionId"], "inp": u["input_tokens"], "out": u["output_tokens"],
                "cr": u["cache_read_input_tokens"], "cw": u["cache_creation_input_tokens"],
                "poll": any("sleep " in json.dumps(c.get("input")) for c in m["content"] if c["type"] == "tool_use")}
    rows = list(calls.values())
    for r in rows:
        r["ctx"] = r["inp"] + r["cr"] + r["cw"]
        r["cost"] = (r["inp"] * RATE["in"] + r["out"] * RATE["out"] + r["cr"] * RATE["cr"] + r["cw"] * RATE["cw"]) / 1e6
    return rows


def test_snapshot_matches_an_independent_computation_over_fixture_transcripts(tmp_path):
    import shutil
    import statistics as st
    home = tmp_path
    shutil.copytree(FIXTURE / "projects", home / ".claude" / "projects")
    rows = _reference(SINCE, UNTIL)
    assert len(rows) == 4, "fixture sanity: 2 duplicates collapse, the out-of-window records are excluded"
    doc = audit_snapshot.build_snapshot("t", SINCE, UNTIL, home=home, run=None)
    a = doc["all"]
    total = sum(r["cost"] for r in rows)
    sessions = {r["session"] for r in rows}
    hours = {r["ts"].strftime("%Y-%m-%dT%H") for r in rows}
    firsts = [min((r for r in rows if r["session"] == s), key=lambda r: r["ts"])["ctx"] for s in sessions]
    cr_total = sum(r["cr"] for r in rows)
    ctxs = sorted(r["ctx"] for r in rows)
    big = [r for r in rows if r["ctx"] > 200_000]
    assert a["calls"] == len(rows) and a["sessions"] == len(sessions) and a["active_hours"] == len(hours)
    assert a["cost_usd"] == round(total, 2)
    assert a["cost_per_active_hour"] == round(total / len(hours), 3)
    assert a["cost_per_session"] == round(total / len(sessions), 4)
    assert a["cost_per_call"] == round(total / len(rows), 5)
    assert a["first_call_ctx_median"] == int(st.median(firsts))
    assert a["ctx_p50"] == int(st.quantiles(ctxs, n=10)[4]) and a["ctx_p90"] == int(st.quantiles(ctxs, n=10)[8])
    assert a["share_calls_ctx_gt_200k"] == round(len(big) / len(rows), 3)
    assert a["share_cache_read_from_ctx_gt_200k"] == round(sum(r["cr"] for r in big) / cr_total, 3)
    assert a["share_cost_poll_turns"] == round(sum(r["cost"] for r in rows if r["poll"]) / total, 3)
    assert a["cache_hit_ratio"] == round(cr_total / sum(r["ctx"] for r in rows), 3)
    # dedupe: the streamed a1 counts once and the LAST record (out=100, not 10) is the one priced
    a1_last = (1000 * 2 + 100 * 10) / 1e6
    assert any(abs(r["cost"] - a1_last) < 1e-12 for r in rows)
    # origins: the subagent transcript is its own origin
    assert set(doc["by_origin"]) == {"claude-code", "claude-code/subagent"}
    assert doc["by_origin"]["claude-code/subagent"]["calls"] == 1


def test_a_past_window_can_be_re_snapshotted_with_since_and_until(tmp_path, monkeypatch):
    """A 'before' baseline is regenerated for any past window with the kit's own method (no bench.py needed)."""
    import shutil
    shutil.copytree(FIXTURE / "projects", tmp_path / ".claude" / "projects")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(audit_snapshot, "_default_run", lambda cmd: "")

    def take(label, since, until):
        ns = argparse.Namespace(label=label, days=None, since=since, until=until, probe=False)
        assert audit_snapshot.cmd_snapshot(ns) == 0
        return json.loads((tmp_path / "state" / "ai-resources" / "cost-baselines" / f"{label}.json").read_text())

    old = take("old", "2026-10-09", "2026-10-11")
    mid = take("mid", "2026-10-20", "2026-10-21")
    assert old["all"]["calls"] == 1 and mid["all"]["calls"] == 4      # mid: a1 (deduped), a2, b1 and the subagent call
    assert old["window"][0].startswith("2026-10-09") and old["method"] == audit_snapshot.METHOD


def test_compare_warns_when_a_snapshot_was_not_taken_with_the_kit_method():
    kit = {**_snap("after"), "method": audit_snapshot.METHOD}
    bench = _snap("before")                                          # bench.py baselines carry no `method`
    assert "Re-take" in audit_snapshot.format_compare(bench, kit)
    assert "Re-take" not in audit_snapshot.format_compare(kit, kit)


def test_snapshot_inventory_records_the_keys_the_plan_changes(home):
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}}))
    (home / ".openclaw").mkdir()
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "session": {"reset": {"mode": "idle"}}, "agents": {"defaults": {"thinkingDefault": "low"},
                                                           "entries": {"main": {"heartbeat": {"every": "2h"}}}}}))
    inv = audit_snapshot.inventory(home, run=None)
    assert inv["claude_code"]["env"] == {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
    assert inv["openclaw"]["session.reset"] == {"mode": "idle"}
    assert inv["openclaw"]["thinkingDefault"] == "low"
    assert inv["openclaw"]["agents"]["main"]["heartbeat"] == {"every": "2h"}


def test_snapshot_writes_to_the_state_dir_and_compare_reads_labels(home, tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(audit_snapshot, "_default_run", lambda cmd: "")
    args = argparse.Namespace(label="before", days=None, since=SINCE.isoformat(), until=UNTIL.isoformat(), probe=False)
    assert audit_snapshot.cmd_snapshot(args) == 0
    path = state / "ai-resources" / "cost-baselines" / "before.json"
    assert path.exists()
    capsys.readouterr()
    assert audit_snapshot.cmd_compare(argparse.Namespace(a="before", b=str(path))) == 0
    out = capsys.readouterr().out
    assert "cost_per_active_hour" in out and "+0%" in out


def _snap(label, **over):
    base = {"calls": 100, "sessions": 10, "active_hours": 5, "cost_usd": 50.0, "cost_per_active_hour": 10.0,
            "cost_per_session": 5.0, "cost_per_call": 0.5, "calls_per_session_median": 10, "first_call_ctx_median": 80000,
            "ctx_p50": 160000, "ctx_p90": 400000, "share_calls_ctx_gt_200k": 0.4, "share_cache_read_from_ctx_gt_200k": 0.7,
            "share_cost_poll_turns": 0.08, "cache_hit_ratio": 0.95}
    base.update(over)
    return {"label": label, "all": base, "by_origin": {"claude-code": base}, "inventory": {"openclaw": {"thinkingDefault": "high"}}}


def test_compare_prints_old_new_and_relative_delta_for_each_metric():
    a = _snap("before")
    b = _snap("after", cost_per_active_hour=5.0, first_call_ctx_median=40000)
    rows = {(s, k): (u, v, d) for s, k, u, v, d in audit_snapshot.compare_rows(a, b)}
    assert rows[("all", "cost_per_active_hour")] == (10.0, 5.0, "-50%")
    assert rows[("all", "first_call_ctx_median")] == (80000, 40000, "-50%")
    assert rows[("claude-code", "calls")] == (100, 100, "+0%")
    text = audit_snapshot.format_compare(a, b)
    assert "before -> after" in text and "-50%" in text


def test_compare_lists_inventory_changes():
    a, b = _snap("before"), _snap("after")
    b["inventory"] = {"openclaw": {"thinkingDefault": "low"}}
    assert audit_snapshot.inventory_diff(a, b) == [("openclaw.thinkingDefault", "high", "low")]


def test_the_existing_bench_baseline_files_are_readable_by_compare():
    base = Path.home() / ".cache" / "cost-analysis" / "baseline" / "before-7d.json"
    if not base.exists():
        pytest.skip("operator baseline not present")
    doc = json.loads(base.read_text())
    rows = audit_snapshot.compare_rows(doc, doc)
    assert any(k == "cost_per_active_hour" and d in ("+0%", "") for _, k, _, _, d in rows)


def test_probes_print_the_cost_notice_first_and_use_the_injected_runner():
    said, calls = [], []

    def run(cmd):
        calls.append(cmd[0])
        assert said, "the cost notice must be printed before the first probe runs"
        return json.dumps({"usage": {"input_tokens": 1}, "total_cost_usd": 0.01})

    out = audit_snapshot.run_probes("x", run=run, say=said.append)
    assert calls == ["claude", "openclaw"]
    assert "REAL model calls" in said[0]
    assert out["claude_p_hi"]["cost_usd"] == 0.01
