"""`ai-resources agy run` (ADR-0019 C2) against a fake `agy` on PATH: ok, quota, timeout, missing, empty, scope violation."""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ai_resources import agy_run, cli, dispatch_ledger, scope_guard

FAKE = Path(__file__).with_name("fake_agy.py")


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A git checkout, a fake agy first on PATH, a usage file with every pool idle."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    agy.chmod(agy.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "agy-call.json"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    monkeypatch.delenv("AI_RESOURCES_AGY_BIN", raising=False)
    usage = tmp_path / "usage.json"
    usage.write_text("{}")
    return type("Box", (), {"repo": repo, "log": log, "bin": bin_dir, "usage": str(usage)})


def run(capsys, box, *argv):
    rc = cli.main(["agy", "run", "--cwd", str(box.repo), "--usage-file", box.usage, "--json", *argv])
    out = capsys.readouterr()
    return rc, (json.loads(out.out) if out.out.strip() else {}), out.err


def test_ok_runs_agy_with_the_matrix_model_and_returns_the_report_contract(box, capsys):
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "list the files", "--run", "r1", "--step", "scan")
    assert rc == 0 and doc["outcome"] == "ok" and doc["route"] == "agy:gemini-3.8-flash-high" and doc["pool"] == "agy-gemini"
    assert (doc["status"], doc["blocked"], doc["verdict"], doc["tokens_in"], doc["tokens_out"]) == ("success", False, "PASS", 120, 17)
    assert doc["report_path"] == ".agent-output/r1/explore/report.md"
    assert "Verdict: PASS" in (box.repo / doc["report_path"]).read_text()
    call = json.loads(box.log.read_text())
    assert call["cwd"] == str(box.repo.resolve())
    argv = call["argv"]
    assert argv[:4] == list(agy_run.AGY_FLAGS) and argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"
    assert argv[-2] == "-p" and "list the files" in argv[-1] and ".agent-output/r1/explore/" in argv[-1] and "Status: success" in argv[-1]
    [row] = dispatch_ledger.load("r1")
    assert (row["outcome"], row["step"], row["backend"], row["tokens_in"], row["fallback_cause"]) == ("ok", "scan", "agy", 120, "")


def test_prompt_file_and_a_write_role_inside_its_scope_logs_no_violation(box, capsys, monkeypatch):
    pf = box.repo.parent / "p.md"
    pf.write_text("write the tests")
    monkeypatch.setenv("FAKE_AGY_WRITE", "src/new_test.py")
    rc, doc, _ = run(capsys, box, "--role", "tester", "--prompt-file", str(pf), "--scope", "src", "--run", "r2")
    assert rc == 0 and doc["scope_violations"] == [] and (box.repo / "src/new_test.py").is_file()
    assert scope_guard.held(str(box.repo), "r2") == []                         # the one-shot claim is released


