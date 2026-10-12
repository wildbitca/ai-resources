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


# --- 2.1.1: `env -S` runs its split string as a command ------------------------------------------------------------

ENV_SPLIT_RESTART_DENIED = [
    "env -S 'systemctl --user restart openclaw-gateway'",
    "env -S'systemctl --user restart openclaw-gateway.service'",
    "env --split-string 'systemctl --user restart openclaw-gateway'",
    "env --split-string='systemctl --user try-restart openclaw-gateway'",
    "env -iS 'openclaw gateway restart'",
    "env -vS 'systemctl --user restart openclaw-gateway'",
    "env -S 'systemctl --user' restart openclaw-gateway",
    "sudo env -S 'openclaw gateway --json restart'",
    "env -S 'bash -c \"systemctl --user restart openclaw-gateway\"'",
    "env --split 'openclaw gateway restart'",
    "env -u HOME -S 'openclaw gateway restart'",
]
ENV_SPLIT_STOP_DENIED = [
    "env -S 'systemctl --user stop openclaw-gateway'",
    "env -S 'openclaw doctor --fix'",
    "env -S 'openclaw gateway stop'",
]
ENV_SPLIT_ALLOWED = [
    "env -S 'echo hi'",
    "env -S 'systemctl --user status openclaw-gateway'",
    "env -S 'grep restart docs/openclaw.md'",
    "env -S 'echo hi; systemctl --user restart openclaw-gateway'",     # env execs echo: the rest are its arguments
    "env -S 'openclaw gateway status'",
    "env FOO=1 echo restart openclaw-gateway",
    "env FOO=1 -S 'openclaw gateway restart'",                          # getopt stops at FOO=1: `-S` is the command
    "env -S 'echo \"unbalanced'",                                       # unparseable and the gateway is not named
]


