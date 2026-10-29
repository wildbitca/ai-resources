"""Handoff by reference: the ledger's Log is append-only, S# text is literal, and `Covered:` names only handed-out ids."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_resources import cli, ledger
from ai_resources.orchestrator import engine as eng
from orchestrator_fakes import OK, dispatches, drive_to, finish, make, reply

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]

LEDGER = """# Tasks: x

## Sizing
- mode: light

## Specs
- **S1**: Given a, when b, then c (source: link; proof: `pytest -k a`)
- **S2**: Given d, when e, then f (source: link; proof: `pytest -k d`)

## Tasks
- [ ] **T1** (covers S1) change a - commit: abc
- [ ] **T2** (covers S2) change d

## Log
- L1 2026-10-27: original request link
- L2 2026-10-28: scope: also handle d
"""


def swap(text, old, new):
    assert old in text
    return text.replace(old, new)


def test_an_unchanged_or_extended_ledger_is_fine():
    assert ledger.append_only_problems(LEDGER, LEDGER) == []
    grown = LEDGER + "- L3 2026-10-29: scope change, S3 supersedes S2\n"
    grown = swap(grown, "\n## Tasks", "- **S3**: Given g, when h, then i (supersedes S2; source: link; proof: `pytest -k g`)\n\n## Tasks")
    assert ledger.append_only_problems(LEDGER, grown) == []


def test_editing_removing_or_reordering_a_log_line_is_refused():
    edited = swap(LEDGER, "original request link", "a different request")
    assert any("line 1 changed" in p for p in ledger.append_only_problems(LEDGER, edited))
    removed = swap(LEDGER, "- L2 2026-10-28: scope: also handle d\n", "")
    assert any("line 2 changed or was removed" in p for p in ledger.append_only_problems(LEDGER, removed))
    assert any("## Log was removed" in p for p in ledger.append_only_problems(LEDGER, LEDGER.split("## Log")[0]))


def test_new_log_lines_must_be_numbered_entries_that_follow_on():
    assert any("must be an `L#` entry" in p for p in ledger.append_only_problems(LEDGER, LEDGER + "- note without a number\n"))
    assert any("does not follow L2" in p for p in ledger.append_only_problems(LEDGER, LEDGER + "- L2 again\n"))


def test_an_s_id_is_literal_text_never_rewritten_or_deleted():
    rewritten = swap(LEDGER, "then c (source", "then CHANGED (source")
    assert any("S1 was rewritten in place" in p for p in ledger.append_only_problems(LEDGER, rewritten))
    deleted = swap(LEDGER, "- **S2**: Given d, when e, then f (source: link; proof: `pytest -k d`)\n", "")
    assert any("S2 was removed" in p for p in ledger.append_only_problems(LEDGER, deleted))


def test_covered_must_come_from_the_ids_handed_out():
    handed = ledger.handed_ids(["T1"], ["S3-S4"])
    assert handed == {"T1", "S3", "S4"}
    assert ledger.covered_problems("T1, S3", handed) == []
    assert "S9" in ledger.covered_problems("S3, S9", handed)[0]
    assert ledger.covered_problems("S3-S4", handed) == [] and ledger.covered_problems("S3-S5", handed)
    assert ledger.covered_problems("anything", set()) == [], "nothing handed out, nothing to check"


def test_a_handed_task_hands_out_the_specs_it_covers_when_the_ledger_says_so():
    assert ledger.handed_ids(["T1"], [], LEDGER) == {"T1", "S1"}
    assert ledger.covered_problems("S1, S2", ledger.handed_ids(["T1"], [], LEDGER), LEDGER)
    assert ledger.covered_problems("T1, S1", ledger.handed_ids(["T1"], [], LEDGER), LEDGER) == []
    assert ledger.covered_problems("S1", {"S1"}, swap(LEDGER, "- **S1**", "- **S7**"))[0].startswith("Covered names ids the ledger does not define")


def test_covered_value_reads_decorated_lines():
    assert ledger.covered_value("x\n- **Covered:** T1, S2\n") == "T1, S2"
    assert ledger.covered_value("no such field") == ""


def test_cli_ledger_check_and_covered_check(tmp_path, capsys):
    old, new = tmp_path / "old.md", tmp_path / "tasks.md"
    old.write_text(LEDGER, encoding="utf-8")
    new.write_text(LEDGER, encoding="utf-8")
    assert cli.main(["ledger-check", str(new), "--against", str(old)]) == 0
    new.write_text(swap(LEDGER, "original request link", "x"), encoding="utf-8")
    assert cli.main(["ledger-check", str(new), "--against", str(old)]) == 1
    assert "changed or was removed" in capsys.readouterr().err
    with pytest.raises(SystemExit) as bad:
        cli.main(["ledger-check"])
    assert bad.value.code == 2
    assert cli.main(["covered-check", "--handed", "T1,S3", "--covered", "S3"]) == 0
    assert cli.main(["covered-check", "--handed", "T1,S3", "--covered", "S4"]) == 1
    assert cli.main(["covered-check", "--handed", "T1", "--covered", ""]) == 0


def test_ledger_check_compares_with_the_committed_version(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    path = tmp_path / "tasks.md"
    assert cli.main(["ledger-check", str(path)]) == 1, "unreadable file"
    path.write_text(LEDGER, encoding="utf-8")
    assert cli.main(["ledger-check", str(path)]) == 0, "not committed yet"
    env = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "add", "tasks.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", *env, "commit", "-qm", "ledger"], cwd=tmp_path, check=True)
    path.write_text(LEDGER + "- L3 more\n", encoding="utf-8")
    assert cli.main(["ledger-check", str(path)]) == 0
    path.write_text(swap(LEDGER, "L2 2026-10-28", "L2 edited"), encoding="utf-8")
    assert cli.main(["ledger-check", str(path)]) == 1


def test_the_engine_rejects_a_covered_line_naming_ids_it_was_not_given(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models={})
    res = drive_to(e, run["id"], "plan", wf)
    finish(e, run["id"], dispatches(res)[0], OK + "Task_ids: T1\nSpec_ids: S1\nSpec_ref: specs/x/tasks.md\n")
    [d] = dispatches(e.approve(run["id"]))
    res = finish(e, run["id"], d, OK + "Covered: T1, S1, S9\n")
    [again] = dispatches(res)
    assert again["step"] == d["step"] and again["attempt"] == 2
    assert e.status(run["id"])["steps"][d["step"]]["reason"].startswith("Covered names ids that were not handed out: S9")
    res = finish(e, run["id"], again, OK + "Covered: T1, S1\n")
    assert e.status(run["id"])["steps"][d["step"]]["state"] == "done"


def test_a_run_that_handed_out_no_ids_checks_nothing(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models={})
    [d] = dispatches(e.next(run["id"]))
    finish(e, run["id"], d, OK + "Covered: S99\n")
    assert e.status(run["id"])["steps"][d["step"]]["state"] == "done"


def _hook(payload, root):
    return subprocess.run([sys.executable, str(REPO / "hooks" / "kit_subagent_return.py")], input=json.dumps(payload), text=True,
                          capture_output=True, env={"CLAUDE_PROJECT_DIR": str(root), "PATH": "/usr/bin:/bin"})


def test_the_return_hook_flags_a_covered_id_the_prompt_never_named(tmp_path):
    out = tmp_path / ".agent-output"
    out.mkdir()
    (out / "handoff-x.md").write_text("---\nstep: plan\n---\n", encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"role": "user", "content": "kit-orchestration: work on specs/x/tasks.md T2 and S3"}}) + "\n")
    block = "## Result\n- Status: success\n## Artifacts\n- Covered: {c}\n## Routing\n- Blocked: no\n"
    base = {"agent_type": "implementer", "cwd": str(tmp_path), "agent_transcript_path": str(transcript)}
    assert _hook({**base, "last_assistant_message": block.format(c="T2, S3")}, tmp_path).returncode == 0
    bad = _hook({**base, "last_assistant_message": block.format(c="T2, S8")}, tmp_path)
    assert bad.returncode == 2 and "S8" in bad.stderr
    assert _hook({**base, "stop_hook_active": True, "last_assistant_message": block.format(c="S8")}, tmp_path).returncode == 0
    none = tmp_path / "n.jsonl"
    none.write_text(json.dumps({"message": {"role": "user", "content": "kit-orchestration: no ids here"}}) + "\n")
    assert _hook({**base, "agent_transcript_path": str(none), "last_assistant_message": block.format(c="S8")}, tmp_path).returncode == 0


def test_covered_reads_only_the_leading_id_list():
    handed = {"T1", "S1"}
    assert ledger.covered_problems("none (T9 not done)", handed) == []
    assert ledger.covered_problems("S1; S9 out of scope", handed) == []
    assert ledger.covered_problems("T1 and S1", handed) == []
    assert ledger.covered_problems("S1, S9", handed)


def test_the_engine_expands_a_handed_task_into_the_specs_it_covers(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models={})
    tasks = tmp_path / "tasks.md"
    tasks.write_text(LEDGER, encoding="utf-8")
    [plan] = dispatches(e.next(run["id"]))
    [d] = dispatches(finish(e, run["id"], plan, OK + f"Task_ids: T1\nSpec_ref: {tasks}\n") and e.approve(run["id"]))
    finish(e, run["id"], d, OK + "Covered: T1, S1\n")
    assert e.status(run["id"])["steps"][d["step"]]["state"] == "done"
