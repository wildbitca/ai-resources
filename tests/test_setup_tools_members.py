"""Screen 1 "Your tools" (ADR-0009): ONE list where every coding CLI is a tool.

Installed tools are tickable as team members, missing ones are ticked to install, the order is installed first and then
alphabetical, and Claude Code carries a label, not a position or a pre-tick. Antigravity is never ticked on its own.
Offline: every installer, login and `--version` probe is a fake."""
from __future__ import annotations

import argparse
import subprocess
import types

import pytest

from ai_resources.setup import detection, state, tools, ui, wizard
from wizard_fakes import Script, host_env

DETECTORS = {"claude": "detect_claude_code", "agy": "detect_agy", "openclaw": "detect_openclaw",
             "cursor": "detect_cursor_agent", "codex": "detect_codex",
             "aider": "detect_aider", "copilot": "detect_copilot", "continue": "detect_continue",
             "opencode": "detect_opencode", "windsurf": "detect_windsurf"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)

    def boom(*a, **k):
        raise AssertionError("a real subprocess.run was invoked")
    monkeypatch.setattr(subprocess, "run", boom)


def _detect(monkeypatch, installed=(), versions=None):
    versions = versions or {}
    monkeypatch.setattr(detection, "detect_gemini_cli", lambda: detection.Detected("Gemini CLI", False, "", ""))
    for tid, fn in DETECTORS.items():
        ok = tid in installed
        monkeypatch.setattr(detection, fn, lambda n=tid, o=ok, v=versions.get(tid, "1.0"): detection.Detected(
            n, o, v if o else "", f"/bin/{n}" if o else ""))


class Rig:
    def __init__(self, monkeypatch, pick=None):
        self.asked, self.installed = [], []

        def checkbox(message, choices, default=None, **k):
            self.asked.append((message, list(choices), list(default or [])))
            if message.startswith(tools.MEMBERS_TITLE):
                return list(pick if pick is not None else default or [])
            return list(default or [])
        monkeypatch.setattr(ui, "checkbox", checkbox)
        monkeypatch.setattr(tools, "install_one", lambda t, s: self.installed.append(t) or True)
        monkeypatch.setattr(tools, "_run_login", lambda tid: True)
        monkeypatch.setattr(ui, "info", lambda *a, **k: None)
        monkeypatch.setattr(ui, "detail", lambda *a, **k: None)

    @property
    def rows(self):
        return self.asked[0][1]


def _offer(monkeypatch, installed, *, pick=None, default=(), **kw):
    _detect(monkeypatch, installed=installed)
    rig = Rig(monkeypatch, pick=pick)
    s = state.SetupState()
    got = tools.offer(s, members=True, members_default=tuple(default), **kw)
    return rig, s, got


def test_one_list_is_shown_even_when_nothing_is_missing(monkeypatch):
    rig, s, got = _offer(monkeypatch, installed=tuple(DETECTORS), default=("claude", "codex"))
    # one members list; the only other prompt is the sign-in list for the ticked, installed tool with a login
    # command (ADR-0010 S14), which starts unticked
    members = [a for a in rig.asked if a[0].startswith(tools.MEMBERS_TITLE)]
    assert len(members) == 1 and got == []
    assert [a[0].split("?")[0] for a in rig.asked[1:]] == ["Sign in now"] and rig.asked[1][2] == []
    assert s._tool_members_this_run == ["claude", "codex"]


def test_installed_rows_are_tickable_and_missing_ones_are_tickable_to_install(monkeypatch):
    rig, s, got = _offer(monkeypatch, installed=("claude", "codex"), default=("claude", "codex"))
    rows = {c.value: c for c in rig.rows}
    assert not rows["claude"].disabled and not rows["codex"].disabled
    assert "installed v1.0" in rows["claude"].title and "not installed" in rows["aider"].title
    assert not rows["aider"].disabled and rows["windsurf"].disabled.startswith("manual install: https://")