@pytest.mark.parametrize("command", ENV_SPLIT_RESTART_DENIED)
def test_an_env_split_string_that_restarts_the_gateway_is_denied(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T29" in err


@pytest.mark.parametrize("command", ENV_SPLIT_STOP_DENIED)
def test_an_env_split_string_that_stops_the_gateway_is_denied(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T01" in err


@pytest.mark.parametrize("command", ENV_SPLIT_ALLOWED)
def test_harmless_env_split_strings_stay_allowed(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


def test_an_unparseable_split_string_that_names_the_gateway_fails_closed(guard, monkeypatch, capsys):
    code, err = run(guard, monkeypatch, "env -S 'systemctl --user restart openclaw-gateway \"'", capsys=capsys)
    assert code == 2 and "T29" in err


def test_nested_env_split_strings_are_bounded_and_fail_closed_when_they_name_the_gateway(guard, monkeypatch, capsys):
    nested = "systemctl --user restart openclaw-gateway"
    for _ in range(5):
        nested = f"env -S '{nested}'" if "'" not in nested else "env -S " + __import__("shlex").quote(nested)
    code, err = run(guard, monkeypatch, nested, capsys=capsys)
    assert code == 2 and "T29" in err


# --- 2.1.1 pass 3, L4: the fail-closed fallbacks deny only when a stop/restart verb sits next to the gateway ----------

def _nest(command: str, times: int = 5) -> str:
    import shlex
    for _ in range(times):
        command = "env -S " + shlex.quote(command)
    return command


FALLBACK_ALLOWED = [
    "env -S 'echo \"openclaw gateway'",                          # unparseable, names the gateway, no verb
    "env -S 'echo \"openclaw gateway status'",
    "env -S 'echo \"restart the openclaw logs gateway'",         # the verb is not after the gateway word
    "env -S 'grep \"openclaw-gateway.service /var/log/syslog'",
    _nest("echo openclaw gateway"),                              # past the depth limit, nothing to act on
    _nest("echo openclaw gateway status"),
    _nest("cat docs/openclaw-gateway.md"),
]
FALLBACK_DENIED = [
    "env -S 'openclaw gateway restart \"'",
    "env -S 'openclaw --profile x gateway --port 1 stop \"'",
    "env -S 'systemctl --user kill openclaw-gateway.service \"'",
    "env -S 'sudo systemctl --user try-restart openclaw-gateway \"'",
    "env -S 'systemctl --user stop openclaw-gateway \"'",
    _nest("openclaw gateway restart"),
    _nest("systemctl --user stop openclaw-gateway"),
    _nest("systemctl --user kill openclaw-gateway.service"),
    # the verb split by shell quoting, a backslash or an expansion, and the tail words past the depth limit
    "env -S \"env -S 'env -S \\\"env -S openclaw gateway re\\\\'\\\\'start\\\"'\"",
    "env -S \"openclaw gateway re''start \\\"\"",
    "env -S 'openclaw gateway res\\tart \"'",
    "env -S 'openclaw gateway $V \"'",
    "env -S \"env -S 'env -S \\\"env -S openclaw gateway restart\\\"'\"",
]


@pytest.mark.parametrize("command", FALLBACK_ALLOWED)
def test_a_fallback_without_a_verb_next_to_the_gateway_is_allowed(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


@pytest.mark.parametrize("command", FALLBACK_DENIED)
def test_a_fallback_with_a_verb_next_to_the_gateway_is_still_denied(guard, monkeypatch, capsys, command):
    code, err = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2, command
    assert "T29" in err or "T01" in err


def test_an_internal_error_still_fails_open(guard, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(guard, "shape", boom)
    assert run(guard, monkeypatch, "env -S 'openclaw gateway restart'", capsys=capsys) == (0, "")


# --- kit 2.1.1 pass 4, S2: shell line continuation ------------------------------------------------------------------
# `\<newline>` is removed by the shell outside single quotes, so a verb or a unit may be broken across lines
# at any character, and a quoted `sh -c '...'` string is joined again by the inner shell.

_CONT_FAMILIES = {
    "doctor": ["openclaw doctor --fix", "openclaw doctor --non-interactive --fix"],
    "stop": ["systemctl --user stop openclaw-gateway.service", "systemctl kill openclaw-gateway",
             "openclaw gateway stop", "openclaw gateway --port 1 stop"],
    "restart": ["systemctl --user restart openclaw-gateway", "systemctl --user try-restart openclaw-gateway.service",
                "openclaw gateway restart", "openclaw gateway --json restart"],
}

# Wrapper templates. In `sh -c '...'` the backslash-newline is literal for the outer shell and removed by the inner.
_CONT_WRAPPERS = [
    "{c}",
    "sudo {c}",
    "FOO=1 {c}",
    "cd ~ && {c}",
    "echo hi; {c} | tee out",
    "timeout 5 {c}",
    'bash -c "{c}"',
    "sh -c '{c}'",
    "eval '{c}'",
    'env -S "{c}"',
]


def _continued_at_every_position(cmd: str) -> list[str]:
    return [cmd[:i] + "\\\n" + cmd[i:] for i in range(1, len(cmd))]


@pytest.mark.parametrize("wrapper", _CONT_WRAPPERS)
@pytest.mark.parametrize("family", sorted(_CONT_FAMILIES))
def test_a_line_continuation_at_any_position_does_not_hide_a_guarded_command(guard, family, wrapper):
    missed = []
    for base in _CONT_FAMILIES[family]:
        for broken in _continued_at_every_position(base):
            command = wrapper.format(c=broken)
            if guard.shape(command) != family:
                missed.append(command)
    assert missed == [], f"{len(missed)} continued forms were not recognised, e.g. {missed[:2]!r}"


def test_a_continuation_with_indentation_on_the_next_line_is_denied(guard):
    assert guard.shape("systemctl --user \\\n    restart \\\n    openclaw-gateway") == "restart"
    assert guard.shape("openclaw \\\n\tgateway \\\n  stop") == "stop"
    assert guard.shape("openclaw \\\n doctor \\\n --fix") == "doctor"


def test_a_comment_does_not_continue_onto_the_next_line(guard):
    # the comment ends at the newline, so the next line is its own command
    assert guard.shape("# note \\\nsystemctl --user restart openclaw-gateway") == "restart"
    assert guard.shape("# systemctl --user restart openclaw-gateway \\\nls") is None


def test_an_escaped_backslash_at_the_end_of_a_line_is_not_a_continuation(guard):
    assert guard.shape("echo foo\\\\\nsystemctl --user restart openclaw-gateway") == "restart"
    assert guard.shape("echo foo\\\\\nsystemctl --user status openclaw-gateway") is None


CONTINUATION_ALLOWED = [
    'git commit -m "docs: never run \\\nsystemctl --user restart openclaw-gateway \\\nfrom an agent"',
    'echo "systemctl --user \\\nrestart openclaw-gateway"',
    "echo 'systemctl --user \\\nrestart openclaw-gateway'",
    'grep -rn "openclaw gateway restart" \\\n  docs/ \\\n  README.md',
    "cat <<'EOF' > notes.md\nsystemctl --user \\\nrestart openclaw-gateway\nEOF\nls",
    "cat <<EOF\nopenclaw doctor \\\n--fix\nEOF\nls",
    # a body line that ends in a backslash must not swallow the delimiter and hide what follows
    "cat <<EOF\nfoo \\\nEOF\nsystemctl --user status openclaw-gateway",
    "python3 script.py \\\n  --note 'systemctl --user restart openclaw-gateway' \\\n  --verbose",
    "systemctl --user \\\n  status \\\n  openclaw-gateway.service",
    "systemctl --user \\\n  restart \\\n  openclaw-watchdog.timer",
    "openclaw \\\n  doctor \\\n  --non-interactive",
    "openclaw gateway \\\n  status",
    "ssh other-host \\\n  systemctl --user restart openclaw-gateway",
    "ai-resources openclaw \\\n  graceful-restart \\\n  --dry-run",
    "echo a \\\n&& echo b \\\n&& ls",
]


@pytest.mark.parametrize("command", CONTINUATION_ALLOWED)
def test_benign_multi_line_commands_stay_allowed(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


def test_a_heredoc_delimiter_after_a_backslash_ended_body_line_still_closes_the_body(guard):
    assert guard.shape("cat <<EOF\nfoo \\\nEOF\nsystemctl --user restart openclaw-gateway") == "restart"
    assert guard.shape("cat <<EOF && \\\nsystemctl --user restart openclaw-gateway\nbody\nEOF") == "restart"


def test_a_stray_apostrophe_cannot_hide_the_next_line(guard):
    # the joined text does not tokenise, so the physical lines are read one by one as before
    assert guard.shape("echo it's\nsystemctl --user restart openclaw-gateway") == "restart"
    assert guard.shape("echo it's\nsystemctl --user status openclaw-gateway") is None


# --- kit 2.1.1 pass 5, P1/P2/P8: a quote join must not hide the next command ----------------------------------------
# Inside a double-quoted string the scanner reads `$(...)`, backticks and `${...}` as flat text, so a nested quote
# made it believe a single quote was still open at the end of the line while the shell had closed it, and the
# following lines were joined into an imaginary string. Each row below runs the guarded command in real bash.

_FAMILY_COMMANDS = {
    "doctor": "openclaw doctor --fix",
    "stop-gateway": "openclaw gateway stop",
    "restart-gateway": "openclaw gateway restart",
    "stop-systemctl": "systemctl --user stop openclaw-gateway.service",
    "restart-systemctl": "systemctl --user restart openclaw-gateway.service",
}
_FAMILY_SHAPE = {"doctor": "doctor", "stop-gateway": "stop", "restart-gateway": "restart",
                 "stop-systemctl": "stop", "restart-systemctl": "restart"}

# line 1: a double-quoted string with a command substitution holding an odd inner quote; line 2: the guarded
# command; line 3: a comment with a single quote. {v} is the guarded command.
_QUOTE_JOIN_SHAPES = {
    "cmdsub": 'echo "$(echo "\'")"\n{v}\n# \'',
    "backtick": 'echo "`echo "\'"`"\n{v}\n# \'',
    "param-default": 'echo "${{x:-"\'"}}"\n{v}\n# \'',
    "param-strip": 'echo "${{x#"\'"}}"\n{v}\n# \'',
    "cmdsub-multi-line": 'echo "$(echo a\necho "\'")"\n{v}\n# \'',
    "cmdsub-single-quoted-dq": 'echo "$(echo \'a"b\')"\n{v}\n# \'',
    # P8: after `$$` bash does not read `$'...'` as ANSI-C quoting, `$$` is the PID
    "dollar-dollar-quote": "echo $$'\\'\n{v}\n# '",
    "dollar-dollar-quote-bare": "echo $$'\\'\n{v}",
    # P8: a backtick substitution with an unbalanced double quote later
    "backtick-unbalanced-dq": 'echo `echo "`\n{v}\n# "',
}


@pytest.mark.parametrize("family", sorted(_FAMILY_COMMANDS))
@pytest.mark.parametrize("shape_name", sorted(_QUOTE_JOIN_SHAPES))
def test_a_nested_quote_in_an_expansion_cannot_hide_the_next_line(guard, shape_name, family):
    command = _QUOTE_JOIN_SHAPES[shape_name].format(v=_FAMILY_COMMANDS[family])
    assert guard.shape(command) == _FAMILY_SHAPE[family], repr(command)


@pytest.mark.parametrize("family", sorted(_FAMILY_COMMANDS))
@pytest.mark.parametrize("shape_name", sorted(_QUOTE_JOIN_SHAPES))
def test_the_quote_join_shapes_are_denied_through_the_hook_entry_point(guard, monkeypatch, capsys, shape_name,
                                                                     family):
    command = _QUOTE_JOIN_SHAPES[shape_name].format(v=_FAMILY_COMMANDS[family])
    code, _ = run(guard, monkeypatch, command, capsys=capsys)
    assert code == 2


QUOTE_JOIN_ALLOWED = [
    # the same shapes with a read-only verb in the second line
    'echo "$(echo "\'")"\nopenclaw gateway status\n# \'',
    'echo "`echo "\'"`"\nsystemctl --user status openclaw-gateway\n# \'',
    "echo $$'\\'\nsystemctl --user is-active openclaw-gateway\n# '",
    # multi-line strings and commands that only mention the words
    'git commit -m "fix: guard\n\nnever run openclaw gateway restart by hand"',
    "git commit -m 'fix: guard\n\nnever run systemctl --user restart openclaw-gateway'",
    'echo "first\nsecond: openclaw doctor --fix\nthird"',
    "echo $$\nopenclaw gateway status",
    "echo $$ 'it'\"'\"'s'\nls",
    'echo "$(date)"\nls\necho done',
]


@pytest.mark.parametrize("command", QUOTE_JOIN_ALLOWED)
def test_benign_commands_next_to_the_quote_join_shapes_stay_allowed(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


def test_an_ansi_c_string_with_an_escaped_quote_is_one_string(guard):
    # S2e: `$'a\'b'` is a single ANSI-C string, so the mention inside it is text and the next line is a command
    assert guard.shape("echo $'a\\'b openclaw gateway restart'\nls") is None
    assert guard.shape("echo $'a\\'b'\nopenclaw gateway restart") == "restart"
    assert guard.shape("echo $'a\\'b\nopenclaw gateway restart'\nls") is None


def test_a_backslash_is_literal_inside_single_quotes_across_lines(guard):
    # S2g: the `\` before the closing quote does not escape it, so the string ends on line 1
    assert guard.shape("echo 'a\\'\nopenclaw gateway restart") == "restart"
    assert guard.shape("echo 'a\\\nopenclaw gateway restart'\nls") is None


def test_a_heredoc_operator_continued_onto_the_next_line_still_hides_its_body(guard):
    # S2h: the delimiter is read from the joined text, not from the physical line
    assert guard.shape("cat <<EOF \\\n> out\nopenclaw gateway restart\nEOF\nls") is None
    assert guard.shape("cat \\\n<<EOF\nopenclaw doctor --fix\nEOF\nls") is None
    assert guard.shape("cat <<EOF \\\n> out\nbody\nEOF\nopenclaw gateway restart") == "restart"


def test_a_comment_before_the_next_line_does_not_join_it_to_the_statement(guard):
    # the unquoted newline is a statement separator even when the joined text holds a comment
    assert guard.shape("echo \"$(echo \"'\")\" # note\nopenclaw gateway restart\n# '") == "restart"


# P2: a backslash before CRLF escapes the CR, it is not a continuation; the next line is its own command.
def test_a_backslash_before_crlf_is_not_a_continuation(guard):
    assert guard.shape("echo hi \\\r\nopenclaw gateway restart") == "restart"
    assert guard.shape("echo\\\r\nopenclaw doctor --fix") == "doctor"
    assert guard.shape("true \\\r\nsystemctl --user stop openclaw-gateway") == "stop"


def test_a_backslash_before_lf_still_continues(guard):
    assert guard.shape("openclaw \\\ngateway \\\nrestart") == "restart"
    assert guard.shape("echo a \\\n  openclaw gateway restart") is None


def test_a_carriage_return_or_other_line_separator_is_not_a_line_break(guard):
    # splitlines() also breaks on \v \f \x1c-\x1e \x85 and \u2028; the shell does not
    assert guard.shape("o\\\x0bpenclaw gateway restart") is None
    assert guard.shape("echo a\\\x0c\nopenclaw gateway restart") == "restart"
    assert guard.shape("echo 'a\x85b\nopenclaw gateway restart'") is None


# --- pass 6, M1: heredoc delimiters bash accepts that the guard did not recognise --------------------------------

@pytest.mark.parametrize("delim", [r"\EOF", "'E-F'", '"a.b"', "E.F"])
def test_an_unusual_heredoc_delimiter_still_closes_the_body_so_the_next_line_is_read(guard, delim):
    word = delim.replace("'", "").replace('"', "").replace("\\", "")
    # body with a stray apostrophe, then the delimiter, then a real guarded command
    cmd = f"cat <<{delim}\nit's a body\n{word}\nopenclaw gateway restart"
    assert guard.shape(cmd) == "restart"


def test_a_heredoc_body_that_mentions_the_words_is_still_not_a_command(guard):
    assert guard.shape("cat <<'E-F'\nopenclaw gateway restart\nE-F\nls") is None
    assert guard.shape("cat <<\\EOF\nopenclaw doctor --fix\nEOF\nls") is None


def test_a_here_string_is_not_a_heredoc(guard):
    assert guard.shape("cat <<<word\nopenclaw gateway restart") == "restart"


# --- pass 6, L3: a huge command is not parsed, it is denied only when it names a guarded shape -------------------

def test_a_huge_command_costs_nothing_and_is_denied_only_when_it_names_the_gateway_and_a_verb(guard):
    import time
    big = "echo \"" + ("x " * 200_000) + "\n"
    t = time.time()
    assert guard.shape(big) is None
    assert time.time() - t < 2
    assert guard.shape(big + "\nopenclaw gateway restart") == "restart"
    assert guard.shape(big + "\nopenclaw doctor --fix") == "doctor"
    small = "echo hi\nopenclaw gateway status"
    assert guard.shape(small) is None


def test_an_arithmetic_shift_is_not_a_heredoc_and_hides_nothing(guard):
    # `1 << 2` inside `$(( ))`: the old identifier-only delimiter did not take `2`; neither may the broadened one
    assert guard.shape(": $(( 1 << 2 ))\nopenclaw gateway restart") == "restart"
    assert guard.shape(": $(( 1 << 2 ))\r\nopenclaw doctor --fix") == "doctor"
    assert guard.shape("echo $((1<<3))\nsystemctl --user stop openclaw-gateway") == "stop"


# --- pass 6, V1: a heredoc inside a quoted command substitution is data (the git commit idiom) -------------------

@pytest.mark.parametrize("body", [
    "openclaw doctor --fix is what the runbook says",
    "  openclaw gateway restart",
    "systemctl --user restart openclaw-gateway\\nand then",
])
def test_a_commit_message_heredoc_whose_body_mentions_a_guarded_command_is_allowed(guard, body):
    cmd = f"git commit -m \"$(cat <<'EOF'\nfix: handle the case\n\n{body}\nEOF\n)\""
    assert guard.shape(cmd) is None
    cmd2 = f"gh pr create --title t --body \"$(cat <<'EOF'\n{body}\nEOF\n)\""
    assert guard.shape(cmd2) is None


def test_a_command_after_the_commit_message_heredoc_is_still_read(guard):
    cmd = "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"\nopenclaw gateway restart"
    assert guard.shape(cmd) == "restart"
    cmd = "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" && openclaw doctor --fix"
    assert guard.shape(cmd) == "doctor"


def test_a_huge_multiline_quoted_command_stays_inside_the_hook_timeout(guard):
    import time
    big = "echo \"$(echo x)\n" + ("line of text with 'quotes' \n" * 9000)   # just under the cap
    t = time.time()
    guard.shape(big)
    assert time.time() - t < 5


# =====================================================================================================================
# Kit 2.2, group A. The recogniser reads command substitutions (double quotes, backticks, process substitution,
# unquoted heredoc bodies), env -S by env's own split rules, ANSI-C strings, redirections and a mid-word `#` like
# bash, follows nesting past the old cap, and keeps its cost bounded. Each row below was executed under real bash
# with stub binaries in the differential (see the handoff); the rows are named minimal shapes, not payload dumps.
# =====================================================================================================================

SUBSTITUTION_DENIED = [
    ('echo "$(openclaw gateway restart)"', "restart"),
    ("echo `openclaw gateway restart`", "restart"),
    ('x="$(systemctl --user stop openclaw-gateway.service)"', "stop"),
    ('echo "$(openclaw doctor --fix)"', "doctor"),
    ("cat <(openclaw gateway restart)", "restart"),
    ("echo ${v:-`openclaw gateway stop`}", "stop"),
    ("cat <<EOF\n$(openclaw gateway stop)\nEOF", "stop"),
    ('echo "$(echo "$(openclaw gateway restart)")"', "restart"),
    ("echo \"$(bash -c 'openclaw gateway restart')\"", "restart"),
    ('echo "$( echo \'"\' )"; openclaw gateway stop', "stop"),          # a quote nested in `$( )` cannot hide the rest
    ('echo "$(cat <<< "\'")" && openclaw doctor --fix', "doctor"),
    ("x=`openclaw gateway stop`", "stop"),
    ('git commit -m "$(openclaw gateway restart)"', "restart"),
]


@pytest.mark.parametrize("command,expected", SUBSTITUTION_DENIED)
def test_a_command_substitution_runs_its_command_and_is_read(guard, command, expected):
    assert guard.shape(command) == expected, command


def test_a_command_substitution_is_denied_through_the_hook_entry_point(guard, monkeypatch, capsys):
    code, err = run(guard, monkeypatch, 'echo "$(openclaw gateway restart)"', capsys=capsys)
    assert code == 2 and "T29" in err
    code, err = run(guard, monkeypatch, "echo `systemctl --user stop openclaw-gateway`", capsys=capsys)
    assert code == 2 and "T01" in err


SUBSTITUTION_ALLOWED = [
    'echo "run openclaw gateway restart later"',
    'echo "$(echo openclaw gateway restart)"',
    "echo 'a $(openclaw gateway restart) b'",
    'echo "\\$(openclaw gateway restart)"',                            # an escaped dollar is text
    'echo "\\`openclaw gateway restart\\`"',                           # so is an escaped backtick
    "git commit -m 'never run `openclaw gateway restart` from an agent'",   # backticks inside single quotes
    "cat <<'EOF'\n$(openclaw gateway stop)\n`openclaw doctor --fix`\nEOF",  # a quoted delimiter: the body is data
    'echo "$(date)" && openclaw gateway status',
    'x="$(systemctl --user is-active openclaw-gateway.service)"',
    'gh pr create --title "t" --body "$(cat <<\'EOF\'\nUse `openclaw gateway restart` never.\nEOF\n)"',
    "git commit -m \"$(cat <<'EOF'\nfix: x\n\nThe guard denies `systemctl --user restart openclaw-gateway`.\nEOF\n)\"",
    'echo "$(printf \'%s\' "a\'b")"',
]


@pytest.mark.parametrize("command", SUBSTITUTION_ALLOWED)
def test_text_that_only_mentions_the_words_next_to_a_substitution_stays_allowed(guard, command):
    assert guard.shape(command) is None, command


def test_a_command_after_a_substitution_with_a_nested_quote_is_still_read(guard):
    assert guard.shape('echo "$( echo \'"\' )"\t\nopenclaw gateway \\\nstop') == "stop"
    assert guard.shape('echo "$(echo ")")" | openclaw doctor \\\n\t--fix') == "doctor"


# --- A2: env -S follows env's own split rules ---------------------------------------------------------------------------

ENV_RULES_DENIED = [
    ("env -S 'openclaw\\_gateway\\_restart'", "restart"),               # `\_` is a separator for env -S
    ("env -S 'systemctl\\_--user\\_stop\\_openclaw-gateway'", "stop"),
    ("env -S 'openclaw gateway \\\n restart'", "restart"),               # backslash-newline is removed
    ("env -S 'o\\\npenclaw gateway stop'", "stop"),
    ("env -S 'openclaw gateway ${VERB}'", "restart"),                    # an env expansion where the verb goes
    ("env -S 'systemctl --user ${ACT} openclaw-gateway'", "restart"),
    ("env -S 'openclaw doctor --fix'", "doctor"),
]


@pytest.mark.parametrize("command,expected", ENV_RULES_DENIED)
def test_env_split_string_rules_are_followed(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "env -S 'echo\\_openclaw\\_gateway\\_restart'",                      # one echo with arguments
    "env -S 'echo ${HOME} hi'",                                           # an expansion, no gateway words
    "env -S 'openclaw gateway status ${X}'",                              # the subcommand is `status`
    "env -S 'echo hi \\c openclaw gateway restart'",                     # `\c` ends the string
])
def test_env_split_strings_that_do_not_run_a_guarded_command_stay_allowed(guard, command):
    assert guard.shape(command) is None, command


# --- A3: braces, globs, a far verb, case ---------------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("openclaw gateway {restart,status}", "restart"),
    ("openclaw gateway re{start,}", "restart"),
    ("openclaw gateway 'rest*'", "restart"),
    ("openclaw gateway re?tart", "restart"),
    ("systemctl --user {restart,status} openclaw-gateway", "restart"),
    ("systemctl --user restart openclaw-gateway{,.service}", "restart"),
    ("systemctl --user restart 'openclaw-gate*'", "restart"),
    ("systemctl --user stop 'openclaw-*'", "stop"),
    ("env -S \"env -S 'env -S \\\"env -S openclaw gateway {restart,stop}\\\"'\"", "restart"),
    ("openclaw GATEWAY RESTART", "restart"),                              # the CLI path: case variants are denied
    ("openclaw Gateway Stop", "stop"),
    # a verb far after the gateway word, in a string that cannot be tokenised (the fallback)
    ("env -S 'openclaw gateway --port 1 --bind lan --token x --auth y --password z --ws-log w "
     "--raw-stream-path p --tailscale off restart \"'", "restart"),
])
def test_braces_globs_far_verbs_and_case_variants_that_reach_the_gateway_are_denied(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "openclaw gateway {status,health}",
    "openclaw gateway st?tus",
    "openclaw gateway --port 1 status",
    "systemctl --user {status,show} openclaw-gateway",
    "systemctl --user restart OPENCLAW-GATEWAY.service",                 # systemd unit names are case-sensitive
    "systemctl --user restart 'openclaw-watch*'",
    "systemctl --user status 'openclaw-*'",
    "ls *.log",
    "env -S 'openclaw gateway status --note restart \"'",
])
def test_braces_globs_and_case_that_do_not_reach_the_gateway_stay_allowed(guard, command):
    assert guard.shape(command) is None, command


# --- A4: heredocs, comments --------------------------------------------------------------------------------------------

def test_a_heredoc_operator_inside_quotes_does_not_hide_the_next_lines(guard):
    assert guard.shape('echo "<<EOF"\nopenclaw gateway restart') == "restart"
    assert guard.shape("echo '<<EOF'\nopenclaw gateway restart\nEOF") == "restart"
    assert guard.shape('echo "<<-X $(date)"\nopenclaw doctor --fix') == "doctor"
    # the real thing still hides its body
    assert guard.shape("cat <<EOF\nopenclaw gateway restart\nEOF") is None


def test_a_delimiter_line_with_a_trailing_space_or_cr_is_read_on_the_safe_side(guard):
    # bash does not end the body at `EOF ` or `EOF\r`; the guard does and reads the lines after it as commands:
    # more reading, never less
    assert guard.shape("cat <<EOF\nbody\nEOF \nopenclaw gateway restart\nEOF") == "restart"
    assert guard.shape("cat <<EOF\nbody\nEOF\r\nopenclaw gateway restart\nEOF") == "restart"


def test_a_delimiter_followed_by_a_continuation_ends_the_body_like_bash(guard):
    assert guard.shape("cat <<E\nbody\nE\\\n\nopenclaw doctor --fix") == "doctor"


@pytest.mark.parametrize("opener", ['cat <<""E', 'cat <<E"O"F', "cat <<E\\OF", "cat <<'E'\"O\"'F'"])
def test_a_delimiter_word_bash_assembles_from_pieces_closes_the_body(guard, opener):
    delim = "E" if opener.endswith('""E') else "EOF"
    assert guard.shape(f"{opener}\nbody\n{delim}\nopenclaw gateway stop") == "stop"


def test_a_hash_inside_a_word_is_not_a_comment(guard):
    assert guard.shape("x=a#b openclaw gateway restart") == "restart"
    assert guard.shape("echo ${#X} & openclaw doctor --fix") == "doctor"
    assert guard.shape("echo# a\nopenclaw gateway stop") == "stop"
    assert guard.shape("echo a # openclaw gateway restart") is None
    assert guard.shape("# openclaw gateway restart\nls") is None


# --- A8 (tokeniser fidelity): ANSI-C strings, redirections, braces, function bodies -------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("sh -c $'openclaw gateway stop'", "stop"),
    ("echo $'a\\x27b'; openclaw doctor --fix", "doctor"),
    ("bash -lc \"sh -c \\$'openclaw gateway restart'\"", "restart"),
    ("systemctl --us>er stop openclaw-gateway", "stop"),
    ("systemctl --user 2>/dev/null restart openclaw-gateway", "restart"),
    ("openclaw gateway >/dev/null 2>&1 restart", "restart"),
    ("openclaw<<E doctor --fix\nbody\nE", "doctor"),
    ("env<<E -S 'openclaw gateway stop'\nx\nE", "stop"),
    ("openclaw gateway { restart", "restart"),                            # a brace in an argument is a word
    ("function g { systemctl --user kill openclaw-gateway; }; g", "stop"),
    ("time { openclaw gateway \\restart; }", "restart"),
    ("if true; then function g { openclaw gateway stop; }; fi", "stop"),
])
def test_the_tokeniser_reads_the_words_bash_runs(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "echo hi > openclaw-gateway.log; systemctl --user status openclaw-gateway",
    "echo gateway restart > out.txt",
    "echo $'openclaw gateway restart'",
    "cat file 2>&1 | grep restart",
    "echo { openclaw gateway restart }",                                  # a brace argument of echo
])
def test_redirections_and_ansi_strings_do_not_make_text_a_command(guard, command):
    assert guard.shape(command) is None, command


