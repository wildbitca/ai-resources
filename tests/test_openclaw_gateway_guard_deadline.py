"""The gateway guard's wall-clock deadline and its cheap word view (kit 2.2c).

The hook timeout FAILS OPEN, so the guard has to answer before it: the precise analysis runs under a deadline
and, when the deadline fires or the analysis fails, a linear word view decides.
Real subprocesses (the hook as Claude Code runs it) are used for the wall-clock cases; nothing here touches
systemd: `systemctl` is a stub that answers `active`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "openclaw_gateway_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("openclaw_gateway_guard_deadline", HOOK)
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


# --- the cheap view -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("openclaw gateway restart", "restart"),
    ("openclaw gateway --port 1 stop", "stop"),
    ("systemctl --user restart openclaw-gateway.service", "restart"),
    ("systemctl --user try-restart openclaw-gateway", "restart"),
    ("systemctl --user kill openclaw-gateway", "stop"),
    ("OPENCLAW GATEWAY RESTART", "restart"),
    ("openclaw gat''eway re''start", "restart"),
    ("openclaw doctor --yes --fix", "doctor"),
    ("openclaw --fix doctor", "doctor"),
    ("systemctl --user status openclaw-gateway", None),
    ("openclaw gateway status", None),
    ("echo restart; echo done", None),
    ("restart the network", None),
    ("openclaw doctor", None),
    ("git stash --fix-whitespace", None),
    ("gateways are stopped" + " " * 400 + "restart", None),
])
def test_the_cheap_view_reads_words_in_one_window(guard, text, expected):
    assert guard._cheap_view(text) == expected


def test_the_cheap_view_window_is_bounded_both_ways(guard):
    near = "gateway " + "x" * 100 + " restart"
    far = "gateway " + "x" * (guard._CHEAP_WINDOW + 50) + " restart"
    assert guard._cheap_view(near) == "restart"
    assert guard._cheap_view(far) is None
    assert guard._cheap_view("restart " + "x" * 100 + " gateway") == "restart"


def test_the_cheap_view_is_one_pass_over_a_huge_text(guard):
    text = "gateway " * 400_000 + "x " * 400_000
    start = time.perf_counter()
    assert guard._cheap_view(text) is None
    assert time.perf_counter() - start < 3
    assert guard._cheap_view(text + " gateway restart") == "restart"


# --- the deadline -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, 4.0), ("", 4.0), ("junk", 4.0), ("nan", 4.0), ("2", 2.0), ("0.25", 0.25),
    ("100", 8.0), ("inf", 8.0), ("0", 0.01), ("-3", 0.01),
])
def test_the_deadline_defaults_to_four_seconds_and_never_exceeds_eight(guard, monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("KIT_GUARD_DEADLINE_S", raising=False)
    else:
        monkeypatch.setenv("KIT_GUARD_DEADLINE_S", raw)
    assert guard._deadline_seconds() == expected


def _spin(*_a, **_k):
    while True:
        pass


def test_a_deadline_that_fires_is_decided_by_the_cheap_view(guard, monkeypatch):
    monkeypatch.setenv("KIT_GUARD_DEADLINE_S", "0.2")
    monkeypatch.setattr(guard, "shape", _spin)
    monkeypatch.setattr(guard, "dangerous", lambda *_a: _spin())
    start = time.perf_counter()
    assert guard.decide("ls; openclaw gateway restart") == "restart"
    assert guard._last_deadline_fired is True
    assert guard.decide("ls -la /tmp") is None
    assert guard._last_deadline_fired is True
    assert time.perf_counter() - start < 3


def test_the_timer_is_disarmed_and_the_handler_restored(guard):
    import signal
    before = signal.getsignal(signal.SIGALRM)
    assert guard.decide("ls") is None
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert guard._deadline_at is None
    assert guard._last_deadline_fired is False


def test_the_clock_check_works_where_signals_do_not(guard, monkeypatch):
    class NoTimers:
        SIGALRM = 14
        ITIMER_REAL = 0

    monkeypatch.setattr(guard, "signal", NoTimers())
    monkeypatch.setenv("KIT_GUARD_DEADLINE_S", "0.2")

    def loop(*_a, **_k):
        while True:
            guard._check_deadline()

    monkeypatch.setattr(guard, "dangerous", loop)
    assert guard.decide("openclaw gateway stop") == "stop"
    assert guard._last_deadline_fired is True


def test_an_internal_error_is_decided_by_the_cheap_view_too(guard, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "dangerous", boom)
    assert guard.decide("openclaw gateway restart") == "restart"
    assert guard.decide("openclaw doctor --fix") == "doctor"
    assert guard.decide("ls -la") is None
    assert guard._last_deadline_fired is False


def test_the_deadline_exception_is_not_swallowed_by_an_except_exception(guard):
    assert not issubclass(guard._DeadlineExceeded, Exception)


# --- the cost of one call -----------------------------------------------------------------------------------------

def test_a_substitution_is_masked_with_one_character(guard):
    assert guard._mask_substitutions("a$(b)c`d`e") == "a_c_e"


def test_what_printf_and_a_variable_expansion_produce_is_charged_before_it_is_built(guard):
    guard._work = 0
    huge = "a" * 100_000
    words = ["$V" * 5000]
    assert guard._expand_known(words, {"V": huge}) == words          # 500 MB would be built: not built
    assert guard._work > guard._WORK_BUDGET
    guard._work = 0
    assert guard._printf_text(["%s%s"] + ["x" * 5000] * 200_000) == ""
    guard._work = 0
    assert guard._printf_text(["%s-%s", "a", "b", "c"]) == "a-bc-"
    assert guard._work < 100


def test_every_shape_call_is_charged_a_fixed_cost(guard):
    guard._work = 0
    guard._memo = None
    guard.shape("x=$(a)")
    assert guard._work >= 2 * guard._SHAPE_COST


# --- real subprocesses: the wall clock ----------------------------------------------------------------------------

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


def _many_cases() -> str:
    return "".join(f"case $x in a) echo $(echo {i});; esac\n" for i in range(4200))


_COST_CLASSES = {
    # the four classes measured in 2.2b
    "252KB-openclaw-words-in-a-9-deep-nest": lambda: "$(echo " * 9 + "openclaw " * 28000 + ")" * 9,
    "6000-heredocs-in-a-group": lambda: 'echo "' + "".join(f"$(cat<<D{i}\nD{i}\n)\n" for i in range(6000)) + '"',
    "28000-openclaw-doctor-words": lambda: "'x\n" + "openclaw doctor " * 14000,
    "1MB-multi-line-quote": lambda: 'echo "$(echo x)\n' + ("a line with 'quotes' and `ticks`\n" * 32_000),
    # the shapes the 2.2b verification found above ten seconds
    "60000-adjacent-substitutions": lambda: "echo " + "$(a)" * 60000,
    "32000-adjacent-substitutions": lambda: "echo " + "$(a)" * 32000,
    "backticks-in-double-quotes-at-the-cap": lambda: 'echo "' + "`a`" * 87000 + '"',
    "one-substitution-per-line-250KB": lambda: "\n".join("x=$(a)" for _ in range(41000)),
    "printf-format-reuse-into-a-shell": lambda: "printf '%s' " + "a " * 60000 + "| sh",
    "assigned-literal-evaluated-many-times": lambda: "V=" + "a" * 2000 + '; eval "' + "$V" * 100 + '"',
    "case-per-line": _many_cases,
}


@pytest.mark.parametrize("name", sorted(_COST_CLASSES))
def test_the_cost_classes_answer_under_six_seconds_and_keep_the_verdict(tmp_path, name):
    benign = _COST_CLASSES[name]()
    rc, elapsed = _hook(tmp_path, benign)
    assert rc == 0, name
    assert elapsed < 6, f"{name}: {elapsed:.1f}s for {len(benign)} bytes"
    guarded = benign + "\nopenclaw gateway restart" if len(benign) < 262000 else benign[:200000] + \
        "\nopenclaw gateway restart"
    rc, elapsed = _hook(tmp_path, guarded)
    assert rc == 2, name
    assert elapsed < 6, f"{name} (guarded): {elapsed:.1f}s"


def test_a_deadline_that_fires_in_the_real_hook_denies_the_gateway_words_and_allows_the_rest(tmp_path):
    big = _many_cases()
    rc, elapsed = _hook(tmp_path, big, deadline=0.05)
    assert (rc, elapsed < 6) == (0, True)
    rc, elapsed = _hook(tmp_path, big + "systemctl --user restart openclaw-gateway.service\n", deadline=0.05)
    assert (rc, elapsed < 6) == (2, True)
    rc, elapsed = _hook(tmp_path, big + "openclaw doctor --fix\n", deadline=0.05)
    assert (rc, elapsed < 6) == (2, True)