def test_order_is_installed_first_then_alphabetical(monkeypatch):
    rig, *_ = _offer(monkeypatch, installed=("openclaw", "claude", "agy", "codex"))
    order = [c.value for c in rig.rows]
    installed = [v for v in order if v in ("openclaw", "claude", "agy", "codex")]
    assert order[:4] == installed == ["agy", "claude", "codex", "openclaw"]
    missing = order[4:]
    assert missing == sorted(missing, key=lambda t: tools.TOOLS[t].label.lower())


def test_the_order_does_not_depend_on_the_tools_dict_order(monkeypatch):
    _detect(monkeypatch, installed=("claude", "codex"))
    first = [c.value for c in tools._member_choices(tools._detect_tools())]
    monkeypatch.setattr(tools, "TOOLS", dict(reversed(list(tools.TOOLS.items()))))
    again = [c.value for c in tools._member_choices(tools._detect_tools())]
    assert first == again


def test_claude_code_has_a_label_and_no_special_position_or_tick(monkeypatch):
    rig, *_ = _offer(monkeypatch, installed=("agy", "claude", "codex"), default=("codex",))
    rows = {c.value: c for c in rig.rows}
    assert "recommended to start" in rows["claude"].title
    assert [c.value for c in rig.rows][:3] == ["agy", "claude", "codex"]
    assert "claude" not in rig.asked[0][2]
    assert sum("recommended to start" in c.title for c in rig.rows) == 1


def test_the_label_shows_when_claude_code_is_missing_too(monkeypatch):
    rig, *_ = _offer(monkeypatch, installed=("codex",))
    rows = {c.value: c for c in rig.rows}
    assert "recommended to start" in rows["claude"].title and "not installed" in rows["claude"].title


def test_antigravity_is_not_ticked_unless_the_caller_says_so_and_says_where_the_content_goes(monkeypatch):
    rig, *_ = _offer(monkeypatch, installed=("agy", "claude"), default=("claude",))
    rows = {c.value: c for c in rig.rows}
    assert "agy" not in rig.asked[0][2] and "claude" in rig.asked[0][2]
    assert "sends repo content to Google; terms check next" in rows["agy"].title


def test_enter_on_the_defaults_installs_nothing(monkeypatch):
    rig, s, got = _offer(monkeypatch, installed=("claude",), default=("claude",))
    assert got == [] and rig.installed == []


def test_unticking_an_installed_tool_never_uninstalls_it(monkeypatch):
    rig, s, got = _offer(monkeypatch, installed=("claude", "codex"), pick=["claude"], default=("claude", "codex"))
    assert s._tool_members_this_run == ["claude"] and got == [] and rig.installed == []
    assert s.tracking.tools_installed_by_us == []


def test_ticking_a_missing_tool_installs_it_and_makes_it_a_member(monkeypatch):
    rig, s, got = _offer(monkeypatch, installed=("claude",), pick=["claude", "aider"], default=("claude",))
    assert rig.installed == ["aider"] and got == ["aider"]
    assert s._tool_members_this_run == ["claude", "aider"]


def test_a_tool_that_failed_to_install_is_not_a_member(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch, pick=["claude", "aider"])
    monkeypatch.setattr(tools, "install_one", lambda t, s: False)
    s = state.SetupState()
    tools.offer(s, members=True, members_default=("claude",))
    assert s._tool_members_this_run == ["claude"]


def test_a_retired_tool_is_not_offered(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch)
    cockpits = {"gemini": detection.Detected("Gemini CLI", True, "0.9", "/bin/gemini")}
    tools.offer(state.SetupState(), members=True, cockpits=cockpits, members_default=("claude",))
    assert "info:gemini" not in [c.value for c in rig.rows] and "gemini" not in [c.value for c in rig.rows]


