"""The gateway guard's word fallbacks (kit 2.2d): backtick-wrapped verbs, the strict second view, stale verdicts.

The word fallback decides where the parser cannot (past the nesting limit, past the work budget, on text that
does not tokenise) and the deadline view decides when the clock runs out. Three things were wrong in 2.2c:

- a verb wrapped in backticks was glued to the backtick (`` `openclaw ``), so after enough substitutions to spend
  the work budget, or past the nesting limit, the fallback did not see it and the hook allowed the command;
- the second view (text that feeds a shell) read the bare word `gateway`, so `docker restart gateway` in a heredoc
  to a shell was denied;
- the verdict of a refused expansion lives in a module global: its reset and its length bound were not pinned.

Real subprocesses (the hook as Claude Code runs it) are used for the wall-clock cases; `systemctl` is a stub that
answers `active`; nothing here touches systemd or the host.
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

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"
GW = "openclaw gateway restart"
SC = "systemctl --user stop openclaw-gateway"


def _load():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard_words", HOOK)
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


# --- 1a: the word view splits at a backtick ------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("`openclaw gateway restart`", "restart"),
    ("``openclaw gateway restart``", "restart"),
    ("`a``b`" + "`openclaw gateway restart`", "restart"),
    ("echo x`openclaw gateway restart`y", "restart"),
    ("`openclaw --profile p gateway stop`", "stop"),
    ("`systemctl --user restart openclaw-gateway.service`", "restart"),
    ("`systemctl --user stop openclaw-gateway`", "stop"),
    ("echo \"`openclaw gateway restart`\"", "restart"),
    ("`openclaw doctor --fix`", "doctor"),
    ("`openclaw gateway status`", None),
    ("`systemctl --user status openclaw-gateway`", None),
    ("`a``b`", None),
    ("echo `openclaw gateway status` restart", None),
])
def test_the_word_fallback_splits_words_at_a_backtick(guard, text, expected):
    assert guard._text_shape(text) == expected


def test_a_data_command_is_not_a_hiding_place_for_a_backtick_substitution(guard):
    # `echo` prints its arguments, but bash runs the substitution first: the fallback must not skip the word
    assert guard._text_shape("echo `openclaw gateway restart`") == "restart"
    assert guard._text_shape("echo openclaw gateway restart") is None


# --- 1b/1c: the budget and the nesting limit, through the real hook at the default deadline -------------------------

def _adjacent(n, tail):
    return "echo " + "`a`" * n + " " + tail


@pytest.mark.parametrize("n", [2015, 2016, 3000, 5000, 12000])
def test_a_backtick_wrapped_verb_after_many_substitutions_is_denied(tmp_path, n):
    rc, elapsed = _hook(tmp_path, _adjacent(n, f"`{GW}`"))
    assert rc == 2, n
    assert elapsed < 3, f"{n}: {elapsed:.1f}s"
    rc, elapsed = _hook(tmp_path, _adjacent(n, f"`{SC}`"))
    assert rc == 2, n


@pytest.mark.parametrize("n", [2016, 5000])
def test_the_same_shape_without_the_verb_is_allowed_in_time(tmp_path, n):
    rc, elapsed = _hook(tmp_path, _adjacent(n, "`openclaw gateway status`"))
    assert rc == 0, n
    assert elapsed < 3, f"{n}: {elapsed:.1f}s"
    rc, elapsed = _hook(tmp_path, _adjacent(n, "`ls -la`"))
    assert rc == 0, n


@pytest.mark.parametrize("depth", [39, 40, 41, 42, 60])
def test_a_backtick_wrapped_verb_past_the_nesting_limit_is_denied(tmp_path, depth):
    deny = "echo " + "$(echo " * depth + f"`{GW}`" + ")" * depth
    allow = "echo " + "$(echo " * depth + "`openclaw gateway status`" + ")" * depth
    deny_dq = "echo " + '"$(echo ' * depth + f"`{GW}`" + ')"' * depth
    for command, want in ((deny, 2), (allow, 0), (deny_dq, 2)):
        rc, elapsed = _hook(tmp_path, command)
        assert rc == want, (depth, command[:60])
        assert elapsed < 3


@pytest.mark.parametrize("command", [
    # the verb is in the REMAINDER of the line, after enough substitutions to spend the work budget
    "echo " + "`$(a)`" * 5000 + f" `{GW}`",
    'echo "' + "`$(a)`" * 5000 + f' `{GW}`"',
    "echo " + "`$(a)`" * 5000 + f" $({GW})",
    "echo " + "`$(a)`" * 5000 + f" `{SC}`",
    "echo " + "`$(a)`" * 5000 + f"; {GW}",
    "echo " + "$(a)" * 30000 + f" `{GW}`",
])
def test_the_budget_fallback_reads_the_remainder_of_the_line(tmp_path, guard, command):
    assert guard.decide(command) in ("restart", "stop")
    assert guard._last_deadline_fired is False
    rc, elapsed = _hook(tmp_path, command)
    assert rc == 2
    assert elapsed < 3


def test_the_budget_fallback_allows_a_remainder_without_the_verb(tmp_path, guard):
    for tail in ("`openclaw gateway status`", "`ls`", "$(date)"):
        command = "echo " + "`$(a)`" * 5000 + " " + tail
        assert guard.decide(command) is None, tail
        assert guard._last_deadline_fired is False
    rc, elapsed = _hook(tmp_path, "echo " + "`$(a)`" * 5000 + " `openclaw gateway status`")
    assert (rc, elapsed < 3) == (0, True)


# --- 1d: the second view needs the OpenClaw gateway words ----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("openclaw gateway restart", "restart"),
    ("openclaw --profile p gateway --port 1 stop", "stop"),
    ("/usr/local/bin/openclaw gateway restart", "restart"),
    ("openclaw.mjs gateway restart", "restart"),
    ("systemctl --user restart openclaw-gateway.service", "restart"),
    ("systemctl --user kill openclaw-gateway", "stop"),
    ("restart openclaw-gateway", "restart"),
    ("systemctl --user restart openclaw-{gateway,x}.service", "restart"),
    ("echo $'systemctl --user stop openclaw-gateway\\n\\tEOF'", "stop"),
    ("openclaw doctor --fix", "doctor"),
    ("systemctl --user stop openclaw-gateway-x.service", None),
    ("kubectl rollout restart deploy/gateway", None),
    ("docker restart gateway", None),
    ("systemctl restart api-gateway", None),
    ("systemctl --user restart nginx; echo gateway", None),
    ("cat ~/.openclaw/openclaw.json; docker restart gateway", None),
    ("cd ~/src/openclaw/x && docker restart gateway", None),
    ("openclaw status; " + "x" * 200 + " docker restart gateway", None),
    ("openclaw gateway status", None),
])
def test_the_strict_view_needs_the_openclaw_gateway_words(guard, text, expected):
    assert guard._cheap_view(text, strict=True) == expected


@pytest.mark.parametrize("command", [
    "bash <<EOF\ndocker restart gateway\nEOF",
    "bash <<'EOF'\nkubectl rollout restart deploy/gateway\nEOF",
    "echo 'systemctl restart api-gateway' | sh",
    "sh <<< 'docker restart gateway'",
    "cat <<EOF | bash\nkubectl rollout restart deploy/gateway\nEOF",
    "eval \"$(cat <<'EOF'\ndocker stop gateway\nEOF\n)\"",
    "bash <<EOF\nsystemctl --user stop openclaw-gateway-x.service\nEOF",
])
def test_a_shell_fed_text_that_restarts_another_gateway_is_allowed(guard, command):
    assert guard.decide(command) is None, command
    assert guard._last_deadline_fired is False


@pytest.mark.parametrize("command", [
    "bash <<EOF\nopenclaw --profile p gateway restart\nEOF",
    "echo 'systemctl --user stop openclaw-gateway' | sh",
    "sh <<< 'openclaw gateway stop'",
    "cat <<EOF | bash\nsudo systemctl restart openclaw-gateway.service\nEOF",
    "bash <<EOF\ndocker restart gateway\nopenclaw gateway restart\nEOF",
])
def test_a_shell_fed_text_that_restarts_the_openclaw_gateway_stays_denied(guard, command):
    assert guard.decide(command) in ("restart", "stop"), command


def test_the_deadline_fallback_keeps_the_wide_word_rule(guard, monkeypatch):
    # fail toward the words: when the deadline fires, any gateway next to a verb is denied (documented)
    def spin(*_a, **_k):
        while True:
            guard._check_deadline()

    monkeypatch.setenv("KIT_GUARD_DEADLINE_S", "0.1")
    monkeypatch.setattr(guard, "dangerous", spin)
    assert guard.decide("docker restart gateway") == "restart"
    assert guard._last_deadline_fired is True
    assert guard._cheap_view("docker restart gateway") == "restart"


# --- 1e: the verdict of a refused expansion does not leak ----------------------------------------------------------

_REFUSED = 'V="openclaw gateway restart"; eval "' + "$V;" * 50000 + '"'


def test_a_refused_expansion_verdict_is_reset_at_depth_zero(guard):
    assert guard.decide(_REFUSED) == "restart"
    assert guard._refused_found == "restart"
    # the next command is a different one: nothing of the last verdict may reach it
    assert guard.decide("ls -la") is None
    assert guard._refused_found is None
    guard._refused_found = "restart"                  # a stale value, as after an interrupted call
    guard._memo = None
    assert guard.shape("ls -la") is None
    assert guard._refused_found is None


def test_a_refused_expansion_view_is_not_read_above_the_size_cap(guard):
    guard._refused_found = None
    big = "x" * (guard._MAX_COMMAND_CHARS + 1) + " openclaw gateway restart"
    guard._note_refused([big])
    assert guard._refused_found is None
    guard._note_refused(["openclaw gateway restart"])
    assert guard._refused_found == "restart"
    guard._note_refused(["openclaw doctor --fix"])    # the first verdict stays
    assert guard._refused_found == "restart"


def test_the_memo_is_keyed_on_the_command_and_cleared_by_decide(guard):
    assert guard.decide("openclaw gateway restart") == "restart"
    assert guard._memo is None
    assert guard.decide("openclaw gateway status") is None


# --- 2.2d, 4: pins for the mutants the 2.2c verification left alive -------------------------------------------------

def test_a_backtick_substitution_unescapes_only_a_backslash_a_dollar_and_the_backtick(guard):
    assert guard._unescape_backticks("echo \\`x\\` \\$y \\\\ \\n \\\"") == "echo `x` $y \\ \\n \\\""
    # the nested form: the outer body holds an escaped inner substitution that runs the verb
    assert guard.decide("echo `echo \\`" + GW + "\\``") == "restart"
    assert guard.decide("echo `echo \\`openclaw gateway status\\``") is None


@pytest.mark.parametrize("payload", [
    {"tool_name": "Read", "tool_input": {"command": GW}},
    {"tool_name": "bash", "tool_input": {"command": GW}},
    {"tool_input": {"command": GW}},
    ["Bash", GW],
])
def test_main_looks_only_at_the_bash_tool(guard, monkeypatch, payload):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0


def test_main_denies_the_bash_tool_with_the_same_command(guard, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": GW}})))
    assert guard.main() == 2


def test_a_markdown_fence_before_a_shell_word_is_not_a_shell_that_is_fed(guard):
    # the third backtick of a fence is preceded by a backtick: `bash` after it is prose, not a command word
    command = "echo ```bash " + GW + " <<x"
    assert guard._feeds_a_shell_text(command) is False
    assert guard.decide(command) is None
    assert guard._feeds_a_shell_text("echo `bash " + GW + " <<x") is True      # one backtick is a quote: kept


def test_a_literal_c_string_is_not_a_consumer_up_to_its_bound_only(guard):
    # `echo TEXT | bash -c 'SCRIPT'`: the shell runs SCRIPT, not the pipe; a literal longer than 2000 characters
    # is not recognised as a literal any more, so the pipe is read as a consumer (the conservative side)
    short = "echo '" + GW + "' | bash -c '" + "x" * 1900 + "'"
    long = "echo '" + GW + "' | bash -c '" + "x" * 2100 + "'"
    assert guard._feeds_a_shell_text(short) is False
    assert guard._feeds_a_shell_text(long) is True
    assert guard.decide(short) is None


def test_the_word_fallback_is_charged_against_the_work_budget(guard):
    guard._work = 0
    guard._text_shape("x " * 5000)
    assert guard._work >= 10000


def test_the_cheap_view_reads_the_head_and_the_tail_above_its_cap(guard, monkeypatch):
    monkeypatch.setattr(guard, "_CHEAP_CAP", 1000)
    middle = "a" * 2000 + " " + GW + " " + "b" * 2000
    head = GW + " " + "a" * 3000
    tail = "a" * 3000 + " " + GW
    assert guard._cheap_view(middle) is None
    assert guard._cheap_view(head) == "restart"
    assert guard._cheap_view(tail) == "restart"
    assert guard._cheap_view(GW + " " + "x" * 900) == "restart"      # under the cap nothing is cut
