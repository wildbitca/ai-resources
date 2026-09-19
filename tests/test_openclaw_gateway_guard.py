"""The gateway guard hook (pitfall T01, T24): deny two exact shapes, and nothing else.

`gateway_active` is a stub and the marker path a temp file, so no test asks systemd anything.
The false-positive corpus is the important half: a deny aborts the whole tool call, and the
kit's own docs are full of the literal strings the guard matches. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "WATCHDOG_OFF", str(tmp_path / "watchdog.off"))
    state = {"active": True}
    monkeypatch.setattr(mod, "gateway_active", lambda: state["active"])
    mod.state = state
    return mod


def run(guard, monkeypatch, command="", tool="Bash", raw=None, capsys=None):
    payload = raw if raw is not None else json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    code = guard.main()
    err = capsys.readouterr().err if capsys else ""
    return code, err


DENIED = [
    "openclaw doctor --fix",
    "openclaw doctor --fix --non-interactive",
    "/home/linuxbrew/.linuxbrew/bin/openclaw doctor --fix",
    "sudo openclaw doctor --fix",
    "FOO=1 BAR=2 openclaw doctor --fix",
    "cd ~ && openclaw doctor --fix",
    "openclaw doctor --fix | tee /tmp/out",
    "if true; then openclaw doctor --fix; fi",
    'bash -c "openclaw doctor --fix"',
    "systemctl --user stop openclaw-gateway.service",
    "systemctl --user stop openclaw-gateway",
    "openclaw health; systemctl --user stop openclaw-gateway.service",
]

# AC-7.3: text that mentions the shapes without running them.
ALLOWED = [
    'grep -rn "openclaw doctor --fix" docs/',
    "grep -n 'systemctl --user stop openclaw-gateway.service' docs/openclaw/pitfalls.md",
    'echo "run openclaw doctor --fix later"',
    'git commit -m "docs: never run openclaw doctor --fix bare"',
    "cat <<'EOF' > notes.md\nopenclaw doctor --fix\nsystemctl --user stop openclaw-gateway.service\nEOF",
    "cat <<EOF\nopenclaw doctor --fix\nEOF\nls",
    "openclaw doctor",
    "openclaw doctor --non-interactive",
    "systemctl --user status openclaw-gateway.service",
    "systemctl --user restart openclaw-watchdog.timer",
    "systemctl --user stop openclaw-watchdog.timer",
    "systemctl --user stop some-other.service",
    "ai-resources openclaw doctor",
    "ai-resources openclaw doctor --dry-run",
    "ssh other-host openclaw doctor --fix",
    "man openclaw",
    "# openclaw doctor --fix",
    "",
]


@pytest.mark.parametrize("command", DENIED)
def test_ac_7_1_an_undrained_stop_is_denied_with_the_alternative(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2
    assert "ai-resources openclaw doctor" in err and "T01" in err


@pytest.mark.parametrize("command", ALLOWED)
def test_ac_7_3_the_false_positive_corpus_is_allowed(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert (code, err) == (0, "")


def test_an_edit_to_a_kit_doc_that_mentions_the_command_is_allowed(guard, monkeypatch, capsys):
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "docs/openclaw/pitfalls.md",
        "new_string": "never run `openclaw doctor --fix` bare; use `systemctl --user stop openclaw-gateway.service`"}})
    assert run(guard, monkeypatch, raw=payload, capsys=capsys) == (0, "")
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x.md",
                                                                 "content": "openclaw doctor --fix"}})
    assert run(guard, monkeypatch, raw=payload, capsys=capsys) == (0, "")


# --- AC-7.2: state ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("command", DENIED[:1] + DENIED[9:10])
def test_a_maintenance_window_lets_the_command_through(guard, monkeypatch, capsys, tmp_path, command):
    (tmp_path / "watchdog.off").write_text("", encoding="utf-8")
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, "")


@pytest.mark.parametrize("command", DENIED[:1] + DENIED[9:10])
def test_an_inactive_gateway_lets_the_command_through(guard, monkeypatch, capsys, command):
    guard.state["active"] = False
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, "")


def test_state_that_cannot_be_determined_fails_open(guard, monkeypatch, capsys):
    def boom():
        raise OSError("no systemd")
    monkeypatch.setattr(guard, "gateway_active", boom)
    assert run(guard, monkeypatch, "openclaw doctor --fix", capsys=capsys) == (0, "")


def test_gateway_active_is_false_when_systemctl_is_missing(monkeypatch):
    mod = _load()

    def missing(*_a, **_k):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(mod.subprocess, "run", missing)
    assert mod.gateway_active() is False


# --- AC-7.4: never noisy, never fatal -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"tool_name": "Bash"}',
                                 '{"tool_name": "Bash", "tool_input": "oops"}',
                                 '{"tool_name": "Bash", "tool_input": {"command": 7}}'])
def test_a_malformed_payload_exits_zero_and_prints_nothing(guard, monkeypatch, capsys, raw):
    assert run(guard, monkeypatch, raw=raw, capsys=capsys) == (0, "")


def test_an_unbalanced_quote_fails_open_instead_of_guessing(guard, monkeypatch, capsys):
    assert run(guard, monkeypatch, 'echo "unterminated openclaw doctor --fix', capsys=capsys) == (0, "")


def test_an_internal_exception_exits_zero(guard, monkeypatch, capsys):
    monkeypatch.setattr(guard, "dangerous", lambda *_a: (_ for _ in ()).throw(RuntimeError("bug")))
    assert run(guard, monkeypatch, "openclaw doctor --fix", capsys=capsys) == (0, "")


def test_the_hook_source_is_english_and_has_no_host_literals():
    text = HOOK.read_text(encoding="utf-8")
    assert not [c for c in text if c.isalpha() and ord(c) > 127]
    for literal in ("/home/bitgandtter", "wildbit", "7961376547"):
        assert literal not in text


# --- registration -----------------------------------------------------------------------------------------------

def test_the_guard_registers_under_the_bash_matcher_and_is_removed_cleanly(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    from ai_resources.setup.cockpits import claude
    path = tmp_path / "settings.json"
    original = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/usr/bin/python3 /u/mine.py"}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")
    claude.install_openclaw_hooks("/kit", team=False, guard=True, settings_path=path)
    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    guard = [e for e in entries if "openclaw_gateway_guard.py" in e["hooks"][0]["command"]]
    assert len(guard) == 1 and guard[0]["matcher"] == "Bash"
    assert claude.install_openclaw_hooks("/kit", team=False, guard=True, settings_path=path) is False
    claude.remove_openclaw_hooks(path)
    assert json.loads(path.read_text(encoding="utf-8")) == original
