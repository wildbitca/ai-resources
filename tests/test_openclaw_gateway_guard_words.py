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

# Wall-clock budgets: a loaded CPU would fail them, so they run in the serial pass (scripts/run-tests.sh).
pytestmark = pytest.mark.timing

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
    assert elapsed < 10, f"{n}: {elapsed:.1f}s"
    rc, elapsed = _hook(tmp_path, _adjacent(n, f"`{SC}`"))
    assert rc == 2, n


@pytest.mark.parametrize("n", [2016, 5000])
def test_the_same_shape_without_the_verb_is_allowed_in_time(tmp_path, n):
    rc, elapsed = _hook(tmp_path, _adjacent(n, "`openclaw gateway status`"))
    assert rc == 0, n
    assert elapsed < 10, f"{n}: {elapsed:.1f}s"
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
        assert elapsed < 10


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
    assert elapsed < 10


def test_the_budget_fallback_allows_a_remainder_without_the_verb(tmp_path, guard):
    for tail in ("`openclaw gateway status`", "`ls`", "$(date)"):
        command = "echo " + "`$(a)`" * 5000 + " " + tail
        assert guard.decide(command) is None, tail
        assert guard._last_deadline_fired is False
    rc, elapsed = _hook(tmp_path, "echo " + "`$(a)`" * 5000 + " `openclaw gateway status`")
    assert (rc, elapsed < 10) == (0, True)


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


# --- 2.2d, 6a: the deadline fails closed, and the slow class is read in time -----------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("openclaw gateway --token " + "x" * 400 + " restart", "restart"),
    ("openclaw " + "x" * 400 + " gateway", "restart"),
    ("res\\\ntart; openclaw status", "restart"),
    ("/usr/local/bin/openclaw " + "x " * 300 + "stop", "stop"),
    ("openclaw " + "y " * 300 + "doctor", "doctor"),
    ("openclaw " + "y " * 300 + "--fix", "doctor"),
    ("systemctl --user " + "x " * 200 + "stop openclaw-gateway", "restart"),
    ("openclaw status", None),
    ("openclaw status --json | jq .", None),
    ("docker " + "x " * 300 + "restart gateway", None),
    ("cat ~/.openclaw/openclaw.json; echo restart gateway", None),
    ("cd ~/src/openclaw/x && npm run stop", None),
    ("ls -la", None),
])
def test_the_fail_closed_view_names_openclaw_and_a_guarded_word_wherever_they_sit(guard, text, expected):
    assert guard._fail_closed_view(text) == expected


def _spin(guard):
    def spin(*_a, **_k):
        while True:
            guard._check_deadline()
    return spin


def test_a_fired_deadline_denies_openclaw_and_a_guarded_word_beyond_the_window(guard, monkeypatch):
    monkeypatch.setenv("KIT_GUARD_DEADLINE_S", "0.05")
    monkeypatch.setattr(guard, "dangerous", _spin(guard))
    far = "openclaw gateway --token " + "x" * 400 + " restart"
    assert guard._cheap_view(far) is None, "the 160-character window does not see it"
    assert guard.decide(far) == "restart"
    assert guard._last_deadline_fired is True
    assert guard.decide("docker restart " + "x " * 300 + "gateway") is None      # far apart, no OpenClaw word
    assert guard.decide("docker restart gateway") == "restart", "the wide word rule of the deadline fallback"
    assert guard.decide("ls -la") is None


def test_an_internal_error_keeps_the_cheap_view_only(guard, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "dangerous", boom)
    assert guard.decide("openclaw gateway --token " + "x" * 400 + " restart") is None
    assert guard._last_deadline_fired is False
    assert guard.decide("openclaw gateway restart") == "restart"


@pytest.mark.parametrize("deadline", ["0.01", "0.05"])
def test_the_real_hook_fails_closed_when_its_deadline_fires(tmp_path, deadline):
    pad = "echo " + "$(a)" * 30000 + "\n"
    far = "openclaw gateway --token " + "x" * 400 + " restart"
    rc, elapsed = _hook(tmp_path, pad + far, deadline=deadline)
    assert (rc, elapsed < 6) == (2, True)
    rc, elapsed = _hook(tmp_path, pad + "docker restart " + "x " * 300 + "gateway", deadline=deadline)
    assert (rc, elapsed < 6) == (0, True)
    rc, elapsed = _hook(tmp_path, pad + "ls -la", deadline=deadline)
    assert (rc, elapsed < 6) == (0, True)