# --- A5: nesting depth and cost --------------------------------------------------------------------------------------

def _wrap(command: str, times: int, shell: str = "bash -c") -> str:
    import shlex
    for _ in range(times):
        command = f"{shell} {shlex.quote(command)}"
    return command


@pytest.mark.parametrize("times", [4, 5, 6, 7])
def test_four_to_seven_nested_wrappers_are_followed(guard, times):
    assert guard.shape(_wrap("openclaw gateway restart", times)) == "restart"
    assert guard.shape(_wrap("openclaw doctor --fix", times, "eval")) == "doctor"
    assert guard.shape(_wrap("openclaw gateway status", times)) is None


def test_past_the_cap_the_text_is_denied_only_when_it_names_the_gateway_and_a_verb(guard):
    # ten levels of quoting is 118 KB: past the nesting limit (8), under the size cap
    assert len(_wrap("systemctl --user stop openclaw-gateway", 10)) < guard._MAX_COMMAND_CHARS
    assert guard.shape(_wrap("openclaw gateway restart", 10)) == "restart"
    assert guard.shape(_wrap("systemctl --user stop openclaw-gateway", 10, "sh -c")) == "stop"
    assert guard.shape(_wrap("openclaw doctor --fix", 10)) == "doctor"
    assert guard.shape(_wrap("echo hello", 10)) is None
    assert guard.shape(_wrap("openclaw gateway status", 10)) is None
    # and past the size cap the old rule stays: a verb word and the gateway word are enough to deny
    assert guard.shape(_wrap("openclaw gateway restart", 14)) == "restart"


