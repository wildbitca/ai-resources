"""Dispatch ledger (ADR-0019 D7): append-only rows, writer families per run, CLI, run-ledger actuals, `route --run/--record`."""
from __future__ import annotations

import json
import threading

import pytest

from ai_resources import cli, dispatch_ledger as dl, run_ledger
from ai_resources.setup import detection


@pytest.fixture(autouse=True)
def _agy_signed_in(monkeypatch):
    monkeypatch.setattr(detection, "agy_active", lambda: True)
    monkeypatch.setattr(detection, "codex_active", lambda: False)


def test_a_row_carries_every_field_and_derives_family_and_pool(tmp_path):
    p = tmp_path / "d.jsonl"
    r = dl.record("tester", "ok", run_id="r1", step="test", cell="agy:gemini-4-argon-high", tokens_in=10, tokens_out=3,
                  duration_s=1.234, fallback_from="agy:gemini-4-argon-high", fallback_cause="timeout", path=p)
    assert {"ts", "run_id", "step", "role", "pool", "backend", "model", "family", "tokens_in", "tokens_out", "duration_s",
            "outcome", "fallback_from", "fallback_cause", "writer_families"} <= set(r)
    assert (r["pool"], r["family"], r["backend"], r["duration_s"]) == ("agy-gemini", "gemini-argon", "agy", 1.23)
    assert dl.load(path=p) == [r]


def test_a_bad_outcome_or_cause_is_refused_and_a_torn_line_is_skipped(tmp_path):
    p = tmp_path / "d.jsonl"
    with pytest.raises(ValueError):
        dl.record("tester", "great", path=p)
    with pytest.raises(ValueError):
        dl.record("tester", "fail", fallback_cause="because", path=p)
    dl.record("tester", "ok", path=p)
    p.write_text(p.read_text() + "{torn\n")
    assert len(dl.load(path=p)) == 1


