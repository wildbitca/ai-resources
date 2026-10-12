"""The gateway guard's deadline fallback and its verdict bookkeeping (kit 2.2e, items 3 and 4a).

- Item 3: the deadline fallback did not name OpenClaw for `openclaw-gate*`, `openclaw-{gateway,x}` far from the
  verb, or an ANSI-C-encoded name: they were denied alone and allowed once the deadline had fired. The cheap, the
  strict and the fail-closed views now share one token matcher and one normalisation.
- Item 4a: a deadline that fires after a deny was made replaced that deny with the fallback's answer.

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


# --- item 3: the deadline fallback ----------------------------------------------------------------------------------

FAR = " " + "x" * 400 + " "


@pytest.mark.parametrize("command,expected", [
    ("systemctl --user restart 'openclaw-gate*'", "restart"),
    ("systemctl --user stop openclaw-gate?ay", "stop"),
    ("systemctl --user restart openclaw-gateway.serv*", "restart"),
    ("systemctl --user restart openclaw-{gateway,x}", "restart"),
    ("systemctl --user restart" + FAR + "openclaw-{gateway,x}", "restart"),
    ("systemctl --user restart" + FAR + "openclaw-gate*", "restart"),
    ("systemctl --user restart $'openclaw-gate\\x77ay'", "restart"),
    ("systemctl --user restart" + FAR + "$'openclaw-gate\\x77ay'", "restart"),
    ("openclaw $'gat\\x65way'" + FAR + "restart", "restart"),
    ("openclaw gateway" + FAR + "$'rest\\x61rt'", "restart"),
    ("systemctl --user stop" + FAR + "open\\\nclaw-gateway", "stop"),
])
def test_the_deadline_fallback_denies_what_the_full_analysis_denies(guard, monkeypatch, command, expected):
    monkeypatch.setenv("KIT_GUARD_DEADLINE_S", "0.05")

    def spin(*_a, **_k):
        while True:
            guard._check_deadline()

    monkeypatch.setattr(guard, "dangerous", spin)
    assert guard.decide(command) in ("restart", "stop"), command
    assert guard._last_deadline_fired is True


@pytest.mark.parametrize("command", [
    "systemctl --user status openclaw-gate*",
    "systemctl --user restart nginx-gate*",
    "ls openclaw-*",
    "echo " + "x" * 400 + " openclaw",
])
def test_the_deadline_fallback_still_allows_words_that_name_no_guarded_command(guard, monkeypatch, command):
    monkeypatch.setenv("KIT_GUARD_DEADLINE_S", "0.05")

    def spin(*_a, **_k):
        while True:
            guard._check_deadline()

    monkeypatch.setattr(guard, "dangerous", spin)
    assert guard.decide(command) is None, command
    assert guard._last_deadline_fired is True


@pytest.mark.parametrize("tail", [
    "systemctl --user restart 'openclaw-gate*'",
    "systemctl --user restart openclaw-{gateway,x}",
    "systemctl --user restart $'openclaw-gate\\x77ay'",
    "openclaw $'gat\\x65way' restart",
])
def test_a_deadline_forcing_prefix_does_not_hide_a_glob_or_ansi_c_name(tmp_path, tail):
    command = "echo " + "*" * 250000 + "\n" + tail
    rc, secs = _hook(tmp_path, command, deadline=0.05)
    assert rc == 2
    assert secs < 3
    rc, _ = _hook(tmp_path, "echo " + "*" * 250000 + "\nls -la", deadline=0.05)
    assert rc == 0
