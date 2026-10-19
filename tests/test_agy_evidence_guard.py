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


# --- low-severity advisories: git am, the data directory without a file name, sweeping git commands ----------------------

def _bash(command: str, cwd=REPO) -> int:
    return run({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}).returncode


@pytest.mark.parametrize("command", [
    f"git am <<'EOF'\n--- a/{REC}\n+++ b/{REC}\nEOF",
    f"git am --3way {REC}.mbox",
    f"cp /dev/null {DATA}",
    f"cp x.json {DATA}",
    f"cp a b {DATA}other.json",
    f"mv x.json {DATA}",
    f"mv x.json {DATA} 2>/dev/null",
    f"cp -t {DATA} x.json",
    f"cp --target-directory {DATA} x.json",
    f"rsync -a src/ {DATA}",
    f"ln -sf /x/evidence.json {DATA}",
    f"install -m 644 x.json {DATA}",
    f"tar -xf evidence.tar -C {DATA}",
    f"tar xzf evidence.tgz -C {DATA}",
    f"tar --extract --file e.tar --directory {DATA}",
    f"unzip -o e.zip -d {DATA}",
    f"sed -i 's/a/b/' {DATA}*.json",
    f"sed --in-place s/a/b/ {DATA}other.json",
    f"echo x | tee {DATA}other.json",
    f"dd if=x of={DATA}other.json",
    f"cd {DATA} && cp ../x.json .",
    f"cd {DATA} && tar xf ~/e.tar",
    f"cd {DATA} && sed -i s/a/b/ other.json",
])
def test_data_directory_writes_are_denied_without_the_file_name(command):
    assert _bash(command) == 2, command


@pytest.mark.parametrize("command", [
    f"cp {DATA}other.json /backup/",
    f"cp {DATA}other.json other.json",
    f"mv {DATA}other.json /tmp/x",
    f"rsync -a {DATA} /backup",
    f"tar -cf out.tar {DATA}",
    f"tar -tf e.tar",
    f"tar -xf e.tar -C build",
    "unzip -o e.zip -d build",
    f"sed -n 1,3p {DATA}other.json",
    f"sed 's/a/b/' {DATA}other.json",
    f"sed -e 's/a/b/' {DATA}other.json > out.json",
    f"grep -rn proven {DATA}",
    f"ls -la {DATA}",
    f"cat {DATA}other.json | tee out.json",
    f"diff {DATA}a.json {DATA}b.json",
    f"cp {DATA}other.json /backup/ 2>&1",
    "cp x scripts/ai_resources/database/y",
    "sed -i s/a/b/ scripts/ai_resources/setup/wizard.py",
    "cd scripts/ai_resources/setup && cp x y",
    "dd if=a of=b",
])
def test_data_directory_rules_leave_reads_and_other_paths_alone(command):
    assert _bash(command) == 0, command


def _repo_with_record(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)
    git("init", "-q")
    rec = tmp_path / REC
    rec.parent.mkdir(parents=True)
    rec.write_text("{}\n")
    (tmp_path / "a.txt").write_text("a\n")
    git("add", ".")
    git("commit", "-qm", "init")
    return rec


SWEEPS = [
    "git commit -a -m x", "git commit -am x", "git commit --all -m x",
    "git add -A", "git add .", "git add --all", "git add -u", "git add -A && git commit -m x",
    "git reset --hard", "git reset --hard HEAD~1", "git stash pop", "git stash apply", "git pull", "git pull --rebase",
    "git merge main",
]
READ_ONLY = [
    "git status", "git diff", "git log --oneline -5", "git show HEAD", "git branch -a", "git fetch", "git stash list",
    "git commit -m x", "git add a.txt", "git reset --soft HEAD~1", "git reset HEAD a.txt", "git push origin main",
    "git commit --amend -m x", "git stash", "git checkout -b topic", "git rebase --abort", "git remote -v",
]


@pytest.mark.parametrize("command", SWEEPS)
def test_sweeping_git_commands_are_denied_while_the_record_is_modified(tmp_path, command):
    rec = _repo_with_record(tmp_path)
    assert _bash(command, tmp_path) == 0, "clean record: ordinary git work is untouched"
    rec.write_text('{"proven": true}\n')
    assert _bash(command, tmp_path) == 2, command


def test_sweeping_git_commands_are_denied_while_the_record_is_untracked(tmp_path):
    rec = _repo_with_record(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "rm", "-q", "--cached", REC], check=True, capture_output=True)
    assert rec.exists()
    assert _bash("git add -A", tmp_path) == 2
    assert _bash("git commit -am x", tmp_path) == 2


@pytest.mark.parametrize("command", READ_ONLY)
def test_ordinary_git_commands_pass_even_while_the_record_is_modified(tmp_path, command):
    rec = _repo_with_record(tmp_path)
    rec.write_text('{"proven": true}\n')
    assert _bash(command, tmp_path) == 0, command


@pytest.mark.parametrize("command", SWEEPS + READ_ONLY)
def test_git_commands_pass_outside_a_repository_and_in_other_repositories(tmp_path, command):
    assert _bash(command, tmp_path) == 0, command
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    assert _bash(command, tmp_path) == 0, command
