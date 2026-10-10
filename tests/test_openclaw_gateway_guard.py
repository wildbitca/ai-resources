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


# --- ADR-0004 (D10): `openclaw gateway restart` from an agent call --------------------------------------------------

RESTART_DENIED = [
    "openclaw gateway restart",
    "/home/linuxbrew/.linuxbrew/bin/openclaw gateway restart",
    "timeout 600 openclaw gateway restart",
    "cd ~ && openclaw gateway restart",
    'bash -c "openclaw gateway restart"',
    "sudo openclaw --profile x gateway restart",
]

RESTART_ALLOWED = [
    "openclaw gateway status",
    "openclaw gateway start",
    "openclaw gateway --help",
    'grep -rn "openclaw gateway restart" docs/',
    'echo "openclaw gateway restart"',
    "ai-resources openclaw graceful-restart --dry-run",
    "ai-resources openclaw graceful-restart --reason manual",
]


@pytest.mark.parametrize("command", RESTART_DENIED)
def test_a_gateway_restart_is_denied_while_the_gateway_is_live(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2
    assert "T29" in err and "ai-resources openclaw graceful-restart" in err


@pytest.mark.parametrize("command", RESTART_ALLOWED)
def test_gateway_status_and_the_sanctioned_cli_are_not_matched(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, "")


def test_a_gateway_restart_is_allowed_when_watchdog_off_is_present(guard, monkeypatch, capsys, tmp_path):
    (tmp_path / "watchdog.off").write_text("", encoding="utf-8")
    assert run(guard, monkeypatch, "openclaw gateway restart", capsys=capsys) == (0, "")


def test_a_gateway_restart_is_allowed_when_the_gateway_is_inactive(guard, monkeypatch, capsys):
    guard.state["active"] = False
    assert run(guard, monkeypatch, "openclaw gateway restart", capsys=capsys) == (0, "")


# --- equivalent forms of the same stop and restart (D10 coverage) ------------------------------------------------

EQUIVALENT_RESTART_DENIED = [
    "systemctl --user restart openclaw-gateway",
    "systemctl --user restart openclaw-gateway.service",
    "systemctl restart openclaw-gateway.service",
    "systemctl --user try-restart openclaw-gateway.service",
    "systemctl --user reload-or-restart openclaw-gateway.service",
    "systemctl --user --no-block restart openclaw-gateway.service",
    "systemctl --user restart --no-block openclaw-gateway.service",
    "systemctl --user restart openclaw-gateway.service openclaw-watchdog.timer",
    "/usr/bin/systemctl --user restart openclaw-gateway.service",
    "openclaw gateway --json restart",
    "openclaw gateway --port 18789 restart",
    "openclaw --profile dev gateway restart",
    "openclaw --log-level debug gateway --json restart",
    "setsid openclaw gateway restart",
    "setsid -f openclaw gateway restart",
    "nohup openclaw gateway restart &",
    "env OPENCLAW_X=1 openclaw gateway restart",
    "env -i PATH=/usr/bin openclaw gateway restart",
    "sudo -n systemctl --user restart openclaw-gateway.service",
    "timeout 30 systemctl --user restart openclaw-gateway.service",
    "timeout -s KILL 30 openclaw gateway restart",
    "bash -c 'systemctl --user restart openclaw-gateway.service'",
    "bash -lc 'openclaw gateway restart'",
    "sh -c \"openclaw gateway --json restart\"",
    "eval 'openclaw gateway restart'",
    "eval openclaw gateway restart",
    "sudo bash -c 'setsid openclaw gateway restart'",
    "bash -c 'bash -c \"systemctl --user restart openclaw-gateway\"'",
    "true && systemctl --user restart openclaw-gateway.service",
]

EQUIVALENT_STOP_DENIED = [
    "systemctl --user kill openclaw-gateway",
    "systemctl --user kill openclaw-gateway.service",
    "systemctl --user kill -s KILL openclaw-gateway.service",
    "systemctl --user --signal=SIGKILL kill openclaw-gateway.service",
    "systemctl stop openclaw-gateway.service",
    "systemctl --user --no-block stop openclaw-gateway.service",
    "openclaw gateway stop",
    "openclaw gateway --json stop",
    "setsid openclaw gateway stop",
    "env FOO=1 systemctl --user stop openclaw-gateway.service",
    "bash -c 'systemctl --user kill openclaw-gateway'",
    "eval \"systemctl --user stop openclaw-gateway.service\"",
]

EQUIVALENT_ALLOWED = [
    # read-only verbs on the gateway unit
    "systemctl --user status openclaw-gateway.service",
    "systemctl --user show openclaw-gateway.service -p MainPID --value",
    "systemctl --user show openclaw-gateway -p ControlGroup -p MainPID",
    "systemctl --user is-active openclaw-gateway.service",
    "systemctl --user is-enabled openclaw-gateway.service",
    "systemctl --user cat openclaw-gateway.service",
    "systemctl --user list-units 'openclaw-*'",
    "systemctl --user list-units --type=service --all",
    "systemctl --user list-dependencies openclaw-gateway.service",
    "systemctl --user start openclaw-gateway.service",
    "systemctl --user reload openclaw-gateway.service",
    "systemctl --user daemon-reload",
    "journalctl --user -u openclaw-gateway.service --since '1 hour ago' --no-pager",
    "journalctl --user -u openclaw-gateway -f",
    "openclaw logs --follow",
    "openclaw gateway status",
    "openclaw gateway --json status",
    "openclaw gateway status --note restart",
    "openclaw gateway --port 18789 status",
    "openclaw gateway health",
    "openclaw gateway --help",
    # another unit with the same verbs
    "systemctl --user restart openclaw-watchdog.timer",
    "systemctl --user kill openclaw-other.service",
    "systemctl --user stop openclaw-gateway-helper.service",
    # the verb as the VALUE of an option, or the unit named only in text
    "systemctl --user show openclaw-gateway.service -p restart",
    "systemctl --user -p restart show openclaw-gateway.service",
    'echo "systemctl --user restart openclaw-gateway.service"',
    "grep -rn 'openclaw gateway --json restart' docs/",
    "git commit -m 'docs: setsid openclaw gateway restart is guarded'",
    "cat <<'EOF'\nsystemctl --user restart openclaw-gateway.service\nEOF",
    "bash -c 'echo systemctl --user restart openclaw-gateway.service'",
    "bash -c 'systemctl --user status openclaw-gateway.service'",
    "bash script.sh restart openclaw-gateway",
    "eval 'echo openclaw gateway restart'",
    "setsid openclaw gateway status",
    "ai-resources openclaw graceful-restart --reason manual",
]


@pytest.mark.parametrize("command", EQUIVALENT_RESTART_DENIED)
def test_equivalent_restart_forms_are_denied_with_the_graceful_restart_alternative(guard, monkeypatch, capsys,
                                                                                 command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T29" in err and "ai-resources openclaw graceful-restart" in err


@pytest.mark.parametrize("command", EQUIVALENT_STOP_DENIED)
def test_equivalent_stop_forms_are_denied_with_the_doctor_alternative(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T01" in err and "ai-resources openclaw doctor" in err


@pytest.mark.parametrize("command", EQUIVALENT_ALLOWED)
def test_read_only_and_look_alike_commands_stay_allowed(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


@pytest.mark.parametrize("command", EQUIVALENT_RESTART_DENIED[:4] + EQUIVALENT_STOP_DENIED[:3])
def test_equivalent_forms_step_aside_for_a_maintenance_window_or_a_stopped_gateway(guard, monkeypatch, capsys,
                                                                                 tmp_path, command):
    guard.state["active"] = False
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, "")
    guard.state["active"] = True
    (tmp_path / "watchdog.off").write_text("", encoding="utf-8")
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, "")


# --- option tables: which options take a value (audited against the host's --help) --------------------------------
# `-T` is --show-transaction, a boolean flag in systemctl: it must not swallow the verb. Short options that DO
# take a value (-C -H -M -t -p -P -s -n -o) must be skipped together with that value, alone or in a cluster.

OPTION_TABLE_RESTART_DENIED = [
    # systemctl boolean short flags, alone and combined
    "systemctl --user -T restart openclaw-gateway",
    "systemctl --user -T restart openclaw-gateway.service",
    "systemctl -T --user restart openclaw-gateway.service",
    "systemctl --user -aT restart openclaw-gateway",
    "systemctl --user -Ta restart openclaw-gateway",
    "systemctl --user -lq restart openclaw-gateway",
    "systemctl --user -fT try-restart openclaw-gateway",
    "systemctl --user --show-transaction restart openclaw-gateway",
    # value-taking short options: the value is not the verb
    "systemctl --user -p Foo restart openclaw-gateway",
    "systemctl --user -pFoo restart openclaw-gateway",
    "systemctl --user -ap Foo restart openclaw-gateway",
    "systemctl --user -aTp Foo restart openclaw-gateway",
    "systemctl --user -P Foo restart openclaw-gateway",
    "systemctl --user -t service restart openclaw-gateway",
    "systemctl --user -n 5 restart openclaw-gateway",
    "systemctl --user -o json restart openclaw-gateway",
    "systemctl -M mycontainer restart openclaw-gateway",
    "systemctl -H user@host -T restart openclaw-gateway",
    "systemctl -C capsule restart openclaw-gateway",
    "systemctl --user -s KILL -T restart openclaw-gateway",
    # long options: separate value, attached value, an unambiguous prefix
    "systemctl --user --property Foo restart openclaw-gateway",
    "systemctl --user --property=Foo restart openclaw-gateway",
    "systemctl --user --prop Foo restart openclaw-gateway",
    "systemctl --user --job-mode replace restart openclaw-gateway",
    "systemctl --user --kill-whom main restart openclaw-gateway",
    "systemctl --user --timestamp unix restart openclaw-gateway",
    # openclaw gateway: a value-taking option holding a verb-looking word is that option's value
    "openclaw gateway --token status restart",
    "openclaw gateway --password health restart",
    "openclaw gateway --ws-log compact restart",
    "openclaw gateway --port=18789 restart",
    "openclaw --profile gateway gateway restart",
    "openclaw --container c --log-level debug gateway restart",
    # wrappers with a value-taking option the table used to miss
    "sudo -r sysadm_r systemctl --user restart openclaw-gateway",
    "sudo -t sysadm_t systemctl --user restart openclaw-gateway",
    "sudo -u root -g wheel systemctl --user -T restart openclaw-gateway",
    "sudo --prompt x systemctl --user restart openclaw-gateway",
    "stdbuf -o L systemctl --user restart openclaw-gateway",
    "stdbuf -oL openclaw gateway restart",
    "/usr/bin/time -f %e systemctl --user restart openclaw-gateway",
    "time -o out.txt openclaw gateway restart",
    "exec -a name systemctl --user restart openclaw-gateway",
    "env -a name openclaw gateway restart",
    "env -u FOO -C /tmp openclaw gateway restart",
    "ionice -c 3 -n 7 openclaw gateway restart",
    "ionice -u 1000 -t openclaw gateway restart",
    "nice -n 5 openclaw gateway restart",
    "timeout -vs KILL 30 openclaw gateway restart",
    "timeout -v -k 5 30 openclaw gateway restart",
    "timeout --signal KILL 30 openclaw gateway restart",
    "timeout --kill-after=5 30 openclaw gateway restart",
    # shells: an option value ahead of -c
    "bash -o pipefail -c 'systemctl --user restart openclaw-gateway'",
    "bash -O extglob -c 'openclaw gateway restart'",
    "bash --rcfile /dev/null -c 'openclaw gateway restart'",
    "sh -eu -c 'systemctl --user -T restart openclaw-gateway'",
]

OPTION_TABLE_STOP_DENIED = [
    "systemctl --user -T stop openclaw-gateway",
    "systemctl --user -aT kill openclaw-gateway",
    "systemctl --user -ap Foo stop openclaw-gateway",
    "systemctl --user -T kill -s KILL openclaw-gateway",
    "openclaw gateway --token status stop",
]

OPTION_TABLE_ALLOWED = [
    "systemctl --user -T status openclaw-gateway",
    "systemctl --user -aT status openclaw-gateway.service",
    "systemctl --user -T is-active openclaw-gateway",
    "systemctl --user -T start openclaw-gateway",
    "systemctl --user -ap restart show openclaw-gateway.service",
    "systemctl --user -pFoo show openclaw-gateway.service",
    "systemctl --user -T show openclaw-gateway -p restart",
    "systemctl --user -T restart openclaw-watchdog.timer",
    "systemctl --user -s TERM -T restart openclaw-other.service",
    "systemctl --user --property restart show openclaw-gateway.service",
    "openclaw gateway --token restart status",
    "openclaw gateway --password stop health",
    "openclaw --profile gateway sessions restart",
    "sudo -r sysadm_r systemctl --user status openclaw-gateway",
    "stdbuf -o L openclaw gateway status",
    "timeout -vs KILL 30 openclaw gateway status",
    "bash -o pipefail -c 'systemctl --user status openclaw-gateway'",
    "bash -o pipefail script.sh restart openclaw-gateway",
]


@pytest.mark.parametrize("command", OPTION_TABLE_RESTART_DENIED)
def test_option_value_tables_never_hide_a_restart(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T29" in err


@pytest.mark.parametrize("command", OPTION_TABLE_STOP_DENIED)
def test_option_value_tables_never_hide_a_stop(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T01" in err


@pytest.mark.parametrize("command", OPTION_TABLE_ALLOWED)
def test_option_value_tables_keep_the_false_positive_guards(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


@pytest.mark.parametrize("short, takes_value", [
    ("-T", False), ("-a", False), ("-l", False), ("-q", False), ("-r", False), ("-f", False), ("-v", False),
    ("-i", False), ("-p", True), ("-P", True), ("-t", True), ("-s", True), ("-n", True), ("-o", True),
    ("-H", True), ("-M", True), ("-C", True),
])
def test_the_systemctl_short_option_classification_matches_the_host_help(guard, short, takes_value):
    verb, operands = guard._systemctl_verb(["systemctl", short, "restart", "openclaw-gateway"])
    if takes_value:
        assert verb == "openclaw-gateway", short       # `restart` was consumed as the option's value
    else:
        assert (verb, operands) == ("restart", ["openclaw-gateway"]), short
