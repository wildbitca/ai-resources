"""Step 1 tool selector: choose which tools to install, then offer sign-in for the new ones.

Offline: every installer and login is a fake; a real `curl | bash` or a real login must never run here.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import detection, state, tools, ui  # noqa: E402

# Integration: touches or fakes an external tool (openclaw, node, git, installers...); skipped unless --integration (see tests/conftest.py).
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("extra_tools_present")]   # the selector now knows more CLIs

INSTALL = "Select the tools to install"
LOGIN = "Sign in now"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)

    def boom(*a, **k):
        raise AssertionError("a real subprocess.run was invoked")
    monkeypatch.setattr(subprocess, "run", boom)


def _detect(monkeypatch, **installed):
    for fn, tid, name in (("detect_claude_code", "claude", "Claude Code"), ("detect_agy", "agy", "agy"),
                          ("detect_openclaw", "openclaw", "OpenClaw")):
        ok = installed.get(tid, False)
        monkeypatch.setattr(detection, fn, lambda n=name, o=ok: detection.Detected(n, o, "1" if o else "", "/bin/x" if o else ""))


class Rig:
    def __init__(self, monkeypatch, install_pick=None, login_pick=None):
        self.asked: list[tuple[str, list, list]] = []
        self.installed: list[str] = []
        self.logins: list[str] = []
        self.notes: list[str] = []

        def checkbox(message, choices, default=None, **k):
            self.asked.append((message, [getattr(c, "value", c) for c in choices], list(default or [])))
            if message.startswith(INSTALL):
                return list(install_pick if install_pick is not None else default or [])
            if message.startswith(LOGIN):
                return list(login_pick if login_pick is not None else default or [])
            raise AssertionError(message)
        monkeypatch.setattr(ui, "checkbox", checkbox)
        monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("confirm")))
        monkeypatch.setattr(tools, "install_one", lambda t, s: self.installed.append(t) or True)
        monkeypatch.setattr(tools, "_run_login", lambda tid: self.logins.append(tid) or True)
        monkeypatch.setattr(ui, "info", lambda m, *a, **k: self.notes.append(str(m)))
        monkeypatch.setattr(ui, "detail", lambda m, *a, **k: self.notes.append(str(m)))


def test_a_subset_installs_exactly_that_subset(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, install_pick=["agy"], login_pick=[])
    assert tools.offer(state.SetupState()) == ["agy"]
    assert rig.installed == ["agy"]


def test_every_known_tool_is_listed_and_nothing_is_preticked(monkeypatch):
    _detect(monkeypatch, claude=True)
    rig = Rig(monkeypatch, install_pick=[])
    tools.offer(state.SetupState())
    msg, values, default = rig.asked[0]
    assert set(tools.TOOLS) <= set(values)
    assert not default, "nothing is pre-ticked: Enter must not run a remote installer"


def test_selecting_none_installs_nothing_and_asks_no_sign_in(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, install_pick=[])
    s = state.SetupState()
    assert tools.offer(s) == [] and rig.installed == []
    assert [a[0].startswith(INSTALL) for a in rig.asked] == [True]
    assert s.tracking.tools_installed_by_us == []


def test_an_installed_tool_is_never_reinstalled_even_if_returned(monkeypatch):
    _detect(monkeypatch, claude=True)
    rig = Rig(monkeypatch, install_pick=["claude", "agy"], login_pick=[])
    assert tools.offer(state.SetupState()) == ["agy"]
    assert rig.installed == ["agy"]


def test_sign_in_runs_only_for_newly_installed_tools_that_are_ticked(monkeypatch):
    _detect(monkeypatch, openclaw=True)
    rig = Rig(monkeypatch, install_pick=["claude", "agy"], login_pick=["claude"])
    tools.offer(state.SetupState())
    login_asks = [a for a in rig.asked if a[0].startswith(LOGIN)]
    assert len(login_asks) == 1
    # ADR-0010 S14: the kit never starts agy, so Antigravity is named in a line and is not a sign-in row
    assert [v for v in login_asks[0][1] if not v.startswith("login:")] == ["claude"]
    assert rig.logins == ["claude"]
    assert any("`agy`" in n for n in rig.notes)


def test_an_installed_tool_is_never_signed_in_by_the_selector_and_gets_an_informational_line(monkeypatch):
    _detect(monkeypatch, claude=True)
    rig = Rig(monkeypatch, install_pick=["login:claude"])      # a stale login row value must be ignored
    assert tools.offer(state.SetupState()) == []
    assert rig.installed == [] and rig.logins == []
    assert any("sign in later" in n and "`claude`" in n for n in rig.notes)


def test_the_selector_is_asked_once_per_run(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, install_pick=["openclaw"], login_pick=[])
    s = state.SetupState()
    tools.offer(s)
    tools.offer(s)
    assert len([a for a in rig.asked if a[0].startswith(INSTALL)]) == 1


def test_yes_installs_the_missing_required_set_without_a_prompt_and_prints_the_login_commands(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert tools.offer(state.SetupState(), assume_yes=True) == ["claude", "agy", "openclaw"]
    assert rig.logins == []
    assert any("claude" in n and "sign in" in n.lower() for n in rig.notes)


def test_without_a_terminal_nothing_is_asked_installed_or_signed_in(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert tools.offer(state.SetupState()) == [] and rig.asked == [] and rig.installed == []


def test_no_terminal_for_the_login_prints_the_command_instead_of_running_it(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, install_pick=["claude"])
    seen = iter([True, False])           # a terminal for the selector, none for the sign-in
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: next(seen, False))
    assert tools.offer(state.SetupState()) == ["claude"]
    assert rig.logins == [] and not [a for a in rig.asked if a[0].startswith(LOGIN)]
    assert any("`claude`" in n for n in rig.notes)


def test_dry_run_installs_and_asks_nothing(monkeypatch):
    """Without a terminal a dry run asks nothing (with a terminal it previews the selector: see test_setup_tool_catalog)."""
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert tools.offer(state.SetupState(), dry_run=True) == []
    assert rig.asked == [] and rig.installed == [] and rig.logins == []


def test_login_inherits_stdio_and_never_captures(monkeypatch):
    _detect(monkeypatch)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    done = []
    monkeypatch.setattr(ui, "record_action", done.append)
    assert tools._run_login("claude") is True
    assert seen["argv"] == ["claude"]
    assert not {"capture_output", "stdout", "stderr", "input", "env"} & set(seen["kw"])
    assert any("claude" in d.lower() for d in done)


def test_a_failed_login_is_reported_and_not_recorded(monkeypatch):
    _detect(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1))
    done = []
    monkeypatch.setattr(ui, "record_action", done.append)
    assert tools._run_login("agy") is False and done == []


def test_every_tool_with_a_login_has_a_command_and_toolspec_stays_backwards_compatible():
    spec = tools.ToolSpec("x", "X", "echo", lambda: detection.Detected("x", False, "", ""))
    assert spec.login_cmd == () and spec.purpose == "" and spec.login_hint == ""
    assert all(t.purpose for t in tools.TOOLS.values())
    assert tools.TOOLS["claude"].login_cmd and tools.TOOLS["agy"].login_cmd
