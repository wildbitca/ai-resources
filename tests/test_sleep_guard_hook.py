"""ADR-0014 D7: the PreToolUse sleep guard blocks a Bash `sleep` longer than 30 seconds (exit 2), allows the rest,
and fails open on anything it cannot read."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "kit_sleep_guard.py"


def run(stdin: str):
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, timeout=20)


def bash(command: str, tool: str = "Bash") -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


@pytest.mark.parametrize("command", [
    "sleep 31", "sleep 45", "sleep 5m", "sleep 600", "sleep 1h", "sleep 0.6m", "sleep 1m 5s", "sleep infinity",
    "make build && sleep 120 && echo done", "echo start; sleep 60", "while true; do sleep 90; done",
    "(sleep 300)", "timeout 900 sleep 400", "echo a\nsleep 100", "sudo sleep 61", "nohup sleep 1d &",
])
def test_long_sleep_is_blocked_with_guidance(command):
    r = run(bash(command))
    assert r.returncode == 2, (command, r.stdout, r.stderr)
    assert "run_in_background" in r.stderr and "Monitor" in r.stderr


@pytest.mark.parametrize("command", [
    "sleep 29", "sleep 30", "sleep 0.5", "sleep 30s", "sleep 0.5m", "sleep 5", "make && sleep 2 && echo",
    "echo no sleep 100 here", "grep -r 'sleep 60' .", 'git commit -m "remove sleep 300"', "asleep 100", "usleep 5000000",
    "ls -la", "", "sleep $DELAY", 'sleep "$N"',
])
def test_short_or_unrelated_commands_pass(command):
    r = run(bash(command))
    assert r.returncode == 0, (command, r.stderr)
    assert r.stderr == ""


def test_29_30_31_boundary():
    assert run(bash("sleep 29")).returncode == 0
    assert run(bash("sleep 30")).returncode == 0
    assert run(bash("sleep 31")).returncode == 2


def test_non_bash_tools_are_ignored():
    assert run(bash("sleep 600", tool="Write")).returncode == 0
    assert run(json.dumps({"tool_name": "Edit", "tool_input": {"command": "sleep 600"}})).returncode == 0


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null", "{}", '{"tool_name": "Bash"}',
                                   '{"tool_name": "Bash", "tool_input": "sleep 600"}',
                                   '{"tool_name": "Bash", "tool_input": {"command": 5}}', "\x00\x01"])
def test_malformed_stdin_fails_open(stdin):
    r = run(stdin)
    assert r.returncode == 0, (stdin, r.stderr)


def test_the_hook_imports_only_the_helper_and_the_standard_library():
    text = HOOK.read_text(encoding="utf-8")
    assert "ai_resources" not in text.replace("sleep_pattern", "") or "import ai_resources" not in text
    for banned in ("import audit", "from ai_resources", "import yaml", "import rich"):
        assert banned not in text


def test_the_shared_helper_is_the_one_the_audit_uses():
    from ai_resources import audit_sources, sleep_pattern
    assert audit_sources.sleep_pattern is sleep_pattern
    assert sleep_pattern.longest_sleep_seconds("sleep 31") == 31
    assert sleep_pattern.longest_sleep_seconds("sleep 5m") == 300
    assert sleep_pattern.longest_sleep_seconds("echo hi") is None
    assert sleep_pattern.has_sleep("make; sleep 3")
    assert not sleep_pattern.has_sleep("echo sleep 3")