def test_concurrent_appends_do_not_tear_rows(tmp_path):
    p = tmp_path / "d.jsonl"
    ts = [threading.Thread(target=lambda i=i: [dl.record("explore", "ok", run_id=f"r{i}", path=p) for _ in range(10)]) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(dl.load(path=p)) == 80 and len(p.read_text().splitlines()) == 80


def test_writer_families_are_those_of_writer_roles_that_ran(tmp_path):
    p = tmp_path / "d.jsonl"
    dl.record("implementer", "ok", run_id="r1", cell="claude-code:sonnet", path=p)
    dl.record("tester", "pending", run_id="r1", cell="agy:gemini-4-argon-high", path=p)
    dl.record("explore", "ok", run_id="r1", cell="agy:gemini-3.8-flash-high", path=p)       # reads only: not a writer
    dl.record("doc-writer", "fail", run_id="r1", cell="claude-code:haiku", path=p)          # failed: wrote nothing we trust
    dl.record("implementer", "ok", run_id="other", cell="claude-code:opus", path=p)
    assert dl.writer_families("r1", p) == ["gemini-argon", "sonnet"]


def test_cli_record_and_list_filter_by_run_and_since(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cli.main(["dispatch", "record", "--role", "verifier", "--outcome", "ok", "--run", "r9", "--cell", "claude-code:opus",
                     "--tokens-in", "5", "--cause", "pool-hot", "--fallback-from", "claude-code:sonnet"]) == 0
    capsys.readouterr()
    assert cli.main(["dispatch", "list", "--run", "r9", "--since", "1h"]) == 0
    [row] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert (row["run_id"], row["family"], row["fallback_cause"], row["tokens_in"]) == ("r9", "opus", "pool-hot", 5)
    assert cli.main(["dispatch", "list", "--run", "nobody"]) == 0 and capsys.readouterr().out == ""


def test_runs_record_from_dispatches_fills_the_actuals(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    dl.record("planner", "ok", run_id="r5", step="plan", cell="claude-code:sonnet", tokens_in=100, tokens_out=10)
    dl.record("implementer", "fail", run_id="r5", step="build", cell="claude-code:sonnet", tokens_in=50, tokens_out=5)
    dl.record("implementer", "ok", run_id="r5", step="build", cell="claude-code:sonnet", tokens_in=60, tokens_out=6)
    assert cli.main(["runs", "record", "--mode", "light", "--outcome", "pass", "--from-dispatches", "r5", "--no-measure"]) == 0
    [row] = run_ledger.load()
    assert (row["run"], row["steps"], row["agents"], row["input"], row["output"], row["rework"]) == ("r5", 2, 2, 210, 21, 1)


def _route(capsys, *argv):
    rc = cli.main(["route", *argv])
    return rc, capsys.readouterr().out


def test_route_run_reads_the_writer_families_of_that_run_and_a_checker_avoids_them(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    usage = tmp_path / "u.json"
    usage.write_text("{}")
    dl.record("implementer", "ok", run_id="r7", cell="claude-code:sonnet")
    rc, out = _route(capsys, "verifier", "--run", "r7", "--usage-file", str(usage), "--json")
    assert rc == 0 and json.loads(out)["cell"] == "claude-code:opus"
    dl.record("tester", "ok", run_id="r7", cell="claude-code:opus")
    rc, out = _route(capsys, "verifier", "--run", "r7", "--usage-file", str(usage), "--json")
    doc = json.loads(out)
    assert rc == 3 and doc["blocked"] and "shares a family" in doc["reason"] and doc["cause"] == "no-independent-checker"
    rc, out = _route(capsys, "verifier", "--usage-file", str(usage), "--json")           # another run: no writers known
    assert json.loads(out)["cell"] == "claude-code:sonnet"


def test_route_record_appends_the_decision_with_its_cause(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    usage = tmp_path / "u.json"
    usage.write_text(json.dumps({"claude-subscription": 0.9}))
    rc, _ = _route(capsys, "implementer", "--run", "r8", "--step", "build", "--record", "--usage-file", str(usage))
    assert rc == 0
    [row] = dl.load("r8")
    assert (row["outcome"], row["step"], row["backend"], row["fallback_cause"], row["fallback_from"]) == \
        ("pending", "build", "agy", "pool-hot", "claude-code:sonnet")
    usage.write_text(json.dumps({"claude-subscription": 1.0, "agy-claude-gpt": 1.0}))
    rc, _ = _route(capsys, "verifier", "--run", "r8", "--step", "verify", "--record", "--usage-file", str(usage))
    assert rc == 3 and dl.load("r8")[-1]["outcome"] == "blocked"


def test_an_unknown_family_is_recorded_as_unknown_and_kept_in_the_writer_families(tmp_path):
    p = tmp_path / "d.jsonl"
    r = dl.record("implementer", "ok", run_id="r1", cell="agy:mystery-model", path=p)
    assert r["family"] == "unknown"
    assert dl.writer_families("r1", path=p) == ["unknown"]
    assert dl.record("implementer", "blocked", run_id="r1", path=p)["family"] == ""          # no model: no family to invent


def test_a_row_with_a_line_separator_inside_stays_one_row_and_a_bad_row_hides_no_other(tmp_path):
    p = tmp_path / "d.jsonl"
    dl.record("implementer", "ok", run_id="r1", step="a\u2028b\u0085c", cell="claude-code:sonnet", path=p)
    assert len(p.read_text().split("\n")) == 2 and "\u2028" not in p.read_text()           # escaped, one line
    legacy = json.dumps({**json.loads(p.read_text()), "step": "x\u2028y"}, ensure_ascii=False)
    broken_ts = json.dumps({**json.loads(p.read_text()), "ts": "not-a-date"})
    no_tokens = json.dumps({k: v for k, v in json.loads(p.read_text()).items() if k != "tokens_in"} | {"tokens_in": "many"})
    p.write_text(p.read_text() + legacy + "\n" + broken_ts + "\n" + no_tokens + "\n{torn\n")
    skipped: list[int] = []
    rows = dl.load(since=1.0, path=p, skipped=skipped)
    assert len(rows) == 2 and skipped == [3]                                                # bad ts, bad tokens, torn
    assert dl.summarize(rows)["dispatches"] == 2                                            # the summary survives


def test_a_run_id_must_be_a_safe_name():
    assert dl.valid_run_id("20261027-120000-feature-ab12") and dl.valid_run_id("acc-pool-1")
    for bad in ("../x", "a/b", "", "a b", "x" * 81, ".."):
        assert not dl.valid_run_id(bad), bad
    with pytest.raises(ValueError):
        dl.record("tester", "ok", run_id="../../x", path=None)


def test_the_ledger_file_is_private(tmp_path):
    p = tmp_path / "d.jsonl"
    dl.record("tester", "ok", path=p)
    assert (p.stat().st_mode & 0o777) == 0o600