def test_nesting_with_many_branches_stays_inside_the_cost_bound(guard):
    import time
    inner = "\n".join(["echo $(date)"] * 400 + ["openclaw gateway restart"])
    cmd = _wrap(inner, 9)
    t = time.time()
    assert guard.shape(cmd) == "restart"
    assert time.time() - t < 5


def test_a_deep_substitution_nest_does_not_raise(guard):
    deep = "echo " + "$(" * 3000 + "x" + ")" * 3000
    assert guard.shape(deep) is None
    assert guard.shape(deep + "; openclaw gateway restart") == "restart"
    assert guard.shape("echo " + "$(" * 3000 + "openclaw gateway restart" + ")" * 3000) == "restart"


# --- A6: the input cap keeps the hook inside its timeout --------------------------------------------------------------

def test_a_one_megabyte_multi_line_quote_is_answered_under_two_seconds(guard, monkeypatch, capsys):
    import time
    big = 'echo "$(echo x)\n' + ("a line with 'quotes' and `ticks`\n" * 32_000)    # about 1 MB
    assert len(big) > 1_000_000
    t = time.time()
    assert guard.shape(big) is None
    code, _ = run(guard, monkeypatch, big, capsys=capsys)
    assert code == 0
    assert time.time() - t < 2
    t = time.time()
    assert guard.shape(big + "\nopenclaw gateway restart") == "restart"
    assert time.time() - t < 2


