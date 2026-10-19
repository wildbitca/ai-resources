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
    f"git log -1 -- {REC}",
    f"git diff HEAD -- {REC}",
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


# --- N1 hardening: the bypasses the security-auditor listed ---------------------------------------------------------------
# These commands were allowed before; the operator reviews every commit touching scripts/ai_resources/data/ (ADR-0010).

DATA = "scripts/ai_resources/data/"


@pytest.mark.parametrize("command", [
    # (i) git commands that name the record or touch the data directory
    f"git add {REC} && git commit -m evidence",
    f"git add {DATA}",
    f"git add -f {DATA}agy_gate_evidence.json",
    f"git commit {REC} -m x",
    f"git -C scripts/ai_resources/data add .",
    f"git apply {REC}.patch",
    f"git checkout HEAD -- {DATA}",
    f"git merge x -- {DATA}",
    f"git restore --source=HEAD~2 {REC}",
    f"git mv x {REC}",
    f"git rm {REC}",
    f"git cherry-pick abc -- {REC}",
    f"git stash push -- {REC}",
    f"cd {DATA} && git add .",
    # (ii) the TTY-only record command, wrapped or not
    "ai-resources lanes gates record --run-dir ~/.cache/ai-resources-live-e2e/20261101T101010Z",
    "script -qc 'ai-resources lanes gates record --run-dir x' /dev/null",
    "expect -c 'spawn ai-resources lanes gates record --run-dir x; interact'",
    "python3 -c \"import pty; pty.spawn(['ai-resources','lanes','gates','record'])\" ",
    "ai-resources   lanes   gates   record",
    "ai-resources lanes gates 'record' --run-dir x",
    # (iii) numeric redirects and globs
    f"echo x 1> {REC}",
    f"echo x 2>> {REC}",
    f"echo x &> {REC}",
    f"echo x >| {REC}",
    f"echo x > {DATA}agy_gate_*",
    f"echo x > {DATA}agy_gate_evidence.js?n",
    f"echo x > {DATA}agy_gate_evidenc[e].json",
    f"echo x > {DATA}agy_gate_evidence.[j]son",
    f"cp /dev/null {DATA}*evidence.json",
    f"rm {DATA}agy_gate_*",
    f"tee {DATA}agy_gate_ev*",
    f"echo x > {DATA}agy_gate_{{evidence,y}}.json",
    f"echo x > {DATA}agy_\"\"gate_evi'dence'.json",
    # (iv) patch / git apply naming the file
    f"patch -p1 <<'EOF'\n--- a/{REC}\n+++ b/{REC}\nEOF",
    f"git apply <<'EOF'\n--- a/{REC}\n+++ b/{REC}\nEOF",
    # (v) inline interpreters that mention or build the name
    "python3 -c \"open('scripts/ai_resources/data/agy_'+'gate_evidence.json','w').write('x')\"",
    "python3 -c \"p='agy_gate'; p+='_evidence.json'; open('scripts/ai_resources/data/'+p,'w')\"",
    "node -e \"require('fs').writeFileSync('scripts/ai_resources/data/'+['agy_gate','evidence.json'].join('_'),'x')\"",
    "perl -e 'open(F, \">scripts/ai_resources/data/agy_\" . \"gate_evidence.json\")'",
    "python3 - <<'EOF'\nname = 'agy_gate' + '_evidence.json'\nEOF",
])
def test_hardened_bypasses_are_denied(command):
    assert run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO)}).returncode == 2, command


def test_a_patch_file_that_names_the_record_is_denied(tmp_path):
    patch = tmp_path / "x.patch"
    patch.write_text(f"--- a/{REC}\n+++ b/{REC}\n")
    proc = run({"tool_name": "Bash", "tool_input": {"command": f"git apply {patch}"}, "cwd": str(tmp_path)})
    assert proc.returncode == 2
    other = tmp_path / "y.patch"
    other.write_text("--- a/README.md\n+++ b/README.md\n")
    assert run({"tool_name": "Bash", "tool_input": {"command": f"git apply {other}"}, "cwd": str(tmp_path)}).returncode == 0


def test_patch_word_in_a_read_only_command_is_allowed_but_real_patch_commands_still_denied(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(f"mentions {REC}\n")
    for command in (f"grep -n patch {doc}", f"kubectl patch deploy x --patch-file {doc}"):
        assert run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}).returncode == 0, command
    for command in (f"patch -p1 < {doc}", f"patch -i {doc}", f"cd . && patch {doc}"):
        assert run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}).returncode == 2, command


@pytest.mark.parametrize("command", [
    "git add README.md && git commit -m x",
    "git add scripts/ai_resources/setup/wizard.py",
    "git status",
    "git log --oneline -3",
    f"git diff HEAD -- {REC}",
    "rm -rf build/*",
    "rm tmp/*.json",
    "cp * /backup",
    "echo x 2> /dev/null",
    "python3 -c \"print(1)\"",
    "python3 -m pytest tests/test_agy_gate_evidence.py",
    "node -e \"console.log(1)\"",
    "ai-resources lanes gates",
    "ai-resources lanes status",
])
def test_hardening_does_not_block_ordinary_commands(command):
    assert run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO)}).returncode == 0, command
