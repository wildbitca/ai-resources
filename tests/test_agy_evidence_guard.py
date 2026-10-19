"""ADR-0010 PD-7: the PreToolUse hook that keeps every agent session away from the Antigravity evidence record."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "agy_evidence_guard.py"
REC = "scripts/ai_resources/data/agy_gate_evidence.json"


def run(payload) -> subprocess.CompletedProcess:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=text, capture_output=True, text=True, cwd=REPO)


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
@pytest.mark.parametrize("path", [str(REPO / REC), REC, "./" + REC, "other/../" + REC, "/x/kit/" + REC])
def test_write_tools_aimed_at_the_record_are_denied_in_any_session(tool, key, path):
    for extra in ({}, {"agent_id": "a1"}):                       # the main thread and a subagent alike
        proc = run({"tool_name": tool, "tool_input": {key: path}, "cwd": str(REPO), **extra})
        assert proc.returncode == 2, (tool, path, extra)
        assert "lanes gates record" in proc.stderr


@pytest.mark.parametrize("command", [
    f"echo '{{}}' > {REC}",
    f"echo '{{}}' >> {REC}",
    f"cat x | tee {REC}",
    f"tee -a {REC} < x",
    f"sed -i 's/a/b/' {REC}",
    f"rm -f {REC}",
    f"truncate -s 0 {REC}",
    f"cp /tmp/x.json {REC}",
    f"mv x.json {REC}",
    f"dd if=x of={REC}",
    f"git checkout HEAD~1 -- {REC}",
    f"git restore {REC}",
    f"python3 -c \"open('{REC}', 'w').write('x')\"",
    f"python3 -c \"import pathlib; pathlib.Path('{REC}').write_text('x')\"",
    f"cd scripts/ai_resources/data && echo x > agy_gate_evidence.json",
])
def test_shell_writes_to_the_record_are_denied(command):
    proc = run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO)})
    assert proc.returncode == 2, command


@pytest.mark.parametrize("command", [
    f"cat {REC}",
    f"grep -n proven {REC}",
    f"jq . {REC}",
    f"git add {REC} && git commit -m evidence",
    f"git log -1 -- {REC}",
    f"git diff HEAD -- {REC}",
    "ai-resources lanes gates record --run-dir ~/.cache/ai-resources-live-e2e/20261101T101010Z",
    "ai-resources lanes gates",
    "python3 -m pytest tests/test_agy_gate_evidence.py",
    "echo hello > notes.txt",
    "ls scripts/ai_resources/data",
])
def test_reading_and_unrelated_commands_pass(command):
    assert run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO)}).returncode == 0, command


@pytest.mark.parametrize("path", ["scripts/ai_resources/agy_gate_evidence.py", "tests/test_agy_gate_evidence.py",
                                  "docs/decisions/0010-setup-five-phases-channels-team.md",
                                  "scripts/ai_resources/data/other.json"])
def test_other_files_and_other_tools_pass(path):
    assert run({"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": str(REPO)}).returncode == 0
    assert run({"tool_name": "Read", "tool_input": {"file_path": REC}, "cwd": str(REPO)}).returncode == 0


def test_a_broken_input_never_blocks():
    assert run("not json").returncode == 0
    assert run("[]").returncode == 0
    assert run({"tool_name": "Write", "tool_input": None, "cwd": str(REPO)}).returncode == 0


def test_a_symlink_to_the_record_is_resolved(tmp_path):
    link = tmp_path / "innocent.json"
    link.symlink_to(REPO / REC)
    assert run({"tool_name": "Write", "tool_input": {"file_path": str(link)}, "cwd": str(tmp_path)}).returncode == 2