# --- A7: a benign corpus of realistic agent commands has no denial -----------------------------------------------------

BENIGN_AGENT_COMMANDS = [
    "git status --short",
    "git diff --stat HEAD~1",
    "git log --oneline -n 20",
    "git add -A && git commit -m 'fix(openclaw): never restart the gateway from an agent call'",
    "git commit -m \"$(cat <<'EOF'\nfix(guard): read command substitutions\n\nThe guard denies `openclaw gateway restart`\nand `systemctl --user restart openclaw-gateway`.\nEOF\n)\"",
    "gh pr create --title \"fix: guard\" --body \"$(cat <<'EOF'\n## Summary\n- reads `$(openclaw gateway restart)` shapes\n\nTest plan: run `openclaw doctor --fix`? never.\nEOF\n)\"",
    "gh pr create --title t --body 'Mentions `openclaw gateway restart` and systemctl --user stop openclaw-gateway'",
    "gh pr comment 12 --body \"Run \\`ai-resources openclaw graceful-restart\\` instead\"",
    "grep -rn \"openclaw gateway restart\" docs/ | head",
    "rg -n 'systemctl --user (stop|restart) openclaw-gateway' docs",
    "sed -i 's/openclaw gateway restart/ai-resources openclaw graceful-restart/' docs/openclaw/runbook.md",
    "awk '/restart/ {print $0}' docs/openclaw/pitfalls.md",
    "cat docs/openclaw/pitfalls.md | grep -n T29",
    "echo \"restart the gateway with: ai-resources openclaw graceful-restart\"",
    "echo \"status: $(systemctl --user is-active openclaw-gateway.service)\"",
    "x=\"$(openclaw gateway status --json)\"; echo \"$x\" | jq .",
    "openclaw gateway status && openclaw gateway health",
    "openclaw gateway logs --follow",
    "ai-resources openclaw graceful-restart --dry-run",
    "ai-resources openclaw doctor --dry-run",
    "systemctl --user status openclaw-gateway.service --no-pager",
    "systemctl --user show openclaw-gateway -p MainPID",
    "systemctl --user restart openclaw-watchdog.timer",
    "journalctl --user -u openclaw-gateway.service --since '10 min ago' | grep -i restart",
    "kubectl --context default rollout restart deployment/web -n apps",
    "docker restart openclaw-sandbox",
    "ssh host 'systemctl --user restart openclaw-gateway'",
    "pytest tests/test_openclaw_gateway_guard.py -q -k 'restart or stop'",
    "python3 -c \"print('openclaw gateway restart')\"",
    "python3 - <<'PY'\nprint('systemctl --user stop openclaw-gateway')\nPY",
    "bash -n scripts/openclaw/openclaw-watchdog.sh",
    "cat <<EOF > notes.md\nopenclaw gateway restart\n$(date)\nEOF",
    "cat > docs/note.md <<'EOF'\n```\nopenclaw doctor --fix\nsystemctl --user stop openclaw-gateway.service\n```\nEOF",
    "for f in docs/*.md; do grep -c restart \"$f\"; done",
    "if [ -f ~/.openclaw/watchdog.off ]; then echo window; fi",
    "env FOO=1 python3 -m pytest tests -q",
    "timeout 30 curl -sS http://127.0.0.1:18789/health",
    "echo 'it'\\''s a note about openclaw gateway restart'",
    "printf '%s\\n' \"openclaw gateway stop\" >> notes.txt",
    "touch ~/.openclaw/watchdog.off && echo ok",
    "tail -n 50 ~/.openclaw/logs/gateway.log 2>&1 | grep -i 'restart'",
    "diff <(openclaw gateway status) <(echo ok)",
    "jq '.gateway.restart' ~/.openclaw/openclaw.json",
    "xargs -n1 echo < list.txt",
    "find . -name '*.md' -exec grep -l 'gateway restart' {} +",
    "echo $((1 << 2))",
    "echo ${#PATH}",
    "git log --grep='gateway restart' --oneline",
    "make test 2>&1 | tail -5",
]


