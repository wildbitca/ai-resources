"""Read-only mode is enforced by a hook, and a verifier returns one machine-readable verdict per handed spec id."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_resources import cli, ledger, readonly
from orchestrator_fakes import OK, dispatches, drive_to, finish, make, reply

REPO = Path(__file__).resolve().parents[1]


def guard(payload, root, **env):
    return subprocess.run([sys.executable, str(REPO / "hooks" / "kit_handoff_guard.py")], input=json.dumps({"cwd": str(root), **payload}), text=True,
                          capture_output=True, env={"CLAUDE_PROJECT_DIR": str(root), "PATH": "/usr/bin:/bin", **env})


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def write(path, tool="Write"):
    return {"tool_name": tool, "tool_input": {"file_path": path}}


def test_without_a_lock_nothing_changes(tmp_path):
    assert guard(write(str(tmp_path / "a.py")), tmp_path).returncode == 0
    assert guard(bash("rm -rf build"), tmp_path).returncode == 0


def test_with_the_lock_the_main_thread_cannot_write_outside_agent_output(tmp_path):
    assert readonly.main(["on", "--reason", "explain request", "--dir", str(tmp_path)]) == 0
    for tool in ("Write", "Edit", "MultiEdit"):
        r = guard(write(str(tmp_path / "src" / "a.py"), tool), tmp_path)
        assert r.returncode == 2 and "Read-only mode (explain request)" in r.stderr and "ai-resources readonly off" in r.stderr
    assert guard({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "n.ipynb"}}, tmp_path).returncode == 2
    assert guard(write(str(tmp_path / ".agent-output" / "planner" / "plan.md")), tmp_path).returncode == 0
    assert guard(write(".agent-output/report.md"), tmp_path).returncode == 0
    assert guard(write(str(tmp_path / ".agent-output-evil" / "x")), tmp_path).returncode == 2
    assert guard({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, tmp_path).returncode == 0


@pytest.mark.parametrize("command", ["echo hi > out.txt", "cat a >> b", "git commit -m x", "git push origin main", "rm -rf x", "mv a b", "sed -i s/a/b/ f",
                                     "echo x | tee f", "mkdir d", "npm install left-pad", "git -C /r checkout main", "ls && touch f", "&> log cmd"])
def test_plainly_writing_bash_is_refused_in_read_only_mode(tmp_path, command):
    readonly.main(["on", "--dir", str(tmp_path)])
    assert guard(bash(command), tmp_path).returncode == 2


@pytest.mark.parametrize("command", ["ls -la", "git status", "git log --oneline -5", "git diff HEAD", "git grep -n reset", "git --no-pager log -3", "grep -rn 'a -> b' src", "cat a 2>/dev/null",
                                     "python3 -c 'print(1)'", "ai-resources readonly off", "echo hi >&2", "pytest -q tests/test_x.py 2>&1 | tail"])
def test_reading_bash_is_allowed_in_read_only_mode(tmp_path, command):
    readonly.main(["on", "--dir", str(tmp_path)])
    assert guard(bash(command), tmp_path).returncode == 0, command


def test_subagents_are_held_to_it_too_and_off_lifts_it(tmp_path):
    readonly.main(["on", "--dir", str(tmp_path)])
    assert guard({**write(str(tmp_path / "a.py")), "agent_id": "w1"}, tmp_path).returncode == 2
    assert readonly.main(["status", "--dir", str(tmp_path)]) == 0
    readonly.main(["off", "--dir", str(tmp_path)])
    assert guard({**write(str(tmp_path / "a.py")), "agent_id": "w1"}, tmp_path).returncode == 0


def test_the_environment_variable_also_sets_it_and_an_old_lock_is_ignored(tmp_path):
    assert guard(write(str(tmp_path / "a.py")), tmp_path, AI_RESOURCES_READONLY="1").returncode == 2
    assert guard(write(str(tmp_path / "a.py")), tmp_path, AI_RESOURCES_READONLY="0").returncode == 0
    readonly.main(["on", "--dir", str(tmp_path)])
    lock = tmp_path / ".agent-output" / "readonly.lock"
    old = lock.stat().st_mtime - 13 * 3600
    import os
    os.utime(lock, (old, old))
    assert guard(write(str(tmp_path / "a.py")), tmp_path).returncode == 0


def test_a_parallel_group_lock_still_guards_the_handoff_without_read_only(tmp_path):
    out = tmp_path / ".agent-output"
    out.mkdir()
    handoff = out / "handoff-x.md"
    handoff.write_text("---\n---\n", encoding="utf-8")
    (out / "parallel-group.json").write_text(json.dumps({"group": "g", "handoff_file": str(handoff)}))
    assert guard({**write(str(handoff)), "agent_id": "a"}, tmp_path).returncode == 2
    assert guard(write(str(handoff)), tmp_path).returncode == 0, "the main thread still performs the join"


def test_cli_readonly(tmp_path, capsys):
    assert cli.main(["readonly", "on", "--dir", str(tmp_path), "--reason", "r"]) == 0
    assert json.loads((tmp_path / ".agent-output" / "readonly.lock").read_text())["reason"] == "r"
    assert cli.main(["readonly", "status", "--dir", str(tmp_path)]) == 0 and "read-only on" in capsys.readouterr().out
    assert cli.main(["readonly", "off", "--dir", str(tmp_path)]) == 0 and not (tmp_path / ".agent-output" / "readonly.lock").exists()
    assert cli.main(["readonly", "bogus"]) == 2 and cli.main(["readonly", "on", "--dir"]) == 2


# --- per-spec verdicts ------------------------------------------------------------------------------------------------

def test_spec_verdicts_are_parsed_leniently():
    assert ledger.parse_spec_verdicts("S1=pass, S2 = FAIL (broke), RULE-A-1: not_run; TS-X-2=pass") == {
        "S1": "pass", "S2": "fail", "RULE-A-1": "not_run", "TS-X-2": "pass"}
    assert ledger.parse_spec_verdicts("") == {}


def test_every_handed_spec_id_needs_a_valid_verdict_and_a_failure_blocks_a_pass():
    handed = {"T1", "S1", "S2"}
    assert ledger.spec_verdict_problems(handed, {"S1": "pass", "S2": "not_run"}, "PASS") == []
    assert "S2" in ledger.spec_verdict_problems(handed, {"S1": "pass"})[0]
    assert "pass, fail or not_run" in ledger.spec_verdict_problems(handed, {"S1": "pass", "S2": "maybe"})[0]
    assert "S2 failed" in ledger.spec_verdict_problems(handed, {"S1": "pass", "S2": "fail"}, "PASS")[0]
    assert ledger.spec_verdict_problems(handed, {"S1": "pass", "S2": "fail"}, "REQUIRES_CHANGES") == []
    assert ledger.spec_verdict_problems({"T1"}, {}) == [], "task ids are not verified one by one"


def _to_verifier(tmp_path, ids="Spec_ids: S1, S2\n"):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models={})
    [plan] = dispatches(e.next(run["id"]))
    finish(e, run["id"], plan, OK + ids)
    [d] = dispatches(drive_to(e, run["id"], "verify", wf))
    return e, run, d


def test_the_engine_rejects_a_verifier_that_skips_a_handed_spec_id(tmp_path):
    e, run, d = _to_verifier(tmp_path)
    res = finish(e, run["id"], d, OK + "Verdict: PASS\nSpec_verdicts: S1=pass\n")
    assert e.status(run["id"])["steps"][d["step"]]["reason"].startswith("the verifier gave no verdict for: S2")
    [again] = dispatches(res)
    res = finish(e, run["id"], again, OK + "Verdict: PASS\nSpec_verdicts: S1=pass, S2=pass\n")
    assert e.status(run["id"])["steps"][d["step"]]["spec_verdicts"] == {"S1": "pass", "S2": "pass"}


def test_a_failed_spec_cannot_hide_behind_a_pass(tmp_path):
    e, run, d = _to_verifier(tmp_path)
    finish(e, run["id"], d, OK + "Verdict: PASS\nSpec_verdicts: S1=pass, S2=fail\n")
    assert "S2 failed" in e.status(run["id"])["steps"][d["step"]]["reason"]


def test_a_run_that_handed_out_no_spec_ids_needs_no_spec_verdicts(tmp_path):
    e, run, d = _to_verifier(tmp_path, ids="")
    finish(e, run["id"], d, OK + "Verdict: PASS\n")
    assert e.status(run["id"])["steps"][d["step"]]["state"] == "done"