def test_a_write_outside_the_scope_is_logged_never_blocking(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_WRITE", "src/ok.py,docs/stray.md")
    rc, doc, _ = run(capsys, box, "--role", "tester", "--prompt", "go", "--scope", "src", "--run", "r3")
    assert rc == 0 and doc["outcome"] == "ok" and doc["scope_violations"] == ["docs/stray.md"]
    [row] = dispatch_ledger.load("r3")
    assert row["scope_violations"] == ["docs/stray.md"]


def test_a_read_role_that_writes_is_logged_too(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_WRITE", "oops.txt")
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r4")
    assert rc == 0 and doc["scope_violations"] == ["oops.txt"]


def test_two_ad_hoc_run_ids_on_one_scope_are_refused_while_the_first_holder_lives(box, capsys):
    assert scope_guard.claim(str(box.repo), "team-a", ["src"], agy_run._live, holder=True) == []   # the first team's claim
    rc, doc, _ = run(capsys, box, "--role", "tester", "--prompt", "go", "--scope", "src/deep", "--run", "team-b")
    assert rc == 2 and "write scope held by team-a: src" in doc["refused"]


def test_quota_exits_75_with_the_cause_and_the_next_cell_and_records_it(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "quota")
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r5")
    assert rc == 75 and doc["cause"] == "pool-exhausted" and doc["fallback"] == "claude-code:haiku"
    [row] = dispatch_ledger.load("r5")
    assert (row["outcome"], row["fallback_cause"], row["fallback_from"]) == ("fail", "pool-exhausted", "agy:gemini-3.8-flash-high")


def test_a_timeout_kills_agy_and_exits_75(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "timeout")
    rc, doc, _ = run(capsys, box, "--role", "code-reviewer", "--prompt", "review", "--timeout", "1", "--run", "r6")
    assert rc == 75 and doc["cause"] == "timeout" and doc["fallback"] == "claude-code:sonnet"
    assert dispatch_ledger.load("r6")[0]["fallback_cause"] == "timeout"


def test_an_empty_answer_exits_75_with_cause_empty(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "empty")
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r7")
    assert rc == 75 and doc["cause"] == "empty" and not (box.repo / ".agent-output/r7/explore/report.md").exists()


def test_a_missing_agy_exits_75_with_cause_agy_missing(box, capsys, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r8")
    assert rc == 75 and doc["cause"] == "agy-missing" and doc["fallback"] == "claude-code:haiku"
    assert dispatch_ledger.load("r8")[0]["fallback_cause"] == "agy-missing"


def test_a_crash_without_quota_text_is_a_plain_failure_not_a_fallback(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "crash")
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r9")
    assert rc == 1 and "boom" in doc["error"] and not doc.get("cause")


def test_a_role_that_does_not_resolve_to_agy_is_refused_with_its_route(box, capsys):
    rc, doc, _ = run(capsys, box, "--role", "implementer", "--prompt", "build")
    assert rc == 2 and "claude-code:sonnet" in doc["refused"] and not box.log.exists()
    box_usage = Path(box.usage)
    box_usage.write_text(json.dumps({"agy-gemini": 0.9}))                       # a hot agy pool resolves to the Claude fallback
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look")
    assert rc == 2 and "claude-code:haiku" in doc["refused"] and "pool-hot" in doc["refused"]


def test_a_checker_run_is_blocked_when_every_rung_shares_a_writer_family(box, capsys):
    dispatch_ledger.record("implementer", "ok", run_id="r10", cell="claude-code:sonnet")
    dispatch_ledger.record("tester", "ok", run_id="r10", cell="claude-code:opus")
    rc, doc, _ = run(capsys, box, "--role", "verifier", "--prompt", "verify", "--run", "r10")
    assert rc == 3 and doc["blocked"]


def test_a_prompt_is_required(box, capsys):
    rc, doc, _ = run(capsys, box, "--role", "explore")
    assert rc == 2 and "prompt" in doc["error"]


def test_parse_stream_folds_text_deltas_when_the_result_has_no_response():
    out = "\n".join(json.dumps(e) for e in (
        {"step_update": {"step_type": "agent_response", "text_delta": "hel"}},
        {"step_update": {"step_type": "agent_response", "text_delta": "lo"}},
        {"result": {"status": "SUCCESS", "usage": {"input_tokens": 1, "output_tokens": 2}}}))
    got = agy_run.parse_stream(out + "\nnot json")
    assert (got["text"], got["tokens_in"], got["tokens_out"], got["error"]) == ("hello", 1, 2, "")


def test_a_bad_run_id_is_refused_before_anything_is_written(box, capsys):
    for bad in ("../../x", "a/b", "..", "a b", "x" * 81):
        rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", bad)
        assert rc == 2 and "bad --run" in doc["error"], bad
    assert not (box.repo / ".agent-output").exists() and not box.log.exists()


def test_the_report_is_never_written_through_a_planted_symlink(box, capsys, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    run_dir = box.repo / ".agent-output" / "r9" / "explore"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").symlink_to(victim)
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r9")
    assert rc == 2 and "error" in doc and victim.read_text() == "keep\n"


def test_a_symlinked_output_dir_that_leaves_the_checkout_is_refused(box, capsys, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (box.repo / ".agent-output").symlink_to(elsewhere)
    rc, doc, _ = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r10")
    assert rc == 2 and "outside" in doc["refused"] and not any(elsewhere.iterdir())


def test_agy_stderr_is_scrubbed_before_it_leaves(box, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "leak")
    rc, doc, err = run(capsys, box, "--role", "explore", "--prompt", "look", "--run", "r11")
    assert rc == 1 and "auth failed" in doc["error"]
    for secret in ("ghp_AAAA", "Bearer abc", "sk-ant-bbbb"):
        assert secret not in doc["error"] and secret not in err


def test_a_checker_of_an_engine_run_with_a_writer_but_no_recorded_writer_is_blocked_with_a_cause(box, capsys):
    from ai_resources.orchestrator.state import RunStore
    store = RunStore()
    store.save({"id": "r12", "status": "running", "workflow_steps": [{"id": "impl", "role": "implementer"}, {"id": "v", "role": "verifier"}]})
    rc, doc, _ = run(capsys, box, "--role", "verifier", "--prompt", "check", "--run", "r12")
    assert rc == 3 and doc["blocked"] and doc["cause"] == "no-independent-checker"
    assert not box.log.exists()                                   # agy never ran