@pytest.mark.parametrize("command", BENIGN_AGENT_COMMANDS)
def test_realistic_agent_commands_are_never_denied(guard, monkeypatch, capsys, command):
    assert run(guard, monkeypatch, command, capsys=capsys) == (0, ""), command


# --- 2.2b section 1: case patterns, shell-fed text, cost, global options, trap/eval, the deep fallback ---------------

@pytest.mark.parametrize("command,expected", [
    # 1a: the `)` that ends a case pattern is not the `)` that ends the substitution
    ("echo $(case 1 in 1) systemctl --user restart openclaw-gateway;; esac)", "restart"),
    ('echo "$(case x in x) openclaw gateway stop;; esac)"', "stop"),
    ("x=$(case $a in (a|b) openclaw gateway restart;; *) true;; esac)", "restart"),
    ("echo $(case $a in a) true;; b) openclaw gateway restart;; esac)", "restart"),
    ("echo $(case $a in a) case $b in b) openclaw doctor --fix;; esac;; esac)", "doctor"),
    ("echo $(case 1 in 1) true;; esac); openclaw gateway restart", "restart"),
    ("echo $(case 1 in 1) (true);; esac) && systemctl --user stop openclaw-gateway", "stop"),
    ("echo $(\ncase 1 in\n1)\nopenclaw gateway restart\n;;\nesac\n)", "restart"),
])
def test_a_case_pattern_inside_a_substitution_does_not_end_it(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "echo $(case 1 in 1) openclaw gateway status;; esac)",
    "echo $(case $a in a) true;; esac) openclaw gateway status",
    "echo $(echo case in x) ; ls",
    "x=$(case \"$1\" in start) echo go;; stop) echo halt;; esac); echo \"$x\"",
])
def test_case_statements_that_run_nothing_guarded_stay_allowed(guard, command):
    assert guard.shape(command) is None, command