def test_without_a_terminal_nothing_is_asked_and_the_caller_derives_the_team(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    s = state.SetupState()
    assert tools.offer(s, members=True) == [] and rig.asked == [] and s._tool_members_this_run is None


def test_yes_installs_only_the_documented_trio_and_asks_nothing(monkeypatch):
    _detect(monkeypatch, installed=())
    rig = Rig(monkeypatch)
    s = state.SetupState()
    got = tools.offer(s, assume_yes=True, members=True)
    assert tools.REQUIRED_FOR_ANTIGRAVITY == ("claude", "agy", "openclaw")
    assert sorted(rig.installed) == ["agy", "claude", "openclaw"] and sorted(got) == sorted(rig.installed)
    assert rig.asked == [] and s._tool_members_this_run is None


def test_a_dry_run_previews_the_list_and_installs_nothing(monkeypatch):
    rig, s, got = _offer(monkeypatch, installed=("claude",), pick=["claude", "aider"], default=("claude",),
                         dry_run=True)
    assert got == [] and rig.installed == []
    assert s._tool_members_this_run == ["claude"]


def test_the_list_is_asked_once_per_run(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch)
    s = state.SetupState()
    tools.offer(s, members=True, members_default=("claude",))
    tools.offer(s, members=True, members_default=("claude",))
    assert len(rig.asked) == 1


# --- the wizard side: screen 1 writes the team ------------------------------------------------------------
COCKPITS = {cid: types.SimpleNamespace(installed=True, version="1.0", binary_path=f"/b/{cid}", name=cid)
            for cid in ("agy", "claude", "codex")}


def _wizard_step(monkeypatch, tmp_path, *, offer, saved=None, installed=None):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: dict(installed or COCKPITS))
    monkeypatch.setattr(tools, "offer", offer)
    Script().install(monkeypatch)
    s = state.SetupState()
    if saved is not None:
        s.profile.customizations = {"__targets__": {"selected": saved}}
    assert wizard._step2_cockpits(s) == 0
    return s


def test_enter_on_three_installed_tools_selects_them_all_except_antigravity(monkeypatch, tmp_path):
    s = _wizard_step(monkeypatch, tmp_path, offer=lambda *a, **k: [])
    assert s.profile.customizations["__targets__"]["selected"] == ["claude", "codex"]


def test_ticks_from_the_list_become_the_targets(monkeypatch, tmp_path):
    def offer(s, *a, **k):
        s._tool_members_this_run = ["codex"]
        return []
    s = _wizard_step(monkeypatch, tmp_path, offer=offer)
    assert s.profile.customizations["__targets__"]["selected"] == ["codex"]


def test_saved_targets_are_honoured_and_a_gone_tool_is_dropped(monkeypatch, tmp_path):
    s = _wizard_step(monkeypatch, tmp_path, offer=lambda *a, **k: [], saved=["codex", "cursor"])
    assert s.profile.customizations["__targets__"]["selected"] == ["codex"]


def test_an_installed_gemini_is_not_offered_but_its_state_entry_loads(monkeypatch, tmp_path):
    seen = {}

    def offer(s, *a, **k):
        seen["default"] = k["members_default"]
        return []
    cockpits = {**COCKPITS, "gemini": types.SimpleNamespace(installed=True, version="0.9", binary_path="/b/g",
                                                            name="Gemini CLI")}
    s = _wizard_step(monkeypatch, tmp_path, offer=offer, installed=cockpits)
    assert "gemini" not in seen["default"]
    assert "gemini" not in s.profile.customizations["__targets__"]["selected"]
    assert s.cockpits["gemini"].installed is True


def test_the_screen_stops_when_no_tool_is_installed(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {
        "claude": types.SimpleNamespace(installed=False, version="", binary_path="", name="Claude Code")})
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    Script().install(monkeypatch)
    assert wizard._step2_cockpits(state.SetupState()) == 1


def test_step_seven_asks_no_tool_checkbox_and_only_openclaw_has_questions(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    script = Script()
    script.install(monkeypatch)
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude", "codex"]}}
    assert wizard._step7_cockpit_config(s) == 0
    assert script.asked == []
    c = types.SimpleNamespace(s=s)
    spec = next(sp for sp in wizard._build_steps(False) if sp.id == "cockpits")
    assert spec.skip(c) is not None and spec.skip(c)[1] == ""
    s.profile.customizations = {"__targets__": {"selected": ["claude", "openclaw"]}}
    assert spec.skip(c) is None
