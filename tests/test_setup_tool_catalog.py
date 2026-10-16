"""Step 1 offers every AI CLI the scan can detect: catalog integrity, selector, wizard placement, sign-in.

Offline: every installer, login and `--version` probe is a fake; a real installer or login must never run here.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import detection, state, tools, ui, wizard  # noqa: E402

INSTALL = "Select the tools to install"
ALL_IDS = {"claude", "agy", "openclaw", "cursor", "gemini", "codex", "aider", "copilot", "continue", "opencode",
           "windsurf"}
INSTALLABLE = ALL_IDS - {"windsurf"}
DETECTORS = {"claude": "detect_claude_code", "agy": "detect_agy", "openclaw": "detect_openclaw",
             "cursor": "detect_cursor_agent", "gemini": "detect_gemini_cli", "codex": "detect_codex",
             "aider": "detect_aider", "copilot": "detect_copilot", "continue": "detect_continue",
             "opencode": "detect_opencode", "windsurf": "detect_windsurf"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)

    def boom(*a, **k):
        raise AssertionError("a real subprocess.run was invoked")
    monkeypatch.setattr(subprocess, "run", boom)


def _detect(monkeypatch, installed=()):
    """Every known tool is missing except the ids in `installed`."""
    for tid, fn in DETECTORS.items():
        ok = tid in installed
        monkeypatch.setattr(detection, fn, lambda n=tid, o=ok: detection.Detected(n, o, "1" if o else "", f"/bin/{n}" if o else ""))


class Rig:
    def __init__(self, monkeypatch, pick=None):
        self.asked: list[tuple[str, list, list, dict]] = []
        self.installed: list[str] = []
        self.logins: list[str] = []
        self.notes: list[str] = []

        def checkbox(message, choices, default=None, **k):
            self.asked.append((message, [c.value for c in choices], list(default or []),
                               {c.value: c.disabled for c in choices}))
            if message.startswith(INSTALL):
                return list(pick if pick is not None else default or [])
            return []
        monkeypatch.setattr(ui, "checkbox", checkbox)
        monkeypatch.setattr(tools, "install_one", lambda t, s: self.installed.append(t) or True)
        monkeypatch.setattr(tools, "_run_login", lambda tid: self.logins.append(tid) or True)
        monkeypatch.setattr(ui, "info", lambda m, *a, **k: self.notes.append(str(m)))
        monkeypatch.setattr(ui, "detail", lambda m, *a, **k: self.notes.append(str(m)))


# --- catalog ---------------------------------------------------------------------------

def test_the_catalog_covers_every_cockpit_the_scan_detects(monkeypatch):
    _detect(monkeypatch)
    assert set(tools.TOOLS) == set(detection.detect_all_cockpits()) == ALL_IDS


def test_every_entry_has_a_purpose_and_a_recorded_doc_url_and_date():
    for tid, spec in tools.TOOLS.items():
        assert spec.purpose.strip(), tid
        assert re.fullmatch(r"https://[^\s]+", spec.doc_url), f"{tid} lacks a recorded doc URL"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec.doc_checked), f"{tid} lacks the date it was checked"


def test_install_commands_are_plain_strings_without_shell_metacharacters():
    for tid, spec in tools.TOOLS.items():
        if not spec.install_cmd:
            continue
        cmd = spec.install_cmd
        # the only documented metacharacter is the final pipe of a vendor's `curl ... | bash`
        body, _, sink = cmd.partition(" | ")
        assert sink in ("", "bash"), f"{tid}: {cmd!r}"
        assert not re.search(r"[;&`$()<>\\\n]|\|\||&&", body), f"{tid}: {cmd!r}"
        assert " | " not in body
        assert not sink or body.startswith("curl "), f"{tid}: a pipe is only for a vendor curl installer"


def test_a_product_without_a_cli_installer_is_manual_with_a_vendor_url():
    manual = {tid for tid, s in tools.TOOLS.items() if not s.install_cmd}
    assert manual == {"windsurf"}
    for tid in manual:
        assert tools.TOOLS[tid].manual_url.startswith("https://") and not tools.TOOLS[tid].login_cmd


def test_package_managers_are_preferred_over_piped_installers_when_documented():
    piped = {tid for tid, s in tools.TOOLS.items() if "|" in s.install_cmd}
    assert piped == {"claude", "agy", "cursor"}      # no documented package-manager install for these


@pytest.mark.parametrize("tid,argv", [
    ("claude", ("claude",)), ("agy", ("agy",)), ("cursor", ("agent", "login")), ("gemini", ("gemini",)),
    ("codex", ("codex", "login")), ("copilot", ("copilot",)), ("continue", ("cn", "login")),
    ("opencode", ("opencode", "auth", "login")), ("aider", ()), ("openclaw", ()), ("windsurf", ())])
def test_login_commands_match_the_ones_verified_against_vendor_docs(tid, argv):
    assert tools.TOOLS[tid].login_cmd == argv


def test_aider_shows_the_env_var_hint_and_never_asks_for_a_key():
    spec = tools.TOOLS["aider"]
    assert "OPENAI_API_KEY" in spec.login_hint and "never asks" in spec.login_hint


# --- selector ---------------------------------------------------------------------------

def test_the_selector_lists_every_known_cli_with_missing_ones_selectable_and_nothing_ticked(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    _, values, default, disabled = rig.asked[0]
    assert ALL_IDS <= set(values) and default == []
    assert disabled["claude"] == "installed"
    assert str(disabled["windsurf"]).startswith("manual install: https://")
    assert all(not disabled[t] for t in INSTALLABLE - {"claude"})


def test_a_subset_installs_exactly_that_subset(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=["gemini", "aider", "opencode"])
    assert tools.offer(state.SetupState()) == ["gemini", "aider", "opencode"]
    assert rig.installed == ["gemini", "aider", "opencode"]


def test_none_installs_nothing(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=[])
    assert tools.offer(state.SetupState()) == [] and rig.installed == []


def test_installed_tools_are_not_reinstalled_even_if_returned(monkeypatch):
    _detect(monkeypatch, installed=("gemini",))
    rig = Rig(monkeypatch, pick=["gemini", "codex"])
    assert tools.offer(state.SetupState()) == ["codex"] and rig.installed == ["codex"]


def test_a_manual_install_entry_is_not_selectable_and_never_executed(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=["windsurf"])      # forced into the answer
    assert tools.offer(state.SetupState()) == [] and rig.installed == []
    monkeypatch.undo()                              # the real install_one must also refuse it
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(ui, "warn", lambda *a, **k: None)
    assert tools.install_one("windsurf", state.SetupState()) is False


def test_the_context_may_preselect_only_what_it_requires(monkeypatch):
    _detect(monkeypatch, installed=("openclaw",))
    rig = Rig(monkeypatch, pick=None)
    assert tools.offer(state.SetupState(), preselect=tools.REQUIRED_FOR_ANTIGRAVITY) == ["claude", "agy"]
    assert rig.asked[0][2] == ["claude", "agy"]      # the installed openclaw is not ticked


def test_yes_installs_only_the_required_set_not_the_other_clis(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    assert tools.offer(state.SetupState(), assume_yes=True) == ["claude", "agy", "openclaw"]
    assert rig.asked == []


def test_non_interactive_and_no_terminal_install_and_ask_nothing(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert tools.offer(state.SetupState()) == []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert tools.offer(state.SetupState()) == []
    assert rig.asked == [] and rig.installed == []


@pytest.mark.parametrize("assume_yes", [False, True])
def test_dry_run_without_a_prompt_path_says_what_it_would_offer(monkeypatch, assume_yes):
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    if not assume_yes:
        monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert tools.offer(state.SetupState(), assume_yes=assume_yes, dry_run=True) == []
    assert rig.asked == [] and rig.installed == []
    assert any("DRY RUN" in n and "nothing is installed" in n for n in rig.notes)


def test_dry_run_on_a_terminal_previews_the_selector_and_installs_nothing(monkeypatch):
    _detect(monkeypatch, installed=("claude",))
    rig = Rig(monkeypatch, pick=["gemini", "login:claude"])
    s = state.SetupState()
    assert tools.offer(s, dry_run=True) == []
    assert len(rig.asked) == 1 and rig.installed == [] and rig.logins == []
    assert any("would install Gemini CLI with `npm install -g @google/gemini-cli`" in n for n in rig.notes)
    assert any("would sign in to Claude Code" in n for n in rig.notes)
    assert s.tracking.tools_installed_by_us == []


def test_dry_run_preview_with_nothing_ticked_says_so(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState(), dry_run=True)
    assert any("no tool selected" in n for n in rig.notes)


# --- sign-in follow-up, state, teardown ------------------------------------------------------

def test_sign_in_is_offered_per_new_tool_with_its_own_login_and_aider_has_none(monkeypatch):
    _detect(monkeypatch)
    asks = []

    def checkbox(message, choices, default=None, **k):
        asks.append((message, [c.value for c in choices], list(default or [])))
        return ["codex", "aider", "gemini"] if message.startswith(INSTALL) else ["codex"]
    monkeypatch.setattr(ui, "checkbox", checkbox)
    monkeypatch.setattr(tools, "install_one", lambda t, s: True)
    logins = []
    monkeypatch.setattr(tools, "_run_login", lambda tid: logins.append(tid) or True)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    assert tools.offer(state.SetupState()) == ["gemini", "codex", "aider"]
    login_ask = [a for a in asks if a[0].startswith("Sign in now")][0]
    assert login_ask[1] == ["gemini", "codex"] and login_ask[2] == ["gemini", "codex"]   # aider has no sign-in
    assert logins == ["codex"]


def test_a_login_runs_the_verified_argv_with_inherited_stdio_and_is_recorded_only_on_success(monkeypatch):
    _detect(monkeypatch)
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0 if len(calls) == 1 else 2)
    monkeypatch.setattr(subprocess, "run", fake_run)
    done = []
    monkeypatch.setattr(ui, "record_action", done.append)
    monkeypatch.setattr(ui, "warn", lambda *a, **k: None)
    assert tools._run_login("codex") is True and tools._run_login("opencode") is False
    assert [c[0] for c in calls] == [["codex", "login"], ["opencode", "auth", "login"]]
    assert all(not ({"capture_output", "stdout", "stderr", "input", "env"} & set(kw)) for _, kw in calls)
    assert done == ["signed in to Codex CLI"]


def test_the_cursor_login_uses_the_agent_binary_beside_the_detected_cursor_agent(monkeypatch, tmp_path):
    (tmp_path / "cursor-agent").write_text("")
    (tmp_path / "agent").write_text("")
    monkeypatch.setattr(detection, "detect_cursor_agent",
                        lambda: detection.Detected("Cursor Agent CLI", True, "1", str(tmp_path / "cursor-agent")))
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(ui, "record_action", lambda *a: None)
    assert tools._run_login("cursor") is True
    assert seen == [[str(tmp_path / "agent"), "login"]]


def test_install_tracks_state_and_prints_the_login_hint(monkeypatch):
    _detect(monkeypatch)
    ran = []
    monkeypatch.setattr(tools, "_run_install", lambda cmd, timeout=300: ran.append(cmd) or (True, ""))
    hints = []
    monkeypatch.setattr(ui, "detail", lambda m, *a, **k: hints.append(str(m)))
    s = state.SetupState()
    assert tools.install_one("aider", s) is True
    assert ran == ["pipx install aider-chat"]
    assert s.tracking.tools_installed_by_us == ["aider"]
    assert s.tracking.tool_install_methods == {"aider": "pipx install aider-chat"}
    assert s.tracking.rc_lines_added == {}
    assert any("OPENAI_API_KEY" in h for h in hints)


def test_a_failed_install_is_not_tracked(monkeypatch):
    monkeypatch.setattr(tools, "_run_install", lambda cmd, timeout=300: (False, "boom"))
    monkeypatch.setattr(ui, "error", lambda *a, **k: None)
    s = state.SetupState()
    assert tools.install_one("gemini", s) is False and s.tracking.tools_installed_by_us == []


def test_teardown_still_removes_only_what_the_kit_installed(monkeypatch):
    s = state.SetupState()
    s.tracking.tools_installed_by_us = ["gemini"]
    s.tracking.tool_install_methods = {"gemini": "npm install -g @google/gemini-cli"}
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    assert tools.teardown(s) == ["gemini"]
    assert s.tracking.tools_installed_by_us == [] and s.tracking.tool_install_methods == {}
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    s.tracking.tools_installed_by_us = ["gemini"]
    assert tools.teardown(s) == [] and s.tracking.tools_installed_by_us == ["gemini"]


# --- wizard placement ---------------------------------------------------------------------------

def _wizard_rig(monkeypatch, scans):
    calls = []
    scan_iter = iter(scans)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: next(scan_iter))
    monkeypatch.setattr(tools, "offer", lambda s, **k: calls.append(k) or (["gemini"] if len(scans) > 1 else []))
    for name in ("section", "detected_table", "info", "warn"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": staticmethod(lambda *a, **k: None)})())
    return calls


def _scan(*installed):
    return {t: detection.Detected(t, t in installed, "1", "/x") for t in ("claude", "gemini")}


def test_the_offer_comes_right_after_the_scan_even_when_no_cockpit_is_installed(monkeypatch):
    calls = _wizard_rig(monkeypatch, [_scan(), _scan("gemini")])     # first run on a bare machine
    s = state.SetupState()
    assert wizard._step2_cockpits(s) == 0
    assert len(calls) == 1 and s.cockpits["gemini"].installed is True    # re-scanned after the install


def test_a_bare_machine_where_nothing_gets_installed_still_stops_after_asking(monkeypatch):
    calls = _wizard_rig(monkeypatch, [_scan()])
    assert wizard._step2_cockpits(state.SetupState()) == 1 and len(calls) == 1


def test_the_offer_also_comes_on_a_re_run(monkeypatch):
    calls = _wizard_rig(monkeypatch, [_scan("claude")])
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState()
    assert wizard._step2_cockpits(s) == 0 and len(calls) == 1


# --- detection -----------------------------------------------------------------------------------

def test_cursor_agent_and_the_copilot_binary_are_detected_without_a_real_probe(monkeypatch):
    monkeypatch.setattr(detection, "_which_extra", lambda b: "/h/.local/bin/cursor-agent" if b == "cursor-agent" else "")
    monkeypatch.setattr(detection, "_run", lambda cmd, timeout=5: (0, "2026.10.14"))
    assert detection.detect_cursor_agent().installed is True
    assert detection.detect_cursor().installed is True            # the agent counts as the Cursor cockpit
    monkeypatch.setattr(detection, "_which", lambda b: "/usr/bin/copilot" if b == "copilot" else "")
    found = detection.detect_copilot()
    assert found.installed and found.binary_path == "/usr/bin/copilot"