@pytest.mark.parametrize("command,expected", [
    # 1b: a heredoc, here-string or pipe that feeds a shell is command text
    ("bash <<EOF\nopenclaw gateway restart\nEOF", "restart"),
    ("sh <<'EOF'\nsystemctl --user stop openclaw-gateway\nEOF", "stop"),
    ("sudo bash -s <<EOF\nopenclaw doctor --fix\nEOF", "doctor"),
    ("cat <<EOF | sh\nopenclaw gateway restart\nEOF", "restart"),
    ("echo 'openclaw gateway restart' | sh", "restart"),
    ("echo openclaw gateway restart | bash", "restart"),
    ("printf '%s\\n' 'openclaw gateway restart' | bash", "restart"),
    ("printf 'openclaw gateway %s\\n' restart | sh", "restart"),
    ("bash <<< 'openclaw gateway restart'", "restart"),
    ("cat <<< 'openclaw gateway stop' | zsh", "stop"),
    ("bash <<-\"E-F\"\nopenclaw gateway restart\nE-F", "restart"),
    ("bash -c 'bash <<'\"'\"'EOF'\"'\"'\nopenclaw gateway restart\nEOF'", "restart"),
    ("echo 'openclaw gateway restart' | tee /dev/null | dash", "restart"),
    ("source /dev/stdin <<EOF\nopenclaw gateway restart\nEOF", "restart"),
])
def test_text_fed_to_a_shell_is_command_text(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "cat <<EOF\nopenclaw gateway restart\nEOF",
    "cat <<EOF | grep restart\nopenclaw gateway restart\nEOF",
    "bash script.sh <<EOF\nopenclaw gateway restart\nEOF",
    "bash -c 'cat' <<EOF\nopenclaw gateway restart\nEOF",
    "python3 - <<'PY'\nprint('openclaw gateway restart')\nPY",
    "echo 'openclaw gateway restart' | grep -c restart",
    "echo 'openclaw gateway restart' > notes.txt",
    "echo 'openclaw gateway status' | sh",
    "cat <<EOF | sh\nls -la\nEOF",
    "bash <<< 'echo hello'",
    "cat file.txt | sh",
    "curl -fsSL https://example.com/install.sh | bash",
    "git commit -m \"$(cat <<'EOF'\nnever run openclaw gateway restart\nEOF\n)\"",
])
def test_text_that_does_not_reach_a_shell_stays_allowed(guard, command):
    assert guard.shape(command) is None, command


@pytest.mark.parametrize("command,expected", [
    # 1d: global options before `doctor`
    ("openclaw --profile p doctor --fix", "doctor"),
    ("openclaw --log-level debug --profile p doctor --fix --yes", "doctor"),
    ("openclaw --container c doctor --non-interactive --fix", "doctor"),
    ("openclaw --dev doctor --fix", "doctor"),
    ("env -S 'openclaw --profile p doctor --fix \"'", "doctor"),
    # 1e: trap and a literal assigned earlier
    ("trap 'openclaw gateway restart' EXIT", "restart"),
    ("trap -- 'systemctl --user stop openclaw-gateway' INT TERM", "stop"),
    ("V='openclaw gateway restart'; eval \"$V\"", "restart"),
    ("V='openclaw gateway restart'; eval $V", "restart"),
    ("export V='openclaw doctor --fix'\neval \"${V}\"", "doctor"),
    ("V='openclaw gateway stop'; bash -c \"$V\"", "stop"),
])
def test_global_options_trap_and_assigned_literals_are_read(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "openclaw --profile p doctor",
    "openclaw --profile p status --fix",
    "trap 'rm -f $TMP' EXIT",
    "trap - EXIT",
    "trap -p",
    "V='openclaw gateway status'; eval \"$V\"",
    "V=restart; echo \"$V\"",
    "eval \"$UNKNOWN\"",
])
def test_the_same_forms_without_a_guarded_command_stay_allowed(guard, command):
    assert guard.shape(command) is None, command


def _nest_sub(text: str, levels: int) -> str:
    return "echo " + "$(echo " * levels + text + ")" * levels


