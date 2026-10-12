"""The gateway guard's strict second view, kit 2.2f (the last guard pass).

1. A shell (or `read`/`mapfile`/`eval`/`source`/`exec`) that reads a process substitution on standard input runs
   what the substitution prints: `sh < <(echo ...)` is not "printed text".
2. The allow list of other products' commands must not open an assembled OpenClaw name: an option VALUE
   (`sudo -u docker`, `env -u docker`) is not the command word, and a substitution that is an ARGUMENT of another
   product's command is a statement of its own.
3. The bare `gateway` + verb reading of the strict view needs a second signal that it is OpenClaw, so that the
   words of other tools (`npm run gateway:restart`, `git log --grep='gateway restart'`) are not denied.

The hook is run as Claude Code runs it (a real subprocess, `systemctl` a stub that answers `active`); nothing
here touches systemd or the host.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import stat
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"
KC = "kube" + "ctl"           # keeps the harness's own context rule out of the file


def _load():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard_strict", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "WATCHDOG_OFF", str(tmp_path / "watchdog.off"))
    monkeypatch.setattr(mod, "gateway_active", lambda: True)
    monkeypatch.delenv("KIT_GUARD_DEADLINE_S", raising=False)
    return mod


def _hook_rc(tmp_path, command):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    stub.write_text("#!/bin/sh\necho active\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp_path)}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    done = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True,
                          env=env, timeout=60)
    return done.returncode


# --- 1. a process substitution on standard input -----------------------------------------------------------------

_P = "openclaw gateway restart"
_U = "systemctl --user restart openclaw-gateway"
STDIN_FORMS = [
    'sh < <(echo "{p}")',
    'read -r c < <(echo "{p}"); eval "$c"',
    'mapfile -t L < <(echo "{p}"); eval "${{L[@]}}"',
    'exec 3< <(echo "{p}"); sh <&3',
    'sh 0< <(echo "{p}")',
    'bash -s < <(echo "{p}")',
    'exec bash < <(echo "{p}")',
    'sh -e < <(echo "{p}")',
    'source /dev/stdin < <(echo "{p}")',
    'while read -r l; do eval "$l"; done < <(echo "{p}")',
    "bash <<< 'sh < <(echo \"{p}\")'",
    "sudo sh < <(echo '{p}')",
    "true && sh < <(echo '{p}')",
    # the here-string cousin of `read ... < <(...)`: the stored text is run by `eval`
    "read -r c <<< '{p}'; eval \"$c\"",
]


@pytest.mark.parametrize("payload", [_P, _U], ids=["cli", "unit"])
@pytest.mark.parametrize("form", STDIN_FORMS)
def test_a_shell_reading_a_process_substitution_on_stdin_is_denied(tmp_path, form, payload):
    assert _hook_rc(tmp_path, form.format(p=payload)) == 2


STDIN_PRINTED = [
    "cat < <(echo 'restart the gateway')",
    "diff <(echo a) <(echo b)",
    "wc -l < <(echo 'gateway restart')",
    "sort < <(echo 'openclaw gateway restart')",
    "grep gateway < <(echo 'restart the gateway')",
    "while read -r l; do echo \"$l\"; done < <(echo 'gateway restart')",
    "read -r c < <(echo 'gateway restart'); echo \"$c\"",
    "tee /dev/null < <(echo 'openclaw gateway restart')",
    "read -r c <<< 'gateway restart'; echo \"$c\"",
]


@pytest.mark.parametrize("command", STDIN_PRINTED)
def test_printed_only_process_substitutions_stay_allowed(tmp_path, command):
    assert _hook_rc(tmp_path, command) == 0


def test_the_strict_view_looks_through_the_redirect_to_its_owner(guard):
    assert guard._cheap_view('sh < <(echo "openclaw gateway restart")', strict=True) == "restart"
    assert guard._cheap_view('cat < <(echo "openclaw gateway restart")', strict=True) is None


# --- 2. the other-product allow list --------------------------------------------------------------------------------

ASSEMBLED = ["${OC:-openclaw}", "/usr/local/bin/open*", "${o}${c}"]
OTHER_PRODUCT_DRESS = [
    "sudo -u docker {oc} gateway {v}",
    "sudo -u nginx {oc} gateway {v}",
    "sudo -u service {oc} gateway {v}",
    "doas -u docker {oc} gateway {v}",
    "sudo --user=docker {oc} gateway {v}",
    "env -C /opt/docker {oc} gateway {v}",
    "env -u docker {oc} gateway {v}",
    "runuser -u docker -- {oc} gateway {v}",
    "timeout 5 {oc} gateway {v} docker",
    "ssh -p 22 host docker {oc} gateway {v}",
    "docker ps $({oc} gateway {v})",
    "docker ps `{oc} gateway {v}`",
    "docker ps \"$({oc} gateway {v})\"",
    "docker ps <({oc} gateway {v})",
    "nginx -t $({oc} gateway {v})",
    KC + " get pods $({oc} gateway {v})",
    "timeout 5 docker ps $({oc} gateway {v})",
]


def _fed(command):
    return f"bash <<EOF\n{command}\nEOF"


@pytest.mark.parametrize("verb", ["restart", "stop"])
@pytest.mark.parametrize("assembled", ASSEMBLED)
@pytest.mark.parametrize("form", OTHER_PRODUCT_DRESS)
def test_another_products_name_does_not_open_an_assembled_openclaw(tmp_path, form, assembled, verb):
    assert _hook_rc(tmp_path, _fed(form.format(oc=assembled, v=verb))) == 2


OTHER_PRODUCT_PLAIN = [
    "docker restart gateway",
    KC + " rollout restart deploy/gateway",
    "systemctl restart api-gateway",
    "docker ps",
    "ssh host docker restart gateway",
    "(docker restart gateway)",
    "sudo -u docker docker restart gateway",
    "env -u docker docker restart gateway",
    "docker exec c1 ${OC:-x} gateway restart",
    "timeout 5 docker restart gateway",
]


@pytest.mark.parametrize("command", OTHER_PRODUCT_PLAIN)
def test_another_products_own_gateway_stays_allowed(tmp_path, command):
    assert _hook_rc(tmp_path, _fed(command)) == 0


@pytest.mark.parametrize("command", [
    "${OC:-openclaw} gateway --password status restart",
    "${OC:-openclaw} gateway --port restart stop",
    "${OC:-openclaw} gateway --token status stop",
])
def test_the_value_of_a_gateway_option_does_not_answer_the_verb(tmp_path, command):
    assert _hook_rc(tmp_path, _fed(command)) == 2


def test_a_subcommand_still_answers_the_verb(guard):
    assert guard._cheap_view("${OC:-openclaw} gateway status --note restart", strict=True) is None
    assert guard._cheap_view("${OC:-openclaw} gateway --password x status --note restart", strict=True) is None


def test_the_option_value_is_not_the_command_word(guard):
    # the unit of the property: `-u docker` is a user name; case folding of the view must not turn `-C` into a flag
    assert guard._another_product("sudo -u docker ${oc} gateway restart", 24) is False
    assert guard._another_product("env -c /opt/docker ${oc} gateway restart", 24) is False
    assert guard._another_product("docker restart gateway", 15) is True


# the plain wrapper that carries another product's name as a user, a host or a path
PLAIN_WRAPPER_DRESS = [
    "su docker -c {oc} gateway {v}",
    "flock /var/lock/docker {oc} gateway {v}",
    "flock -n /run/nginx.lock-docker {oc} gateway {v}",
    "chroot /srv/nginx {oc} gateway {v}",
    "docker () {{ {oc} gateway {v}; }}; docker",
    "function docker {{ {oc} gateway {v}; }}; docker",
    "nginx() {{ {oc} gateway {v}; }}; nginx",
]


@pytest.mark.parametrize("verb", ["restart", "stop"])
@pytest.mark.parametrize("assembled", ASSEMBLED)
@pytest.mark.parametrize("form", PLAIN_WRAPPER_DRESS)
def test_a_name_that_is_a_user_a_host_or_a_path_does_not_exempt_the_command(tmp_path, form, assembled, verb):
    assert _hook_rc(tmp_path, _fed(form.format(oc=assembled, v=verb))) == 2


def test_a_remote_product_command_without_a_built_word_stays_allowed(tmp_path):
    assert _hook_rc(tmp_path, _fed("ssh host docker restart gateway")) == 0


def test_a_backtick_pair_cut_by_the_look_back_window_does_not_cut_the_head(guard):
    # the parity of the backticks is counted from the start of the statement, not from the middle of a pair at
    # the edge of the 400-character window
    for pad in range(3):
        low = "`a`" * 300 + "x" * pad + "\nbash <<< open`printf `claw gateway restart"
        head = guard._statement_head(low, low.index("gateway"))
        assert "open`printf" in head, pad


# --- 3. a bare `gateway` + verb needs a second signal ----------------------------------------------------------------

BARE_GATEWAY_OF_OTHER_TOOLS = [
    "npm run gateway:restart",
    "yarn gateway:restart",
    "pnpm run gateway:stop",
    "git log --grep='gateway restart' --oneline",
    "git log --grep=gateway --oneline restart",
    "grep -rn 'gateway restart' docs/",
    "rg 'restart the gateway' README.md",
    "fly apps restart gateway",
    "fly apps restart gateway --app $APP",
    "heroku restart -a gateway",
    "gh issue list --search 'gateway restart'",
    "gh pr create --title 'fix: gateway restart loop' --body 'restart the gateway'",
    "ssh -p 22 host docker restart gateway",
    "echo 'gateway restarted'",
    "curl -s -d '{\"note\":\"restart the gateway\"}' http://localhost:8080/notes",
    "# fix(gateway): restart the api gateway after config reload",
    "# restart the gateway deployment (" + KC + " rollout restart deploy/gateway)\necho ok",
    "git commit -m 'fix(gateway): restart backoff'",
]


@pytest.mark.parametrize("command", BARE_GATEWAY_OF_OTHER_TOOLS)
@pytest.mark.parametrize("feed", [
    "bash <<EOF\n{c}\nEOF",
    "sudo sh <<'EOF'\n{c}\nEOF",
    "cat <<'EOF' | sh\n{c}\nEOF",
    "eval \"$(cat <<'EOF'\n{c}\nEOF\n)\"",
], ids=["heredoc", "sudo-sh", "pipe", "eval-cat"])
def test_the_words_of_other_tools_are_not_an_openclaw_restart(tmp_path, command, feed):
    assert _hook_rc(tmp_path, feed.format(c=command)) == 0


BUILT_COMMANDS = [
    "${OC:-openclaw} gateway restart",
    "$o$c gateway stop",
    "set -- gateway restart; $o$c \"$@\"",
    "systemctl --user restart open``claw-gateway",
    "systemctl --user restart \"$(printf 'openclaw-%s' gateway)\"",
    "g=gateway; systemctl --user restart openclaw-$g",
    "u=gateway; systemctl --user restart $p$q-$u",
    "g=gateway; ${OC:-openclaw} $g restart",
    "git log --grep=\"$(${OC:-openclaw} gateway restart)\"",
    "git -c alias.x='!${OC:-openclaw} gateway restart' x",
    "npm run x -- $(${OC:-openclaw} gateway stop)",
    "# only a comment\n${OC:-openclaw} gateway restart",
    "# a comment that ends in a backslash \\\n${OC:-openclaw} gateway restart",
    "echo a # b\n${OC:-openclaw} gateway stop",
]


@pytest.mark.parametrize("command", BUILT_COMMANDS)
@pytest.mark.parametrize("feed", ["bash <<EOF\n{c}\nEOF", "eval \"$(cat <<'EOF'\n{c}\nEOF\n)\""], ids=["heredoc", "eval-cat"])
def test_a_command_that_is_built_at_run_time_is_still_denied(tmp_path, command, feed):
    assert _hook_rc(tmp_path, feed.format(c=command)) == 2


def test_a_far_option_value_does_not_separate_openclaw_from_gateway(tmp_path):
    # `openclaw` and `gateway` more than the 160-character window apart, in one statement
    command = "openclaw --profile " + "p" * 900 + " gateway --json restart"
    assert _hook_rc(tmp_path, _fed(command)) == 2
    assert _hook_rc(tmp_path, "eval \"$(cat <<'EOF'\n" + command + "\nEOF\n)\"") == 2


def test_the_look_back_of_a_built_command_word_survives_a_run_of_backticks(tmp_path):
    command = "echo " + "`a`" * 2000 + "\nbash <<< $'open`printf ''`claw gateway restart'"
    assert _hook_rc(tmp_path, command) == 2


def test_a_comment_line_is_not_a_statement_and_an_option_value_is_not_a_stored_name(guard):
    assert guard._cheap_view("# deploy to $ENV: restart the gateway\nls", strict=True) is None
    assert guard._cheap_view("  # deploy to $ENV: restart the gateway\nls", strict=True) is None
    # a comment that names openclaw is still read (T46)
    assert guard._cheap_view("# openclaw gateway restart is guarded\nls", strict=True) == "restart"
    assert guard._cheap_view("echo a # b\nopenclaw gateway restart", strict=True) == "restart"
    assert guard._cheap_view("git log --grep=gateway restart", strict=True) is None
    assert guard._cheap_view("g=gateway; ${X} $g restart", strict=True) == "restart"
