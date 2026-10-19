"""ADR-0010 S14: sign-in for ticked, already installed tools; agy is only ever named, never started."""
from __future__ import annotations

import subprocess

import pytest

from ai_resources.setup import state, tools, ui
from test_setup_tools_members import DETECTORS, _detect


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)


class Rig:
    def __init__(self, monkeypatch, pick, signin_pick=()):
        self.asked, self.logins, self.runs, self.info = [], [], [], []

        def checkbox(message, choices, default=None, **k):
            self.asked.append((message, list(choices), list(default or [])))
            if message.startswith(tools.MEMBERS_TITLE):
                return list(pick)
            return list(signin_pick)
        monkeypatch.setattr(ui, "checkbox", checkbox)
        monkeypatch.setattr(tools, "install_one", lambda t, s: True)
        monkeypatch.setattr(ui, "info", lambda m, *a, **k: self.info.append(m))
        monkeypatch.setattr(ui, "detail", lambda m, *a, **k: self.info.append(m))
        monkeypatch.setattr(ui, "warn", lambda m, *a, **k: self.info.append(m))
        monkeypatch.setattr(ui, "record_action", lambda *a, **k: None)

        def run(argv, *a, **k):
            self.runs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)
        monkeypatch.setattr(subprocess, "run", run)

    def signin_rows(self):
        return [a for a in self.asked if a[0].startswith("Sign in now?")]


def test_ticked_installed_codex_is_listed_unticked(monkeypatch):
    """AC-S14a."""
    _detect(monkeypatch, installed=("claude", "codex"))
    rig = Rig(monkeypatch, pick=["claude", "codex"])
    tools.offer(state.SetupState(), members=True, members_default=("claude", "codex"))
    (_m, choices, default), = rig.signin_rows()
    assert [c.value for c in choices] == ["codex"] and default == []
    assert "codex login" in choices[0].title
    assert rig.runs == []                                   # Enter signs in to none


def test_ticking_codex_in_the_sign_in_list_runs_only_its_login(monkeypatch):
    _detect(monkeypatch, installed=("claude", "codex"))
    rig = Rig(monkeypatch, pick=["claude", "codex"], signin_pick=["codex"])
    tools.offer(state.SetupState(), members=True, members_default=("claude", "codex"))
    assert len(rig.runs) == 1
    assert rig.runs[0][0].split("/")[-1] == "codex" and rig.runs[0][-1] == "login"


def test_an_unticked_installed_tool_is_not_offered(monkeypatch):
    _detect(monkeypatch, installed=("claude", "codex"))
    rig = Rig(monkeypatch, pick=["claude"])
    tools.offer(state.SetupState(), members=True, members_default=("claude",))
    assert rig.signin_rows() == []


def test_yes_runs_no_login(monkeypatch):
    """AC-S14b: --yes never reaches the members list and never runs a login."""
    _detect(monkeypatch, installed=("claude", "codex"))
    rig = Rig(monkeypatch, pick=["claude", "codex"], signin_pick=["codex"])
    tools.offer(state.SetupState(), assume_yes=True, members=True, members_default=("claude", "codex"))
    assert rig.runs == [] and rig.signin_rows() == []


def test_agy_ticked_is_never_started(monkeypatch):
    """AC-S14c: ticked and installed, newly installed, or forced through _run_login, no argv starts with agy."""
    _detect(monkeypatch, installed=("claude", "agy", "codex"))
    rig = Rig(monkeypatch, pick=["claude", "agy", "codex"], signin_pick=["agy", "codex"])
    tools.offer(state.SetupState(), members=True, members_default=("claude", "agy", "codex"))
    assert not any(argv and argv[0].split("/")[-1] == "agy" for argv in rig.runs)
    assert all("agy" not in [c.value for c in choices] for _m, choices, _d in rig.signin_rows())
    assert tools._run_login("agy") is False
    assert not any(argv and argv[0].split("/")[-1] == "agy" for argv in rig.runs)
    assert any("agy" in line for line in rig.info)          # the command is named for the operator


def test_newly_installed_agy_is_named_not_offered(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch, pick=["claude", "agy"], signin_pick=["agy"])
    tools.offer(state.SetupState(), members=True, members_default=("claude",), preselect=("agy",))
    assert not any(argv and argv[0].split("/")[-1] == "agy" for argv in rig.runs)
    assert any("`agy`" in line for line in rig.info)


def test_bare_claude_is_not_offered_as_a_sign_in_for_an_installed_tool(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch, pick=["claude"])
    tools.offer(state.SetupState(), members=True, members_default=("claude",))
    assert rig.signin_rows() == []