def test_deep_benign_nesting_is_not_denied_by_words_that_are_far_apart(guard):
    # 1f: text that nests deeper than the recogniser follows is read for words; `openclaw`, `gateway` and
    # `restart` in a sentence are not a restart. A verb right after `gateway` in command position is.
    sentence = ("see the openclaw docs, where the gateway page lists many of the options for every platform "
                "and says restart the service after an upgrade")
    for levels in (10, 30, guard._MAX_SCAN_DEPTH + 12):
        assert guard.shape(_nest_sub(sentence, levels)) is None, levels
        assert guard.shape(_nest_sub("openclaw gateway status then restart later", levels)) is None, levels
        if levels < guard._MAX_SCAN_DEPTH:         # followed to the end: `echo` only prints the words
            assert guard.shape(_nest_sub("echo openclaw gateway restart", levels)) is None, levels
        assert guard.shape(_nest_sub("true; openclaw gateway restart", levels)) == "restart", levels
        assert guard.shape(_nest_sub("true; systemctl --user stop openclaw-gateway", levels)) == "stop", levels
        assert guard.shape(_nest_sub("true; openclaw --profile p gateway --port 1 --bind lan stop", levels)) == "stop"


def test_the_fallback_unit_window_is_two_near_words(guard):
    # 1g: in a string that cannot be tokenised a verb up to 2 * _NEAR words before the unit is read, one more is not
    near = guard._NEAR
    assert guard.shape("env -S 'restart" + " a" * (2 * near - 1) + " openclaw-gateway \"'") == "restart"
    assert guard.shape("env -S 'restart" + " a" * (2 * near) + " openclaw-gateway \"'") is None


@pytest.mark.parametrize("command,expected", [
    ('echo "`openclaw gateway restart`"', "restart"),
    ('x="`systemctl --user stop openclaw-gateway`"', "stop"),
    ('echo "run `openclaw gateway status` first"', None),
    ("echo# openclaw gateway restart", None),
    ("echo #openclaw gateway restart", None),
    ("echo a# openclaw gateway restart", None),
    ("echo# a; openclaw gateway restart", "restart"),
    ("x=a#b openclaw gateway restart", "restart"),
])
def test_backticks_in_double_quotes_and_mid_word_hash_are_read_like_bash(guard, command, expected):
    assert guard.shape(command) == expected, command


def _timed(guard, command, limit=3.0):
    import time
    t = time.time()
    result = guard.shape(command)
    elapsed = time.time() - t
    assert elapsed < limit, f"{elapsed:.2f}s for {len(command)} bytes"
    return result


def test_28000_openclaw_words_in_a_deep_nest_cost_less_than_three_seconds(guard):
    # 1c: the window of an `openclaw` word is bounded, and the fallback is charged against the work budget
    deep9 = "$(echo " * 9 + "openclaw " * 28000 + ")" * 9
    assert _timed(guard, deep9) is None
    deep12 = "echo " + "$(echo " * 12 + "openclaw " * 27000 + ")" * 12
    assert _timed(guard, deep12 + "; openclaw gateway restart") == "restart"
    assert _timed(guard, "'x\n" + "openclaw doctor " * 14000) is None


def test_thousands_of_heredoc_operators_cost_less_than_three_seconds(guard):
    group = 'echo "' + "".join(f"$(cat<<D{i}\nD{i}\n)\n" for i in range(6000)) + '"'
    assert _timed(guard, group) is None
    same = "cat " + "<<E " * 14000 + "\n" + "E\n" * 14000          # one body per operator
    assert _timed(guard, same) is None
    assert _timed(guard, same + "bash <<X\nopenclaw gateway restart\nX\n") == "restart"
    many = "".join(f"cat <<D{i}\nx\nD{i}\n" for i in range(4000))
    assert _timed(guard, many + "openclaw gateway restart") == "restart"


def test_the_one_megabyte_multi_line_quote_stays_under_three_seconds(guard):
    big = 'echo "$(echo x)\n' + ("a line with 'quotes' and `ticks`\n" * 32_000)
    assert _timed(guard, big) is None
    assert _timed(guard, big + "\nopenclaw gateway restart") == "restart"


@pytest.mark.parametrize("command,expected", [
    # commands that other commands run: xargs, find -exec, parallel; an alias value; a locale string
    ("echo x | xargs sh -c 'openclaw gateway restart'", "restart"),
    ("echo x | xargs -n1 -I{} openclaw gateway restart", "restart"),
    ("printf x | xargs -0 bash -c 'systemctl --user stop openclaw-gateway'", "stop"),
    ("find . -maxdepth 0 -exec systemctl --user restart openclaw-gateway \;", "restart"),
    ("find . -maxdepth 0 -exec bash -c 'openclaw doctor --fix' {} +", "doctor"),
    ("parallel openclaw gateway restart ::: a b", "restart"),
    ("alias gr='openclaw gateway restart'", "restart"),
    ("alias zz='echo $(systemctl --user stop openclaw-gateway)'; zz", "stop"),
    ("eval $\"openclaw gateway restart\"", "restart"),
    ("eval $\"eval \\$\\\"openclaw doctor --fix\\\"\"", "doctor"),
    ("echo openclaw doctor --fix | sh", "doctor"),
])
def test_commands_run_by_xargs_find_parallel_aliases_and_locale_strings_are_read(guard, command, expected):
    assert guard.shape(command) == expected, command


@pytest.mark.parametrize("command", [
    "echo x | xargs -n1 echo openclaw gateway restart",
    "find . -name '*.md' -exec grep -l 'gateway restart' {} +",
    "parallel echo ::: openclaw gateway restart",
    "alias ll='ls -la'",
    "alias gs='git status'; gs",
    "echo $\"restart the openclaw gateway\"",
    "xargs -n1 echo < list.txt",
])
def test_the_same_wrappers_around_harmless_commands_stay_allowed(guard, command):
    assert guard.shape(command) is None, command


def test_the_word_fallback_does_not_read_a_data_command_as_a_command(guard):
    # a string that cannot be tokenised is read for words: `echo`/`grep` only print their arguments
    assert guard.shape("env -S 'echo openclaw gateway restart \"'") is None
    assert guard.shape("env -S 'grep -r openclaw gateway stop dir \"'") is None
    assert guard.shape("env -S 'bash -c \"echo openclaw gateway stop\" \"'") is None
    # but not when the text is fed to a shell or evaluated, or sits after a separator
    assert guard.shape("env -S 'echo openclaw gateway restart | sh \"'") == "restart"
    assert guard.shape("env -S 'echo hi; openclaw gateway restart \"'") == "restart"
    assert guard.shape("env -S 'eval \"$(echo openclaw gateway restart)\" \"'") == "restart"