def _open_quote_class(line, cap=255_000):
    """An open double quote, then thousands of lines that hold an expansion: the shape the 2.2c auditor found
    at 4.1 s (the deadline fired and the 160-character window decided)."""
    head = 'echo "'
    return head + "\n".join([line] * ((cap - len(head)) // (len(line) + 1)))


_OPEN_QUOTE_LINES = ["${A}", "x ${A} y", "some ${VAR:-default} text ${OTHER} here", "x $(a) y", "x `a` y"]


@pytest.mark.parametrize("line", _OPEN_QUOTE_LINES)
def test_an_open_quote_over_many_expansion_lines_is_read_in_time_and_keeps_its_verdict(tmp_path, guard, line):
    benign = _open_quote_class(line)
    start = time.perf_counter()
    assert guard.decide(benign) is None
    parsed_in = time.perf_counter() - start
    assert guard._last_deadline_fired is False, "the deadline must not fire on realistic input"
    assert parsed_in < 10, f"{line!r}: {parsed_in:.1f}s for {len(benign)} bytes"
    rc, elapsed = _hook(tmp_path, benign)
    assert (rc, elapsed < 10) == (0, True), f"{line!r}: {elapsed:.1f}s"
    rc, elapsed = _hook(tmp_path, benign + "\n" + GW)
    assert (rc, elapsed < 10) == (2, True), f"{line!r} + verb: {elapsed:.1f}s"
    rc, _elapsed = _hook(tmp_path, benign + "\n" + SC)
    assert rc == 2


def test_the_physical_lines_of_a_failed_join_are_read_once(guard, monkeypatch):
    calls = []
    real = guard._line_statements
    monkeypatch.setattr(guard, "_line_statements", lambda lines: calls.append(len(lines)) or real(lines))
    assert guard.decide('echo "\n' + "x ${A} y\n" * 200) is None
    assert len(calls) == 1, "an unbalanced join was read line by line twice"
    calls.clear()
    # a balanced join that holds a nested expansion is still read the second way, once
    assert guard.decide('echo "a ${A}\nb $(echo "c")\nd"') is None
    assert len(calls) == 1


def test_the_second_reading_of_the_physical_lines_is_charged_and_capped(guard):
    guard._work = 0
    out = guard._line_statements(["echo a"] * 10)
    assert len(out) == 10 and guard._work >= 10 * len("echo a")
    guard._work = guard._WORK_BUDGET
    assert guard._line_statements(["echo a"] * 50) == [], "the budget is spent: no line is read"
    assert guard._work > guard._WORK_BUDGET


def test_a_spent_budget_in_the_line_reading_falls_back_to_the_words_of_the_whole_line(guard):
    # an unbalanced join is read line by line; when that reading spends the budget the lines left are not read, and
    # the whole logical line is read for words instead: a verb on the last line is still found
    command = 'echo "\n' + "x ${A} y\n" * 4000 + GW
    guard._work = guard._WORK_BUDGET - 100_000        # a nested call: the budget of the whole command is shared
    guard._memo = None
    assert guard.shape(command, 1) == "restart"
    guard._work = 0
    assert guard.shape(command, 1) == "restart", "and with the budget intact the lines are simply read"
    guard._work = guard._WORK_BUDGET - 100_000
    assert guard.shape('echo "\n' + "x ${A} y\n" * 4000 + "ls", 1) is None


def test_a_text_without_a_substitution_is_not_scanned_for_one(guard, monkeypatch):
    scans = []
    real = guard._scan
    monkeypatch.setattr(guard, "_scan", lambda *a, **k: scans.append(1) or real(*a, **k))
    assert guard._mask_substitutions("echo a b; ls -la") == "echo a b; ls -la"
    assert scans == [], "a substitution opens with $( ` <( or >( : without one there is nothing to scan"
    assert guard._mask_substitutions("cat <(ls) >(wc)") == "cat _ _"
    assert scans


# --- 2.2e item 1: a prefix that spends the work budget must not turn a denied form into an allowed one -------------

def _padded(tail: str, n: int = 2200) -> str:
    """About 8 KB of adjacent harmless substitutions: the work budget is spent in well under a second, the
    deadline never fires, and the rest of the command is read by the fallback."""
    return "echo " + "$(a)" * n + "\n" + tail


@pytest.mark.parametrize("tail", [
    "openclaw gateway \\\nrestart",                      # a line continuation inside the statement
    "openclaw \\\ngateway restart",
    "systemctl --user \\\nrestart openclaw-gateway",
    "openclaw --profile `echo p` gateway restart",       # an inline substitution in an option slot
    "openclaw --profile $(echo p) gateway restart",
    "openclaw doctor \\\n--fix",
])
def test_a_budget_spending_prefix_does_not_hide_a_guarded_command(guard, tmp_path, tail):
    cmd = _padded(tail)
    assert guard.shape(cmd), "the fallback after the budget is spent must still deny"
    rc, secs = _hook(tmp_path, cmd)
    assert rc == 2 and secs < 10


def test_a_budget_spending_prefix_with_a_harmless_tail_is_allowed(guard, tmp_path):
    rc, secs = _hook(tmp_path, _padded("openclaw gateway status \\\n--json"))
    assert rc == 0 and secs < 10


def test_a_line_continuation_is_undone_in_the_word_fallback(guard):
    assert guard._text_shape("openclaw gateway \\\nrestart") == "restart"


# --- 2.2e: the placeholder of an inline substitution is an unknown word, not a plain one (found by the differential) --

@pytest.mark.parametrize("text,expected", [
    ("openclaw gateway `echo restart`", "restart"),
    ("openclaw gateway `echo stop`", "restart"),            # an unknown verb fails closed as "restart"
    ("openclaw --profile `echo p` gateway restart", "restart"),
    ("openclaw gateway --port `echo 1` stop", "stop"),
    ("echo $(openclaw gateway `echo restart`)", "restart"),
    ("openclaw gateway status `date`", None),
    ("openclaw gateway status", None),
    ("echo `date` $(uname)", None),
])
def test_an_inline_substitution_in_the_verb_slot_is_an_unknown_verb(guard, text, expected):
    assert guard._text_shape(text) == expected


@pytest.mark.parametrize("word", ["restart", "stop"])
@pytest.mark.parametrize("depth", [41, 44, 48])
def test_a_substituted_verb_past_the_nesting_limit_is_denied(guard, word, depth):
    command = "echo $(" * depth + f"openclaw gateway `echo {word}`" + ")" * depth
    assert guard.decide(command) in ("restart", "stop")
    assert guard._last_deadline_fired is False


def test_a_substituted_verb_after_a_budget_spending_prefix_is_denied(tmp_path):
    command = "echo " + "$(a)" * 2200 + '\na="`echo \\"x\\"`"; openclaw gateway `echo restart`'
    rc, secs = _hook(tmp_path, command)
    assert rc == 2 and secs < 10
