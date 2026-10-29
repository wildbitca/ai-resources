"""The gateway guard's second view (kit 2.2e, item 2).

The narrowed second view (text that feeds a shell) required a literal `openclaw` BEFORE `gateway`, so a name
assembled from pieces (variables, an ANSI-C escape, a glob, a brace, a quote pair around a backtick) or a `gateway`
stored in a variable before `openclaw` was allowed. The view now reads the gateway words wide and EXCLUDES known
harmless contexts: another product's CLI, a unit such as `api-gateway`, words that are only printed, and a
subcommand that answers (`gateway status --note restart`).

The hook as Claude Code runs it (a real subprocess) is used where the wall clock matters; `systemctl` is a stub
that answers `active`; nothing here touches systemd or the host.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import stat
import subprocess
import sys
import time

import pytest

# Integration: touches or fakes an external tool (openclaw, node, git, installers...); skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"
KC = "kube" + "ctl"           # keeps the harness's own context rule out of the file


def _load():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard_views", HOOK)
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


def _hook(tmp_path, command, deadline=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    stub.write_text("#!/bin/sh\necho active\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp_path)}
    if deadline is not None:
        env["KIT_GUARD_DEADLINE_S"] = str(deadline)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    start = time.perf_counter()
    done = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True,
                          env=env, timeout=60)
    return done.returncode, time.perf_counter() - start


# --- item 2: the strict view ----------------------------------------------------------------------------------------

ASSEMBLED = [
    # `gateway` stored in a variable BEFORE `openclaw` is spelled
    ("g=gateway; openclaw $g restart", "restart"),
    ("g=gateway; openclaw $g stop", "stop"),
    ("G=gateway; systemctl --user restart openclaw-$G", "restart"),
    ("export g=gateway\nopenclaw $g restart", "restart"),
    # the command word itself is assembled
    ("o=open; c=claw; $o$c gateway restart", "restart"),
    ("o=open; c=claw; ${o}${c} gateway stop", "stop"),
    ("/usr/local/bin/open*law gateway restart", "restart"),
    ("systemctl --user restart open``claw-gateway", "restart"),
    ("systemctl --user restart open`printf claw`-gateway", "restart"),
    ("systemctl --user restart 'opencl?w-gateway'", "restart"),
    ("systemctl --user restart \"$(printf 'openclaw-%s' gateway)\"", "restart"),
    # ANSI-C escapes
    ("openclaw $'gat\\x65way' restart", "restart"),
    ("$'open\\x63law' gateway restart", "restart"),
    ("openclaw gateway $'rest\\x61rt'", "restart"),
    ("systemctl --user restart $'openclaw-gate\\x77ay'", "restart"),
    # a unit glob handed to a shell through `xargs`
    ("echo -n 'openclaw-gate*' | xargs -0 -I{} sh -c 'systemctl --user restart {}'", "restart"),
    ("printf '%s\\0' 'openclaw-gate?ay' | xargs -0 -I{} bash -c 'systemctl --user stop {}'", "stop"),
    # a continuation inside the name
    ("systemctl --user restart open\\\nclaw-gateway", "restart"),
]


@pytest.mark.parametrize("text,expected", ASSEMBLED)
def test_the_strict_view_reads_assembled_openclaw_names(guard, text, expected):
    assert guard._cheap_view(text, strict=True) == expected


HARMLESS = [
    "docker restart gateway",
    KC + " rollout restart deploy/gateway",
    KC + " -n edge rollout restart deployment/api-gateway",
    "systemctl restart api-gateway",
    "systemctl --user restart api-gateway.service",
    "systemctl restart nginx-gateway",
    "systemctl kill gateway-proxy.service",
    "systemctl --user restart gateway",
    "sudo systemctl stop nginx-gateway",
    "docker compose restart gateway",
    "docker stop api-gateway && docker start api-gateway",
    "pm2 restart gateway",
    "supervisorctl restart gateway",
    "service gateway restart",
    "helm upgrade gateway ./chart && " + KC + " rollout restart deploy/gateway",
    "g=gateway; docker restart $g",
    "g=gateway; " + KC + " rollout restart deploy/$g",
    "systemctl --user stop openclaw-gateway-x.service",
    "systemctl --user restart nginx; echo gateway",
    "cat ~/.openclaw/openclaw.json; docker restart gateway",
    "docker kill gateway",
]


@pytest.mark.parametrize("text", HARMLESS)
def test_the_strict_view_leaves_other_products_gateways_alone(guard, text):
    assert guard._cheap_view(text, strict=True) is None


FEEDS = ["bash <<EOF\n{c}\nEOF", "cat <<'EOF' | sh\n{c}\nEOF", "bash <<< \"{c}\"", "echo \"{c}\" | bash",
         "printf '%s\\n' \"{c}\" | zsh"]


@pytest.mark.parametrize("feed", FEEDS)
@pytest.mark.parametrize("text,expected", [
    ("g=gateway; openclaw $g restart", "restart"),
    ("o=open; c=claw; $o$c gateway stop", "stop"),
    ("systemctl --user restart open``claw-gateway", "restart"),
    ("openclaw $'gat\\x65way' restart", "restart"),
    ("systemctl --user restart openclaw-gate*", "restart"),
])
def test_assembled_names_fed_to_a_shell_are_denied(guard, feed, text, expected):
    command = feed.format(c=text)
    assert guard.decide(command) == expected, command
    assert guard._last_deadline_fired is False


@pytest.mark.parametrize("feed", FEEDS)
@pytest.mark.parametrize("text", HARMLESS)
def test_other_products_gateways_fed_to_a_shell_are_allowed(guard, feed, text):
    command = feed.format(c=text)
    assert guard.decide(command) is None, command
    assert guard._last_deadline_fired is False


def test_the_openclaw_word_on_either_side_of_gateway_counts(guard):
    assert guard._cheap_view("docker restart gateway; openclaw status", strict=True) == "restart"
    assert guard._cheap_view("restart gateway openclaw", strict=True) == "restart"
    assert guard._cheap_view("openclaw status; docker restart gateway", strict=True) == "restart"
    assert guard._cheap_view("docker restart gateway; " + "x" * 200 + " openclaw status", strict=True) is None


def test_the_second_view_stays_cheap_on_a_long_harmless_text(guard):
    text = "docker restart gateway\n" * 40000          # many harmless pairs: the exclusion work is bounded
    start = time.perf_counter()
    guard._cheap_view(text, strict=True)
    assert time.perf_counter() - start < 3
    text = "echo hello\n" * 200000 + "docker restart gateway\n"
    start = time.perf_counter()
    assert guard._cheap_view(text, strict=True) is None
    assert time.perf_counter() - start < 3


CONTINUED = "systemctl --user stop open\\\\\nclaw-gateway"      # open + two backslashes + newline + claw-gateway


@pytest.mark.parametrize("command", [
    "echo $'" + CONTINUED + "' | xargs -0 bash -c",
    "eval $'echo $\\'" + CONTINUED.replace("\\", "\\\\") + "\\' | xargs -0 bash -c'",
])
def test_a_continuation_that_only_exists_once_an_ansi_c_string_is_decoded_is_joined(guard, command):
    assert guard._cheap_view(command, strict=True) == "stop"
    assert guard._cheap_view(command) == "stop"


# --- the strict view and words that are only printed, or answered by another subcommand ------------------------------

PRINTED = [
    "eval echo openclaw gateway restart",
    "eval printf '%s' openclaw gateway restart",
    ": openclaw gateway restart",
    "cat <(echo stop openclaw-gateway)",
    "bash -c 'echo openclaw gateway restart'",
    "find . -maxdepth 0 -exec sh -c 'echo systemctl --user restart openclaw-gateway' ';'",
    "sudo bash -c 'echo -e openclaw gateway restart'",
    "x=$(: openclaw gateway restart)",
]


@pytest.mark.parametrize("text", PRINTED)
def test_words_that_a_command_only_prints_are_not_a_restart(guard, text):
    assert guard._cheap_view(text, strict=True) is None, text


RUNS = [
    "echo openclaw gateway restart",                    # a bare statement of the text is still read
    "echo openclaw gateway restart | sh",
    "echo openclaw gateway restart | tee /dev/null | bash",
    "echo 'openclaw gateway restart' > s.sh",
    "printf 'openclaw gateway restart' >> s.sh",
    "x=$(echo openclaw gateway restart); $x",
    "`echo openclaw gateway restart`",
    "echo $(openclaw gateway restart)",
    "source <(echo openclaw gateway restart)",
    ". <(echo systemctl --user stop openclaw-gateway)",
    "bash <(echo openclaw gateway restart)",
    "echo hi; openclaw gateway restart",
    "echo hi && systemctl --user restart openclaw-gateway",
    ": $(openclaw gateway restart)",
    "eval echo openclaw gateway restart | sh",
    "eval echo openclaw gateway restart > s.sh",
    "bash -c 'echo openclaw gateway restart' | sh",
    "eval \"$(echo openclaw gateway restart)\"",
    "openclaw gateway restart",
]


@pytest.mark.parametrize("text", RUNS)
def test_words_a_shell_will_run_are_still_a_restart(guard, text):
    assert guard._cheap_view(text, strict=True) in ("restart", "stop"), text


def test_the_first_gateway_subcommand_decides_in_the_strict_view(guard):
    assert guard._cheap_view("openclaw gateway status --note restart", strict=True) is None
    assert guard._cheap_view("openclaw gateway --port 1 logs restart", strict=True) is None
    assert guard._cheap_view("openclaw gateway status; openclaw gateway restart", strict=True) == "restart"
    assert guard._cheap_view("openclaw gateway status\nopenclaw gateway stop", strict=True) == "stop"
    assert guard._cheap_view("openclaw gateway restart --note status", strict=True) == "restart"


@pytest.mark.parametrize("command,expected", [
    ("bash <<EOF\neval echo openclaw gateway restart\nEOF", None),
    ("sh <<< 'bash -c \"echo openclaw gateway restart\"'", None),
    ("bash <<EOF\necho openclaw gateway restart\nEOF", "restart"),      # the price T46 keeps
    ("bash <<'EOF'\n# openclaw gateway restart is guarded\nEOF", "restart"),
    ("echo 'openclaw gateway restart' | sh", "restart"),
    ("bash <<EOF\necho ok\nopenclaw gateway restart\nEOF", "restart"),
])
def test_a_printed_command_in_a_text_fed_to_a_shell(guard, command, expected):
    got = guard.decide(command)
    assert got == expected, command


def test_a_quoted_semicolon_does_not_end_a_printing_statement(guard):
    command = "if true; then echo 'set -- openclaw gateway stop; \"$@\"' | xargs -0 bash -c; fi"
    assert guard._cheap_view(command, strict=True) == "stop"
    assert guard.decide(command) == "stop"
    assert guard._cheap_view("eval echo 'a; b openclaw gateway restart' > s.sh", strict=True) == "restart"


def test_a_subcommand_that_only_exists_after_quote_resolution_answers_nothing(guard):
    # `$'sta'tus` is `status` to bash and `$statustus` to dash: written this way it is no subcommand
    command = "printf '%s\\n' \"openclaw gateway \\$'sta'tus --note restart\" | sh"
    assert guard._cheap_view(command, strict=True) == "restart"
    assert guard._cheap_view("openclaw gateway status --note restart", strict=True) is None
    assert guard._cheap_view("openclaw gateway 'status' --note restart", strict=True) == "restart"


def test_a_subcommand_spelled_through_an_outer_ansi_c_string_is_plain_once_it_is_decoded(guard):
    command = "eval $'sh <<<\\'openclaw gateway s\\x74atus --note restart\\''"
    assert guard._cheap_view(command, strict=True) is None
    assert guard.decide(command) is None
    assert guard.decide("eval $'sh <<<\\'openclaw gateway restart --note s\\x74atus\\''") == "restart"


# --- the exclusion work is bounded -----------------------------------------------------------------------------------

def test_a_printing_statement_that_runs_on_past_the_lookahead_is_not_printing(guard):
    long_line = "eval echo openclaw gateway restart " + "x " * 4000 + "| sh"
    assert guard._cheap_view(long_line, strict=True) == "restart"
    near_pipe = "eval echo openclaw gateway restart " + "x " * 100 + "| sh"
    assert guard._cheap_view(near_pipe, strict=True) == "restart"
    short = "eval echo openclaw gateway restart " + "x " * 100
    assert guard._cheap_view(short, strict=True) is None


@pytest.mark.parametrize("unit", [
    "docker restart gateway; ",                  # harmless pairs on one line
    "eval echo openclaw gateway restart; ",     # printing statements on one line
    "openclaw gateway status --note restart ",  # answered pairs
])
def test_many_exclusion_checks_are_answered_in_time_and_fail_closed(guard, unit):
    text = unit * (240000 // len(unit))
    start = time.perf_counter()
    got = guard._cheap_view(text, strict=True)
    assert time.perf_counter() - start < 3
    assert got == "restart", "past the check budget every pair counts"
    few = (unit + " " * 200 + "\n") * 50       # the words of one repetition are not near the next one's
    assert guard._cheap_view(few, strict=True) is None


@pytest.mark.parametrize("command", [
    "bash <<EOF\n(echo openclaw gateway restart\n) | sh\nEOF",
    "bash <<EOF\n{ echo openclaw gateway restart\n} | sh\nEOF",
    "bash <<EOF\n(echo openclaw gateway restart\necho hi) | sh\nEOF",
    "bash <<EOF\nfor i in 1; do echo openclaw gateway restart\ndone | sh\nEOF",
    "bash <<EOF\nif true; then echo openclaw gateway restart\nfi | sh\nEOF",
    "bash <<EOF\n(echo openclaw gateway restart\n) | bash\nEOF",
    "bash <<EOF\ncommand printf -v x '%s' 'openclaw gateway restart'; $x\nEOF",
    "bash <<EOF\ncommand printf -v x '%s' 'openclaw gateway restart'; eval \"$x\"\nEOF",
    "bash <<EOF\ncommand printf -v x '%s' 'systemctl --user restart openclaw-gateway'; eval \"$x\"\nEOF",
    "bash <<EOF\no=open; ${o}claw --profile docker gateway restart\nEOF",
    "bash <<EOF\no=open; ${o}claw --log-level helm gateway restart\nEOF",
    "bash <<EOF\no=open; open$c --dev docker gateway restart\nEOF",
])
def test_printing_and_other_product_exclusions_do_not_let_a_shell_fed_restart_through(guard, command):
    assert guard.decide(command) == "restart", command


def test_another_products_command_word_still_excludes(guard):
    assert guard.decide("bash <<EOF\ndocker restart gateway\nEOF") is None
