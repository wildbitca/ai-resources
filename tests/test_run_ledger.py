"""run ledger: append-only records, heuristic first then history, mid-run advice, calibration proposals."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ai_resources import cli, run_ledger as rl


def rec(mode="light", kind="bugfix", rework=0, outcome="pass", risks=(), **kw):
    base = {"mode": mode, "kind": kind, "rework": rework, "outcome": outcome, "risks": list(risks),
            "agents": 0, "steps": 4, "input": 1000, "output": 100, "wall_s": 60}
    return {**base, **kw}


def test_append_validates_and_load_skips_torn_lines(tmp_path):
    p = tmp_path / "l.jsonl"
    rl.append(rec(), p)
    with pytest.raises(ValueError):
        rl.append(rec(mode="huge"), p)
    with pytest.raises(ValueError):
        rl.append({**rec(), "agents": -1}, p)
    p.write_text(p.read_text() + "{torn\n", encoding="utf-8")
    rows = rl.load(p)
    assert len(rows) == 1 and rows[0]["schema"] == rl.SCHEMA


def test_ledger_never_lives_on_tmpfs(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/x")
    with pytest.raises(RuntimeError):
        rl.ledger_path()


def test_heuristic_rules_with_few_samples():
    assert rl.heuristic({"units": 1, "lines": 20})[0] == "inline"
    assert rl.heuristic({"risks": ["3"]})[0] == "light"
    assert rl.heuristic({"resume_pass": False})[0] == "light"
    assert rl.heuristic({"units": 2, "lines": 400})[0] == "full"
    assert rl.heuristic({"separate_deliverables": True})[0] == "full"
    assert rl.heuristic({"ambiguous": True})[0] == "light"
    history = [rec("inline", rework=1)] * (rl.MIN_SAMPLES - 1)
    out = rl.suggest({"kind": "bugfix"}, history)
    assert out["mode"] == "inline" and out["basis"] == "heuristic" and out["n"] == len(history)


def test_history_moves_one_step_once_enough_similar_runs_exist():
    bad = [rec("inline", rework=1)] * rl.MIN_SAMPLES
    out = rl.suggest({"kind": "bugfix"}, bad)
    assert (out["mode"], out["basis"]) == ("light", "history")
    other_kind = [rec("inline", kind="docs", rework=1)] * 9
    assert rl.suggest({"kind": "bugfix"}, other_kind)["basis"] == "heuristic"
    clean_light = [rec("light", rework=0)] * rl.MIN_SAMPLES
    assert rl.suggest({"kind": "bugfix"}, clean_light)["mode"] == "inline", "history never exceeds the heuristic upward"
    clean_light_risk = [rec("light", risks=["3"])] * rl.MIN_SAMPLES
    out = rl.suggest({"kind": "bugfix", "risks": ["3", "5"], "units": 2, "lines": 500}, clean_light_risk)
    assert out["heuristic"] == "full" and out["mode"] == "light" and out["basis"] == "history"


def test_forced_mode_always_wins_and_stays_a_recommendation():
    bad = [rec("inline", rework=1)] * 9
    out = rl.suggest({"kind": "bugfix"}, bad, force="inline")
    assert out["mode"] == "inline" and out["basis"] == "forced" and out["recommendation"] is True
    with pytest.raises(ValueError):
        rl.suggest({}, [], force="max")


def test_reassess_moves_both_ways_without_floor_or_ceiling():
    assert rl.reassess("inline", {"verifier_fail": True})["to"] == "light"
    assert rl.reassess("inline", {"verifier_fail": True, "files_estimated": 2, "files_actual": 9})["to"] == "full"
    assert rl.reassess("light", {"files_estimated": 3, "files_actual": 7})["to"] == "full"
    assert rl.reassess("inline", {"sensitive_area": True})["to"] == "light"
    assert rl.reassess("full", {"trivial": True})["to"] == "light"
    assert rl.reassess("inline", {"trivial": True}) is None
    assert rl.reassess("full", {"verifier_fail": True}) is None
    assert rl.reassess("light", {}) is None


def test_calibrate_flags_undersized_and_overkill_only_with_enough_samples():
    few = rl.calibrate([rec("light", rework=1)] * (rl.MIN_SAMPLES - 1))
    assert few["proposals"] == [] and any("light: n=4" in n for n in few["notes"])
    hist = [rec("light", rework=1)] * 6 + [rec("full", lines=40, units=1)] * 5
    rep = rl.calibrate(hist)
    text = " ".join(rep["proposals"])
    assert "light: 100%" in text and "full: 5 full runs" in text and "owner approves" in text
    assert rep["modes"]["light"]["n"] == 6 and rep["modes"]["full"]["rework_rate"] == 0.0
    assert "Proposals" in rl.format_report(rep)


def test_measure_prices_only_the_run_window():
    def u(ts, sess="s"):
        return SimpleNamespace(ts=datetime.fromtimestamp(ts, timezone.utc), session=sess, input=10, output=5,
                               cache_read=100, cache_write=0)
    m = rl.measure([u(100), u(200), u(900)], 50, 300, price=lambda _: 0.5)
    assert (m["calls"], m["input"], m["output"], m["usd"]) == (2, 220, 10, 1.0)
    only = rl.measure([u(100, "abc"), u(100, "b"), u(900, "abc")], 50, 300, ["ab"], price=lambda _: None)
    assert (only["calls"], only["unpriced_calls"]) == (1, 1), "session prefix and window both apply"


def test_from_orchestrator_counts_steps_agents_and_returns():
    run = {"id": "r1", "status": "completed", "started_at": 100, "finished_at": 160, "returns": {"implement": 2},
           "handoff": {"workflow": "odd"},
           "steps": {"a": {"state": "done", "agent": "x"}, "b": {"state": "done", "agent": "x"},
                     "c": {"state": "done", "agent": "y"}, "d": {"state": "pending"}}}
    r = rl.from_orchestrator(run)
    assert (r["steps"], r["agents"], r["rework"], r["wall_s"], r["outcome"]) == (3, 2, 2, 60, "pass")


def test_cli_record_suggest_report_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    argv = ["runs", "record", "--mode", "light", "--outcome", "pass", "--agents", "0", "--steps", "4",
            "--rework", "0", "--start", "100", "--end", "160", "--no-measure"]
    assert cli.main(argv) == 0
    assert cli.main(["runs", "record", "--mode", "light", "--outcome", "pass", "--no-measure"]) == 2
    row = json.loads((tmp_path / "ai-resources" / "run-ledger.jsonl").read_text().splitlines()[0])
    assert row["wall_s"] == 60 and row["input"] is None and row["source"] == "manual"
    capsys.readouterr()
    assert cli.main(["runs", "suggest", "--risk", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "light"
    assert cli.main(["runs", "suggest", "--risk", "3", "--force", "full"]) == 0
    assert json.loads(capsys.readouterr().out)["basis"] == "forced"
    assert cli.main(["runs", "reassess", "inline", "--verifier-fail"]) == 0
    assert json.loads(capsys.readouterr().out)["to"] == "light"
    assert cli.main(["runs", "report"]) == 0
    assert "Run ledger calibration (1 runs)" in capsys.readouterr().out
