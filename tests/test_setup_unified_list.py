"""Step 1 shows ONE list: installed tools with their version, installable ones tickable, manual ones informational.

Offline: every installer, login and `--version` probe is a fake; a real installer or login must never run here.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import detection, state, tools, ui, wizard  # noqa: E402

INSTALL = "Select the tools to install"
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


def _detect(monkeypatch, installed=(), versions=None):
    versions = versions or {}
    for tid, fn in DETECTORS.items():
        ok = tid in installed
        monkeypatch.setattr(detection, fn, lambda n=tid, o=ok, v=versions.get(tid, "1.0"): detection.Detected(
            n, o, v if o else "", f"/bin/{n}" if o else ""))


class Rig:
    def __init__(self, monkeypatch, pick=None, login_pick=None):
        self.asked: list[tuple[str, list, list]] = []
        self.installed: list[str] = []
        self.logins: list[str] = []
        self.notes: list[str] = []

        def checkbox(message, choices, default=None, **k):
            self.asked.append((message, list(choices), list(default or [])))
            if message.startswith(INSTALL):
                return list(pick if pick is not None else default or [])
            return list(login_pick if login_pick is not None else default or [])
        monkeypatch.setattr(ui, "checkbox", checkbox)
        monkeypatch.setattr(tools, "install_one", lambda t, s: self.installed.append(t) or True)
        monkeypatch.setattr(tools, "_run_login", lambda tid: self.logins.append(tid) or True)
        monkeypatch.setattr(ui, "info", lambda m, *a, **k: self.notes.append(str(m)))
        monkeypatch.setattr(ui, "detail", lambda m, *a, **k: self.notes.append(str(m)))


def _by_value(rig):
    return {c.value: c for c in rig.asked[0][1]}


# --- the single list ------------------------------------------------------------------------------

def test_the_list_merges_installed_with_version_installable_and_manual_rows(monkeypatch):
    _detect(monkeypatch, installed=("claude",), versions={"claude": "2.1.300"})
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    rows = _by_value(rig)
    assert "v2.1.300" in rows["claude"].title and rows["claude"].disabled == "installed"
    assert "not installed" in rows["gemini"].title and "npm install -g @google/gemini-cli" in rows["gemini"].title
    assert not rows["gemini"].disabled
    assert rows["windsurf"].disabled.startswith("manual install: https://")
    assert len(rig.asked) == 1


def test_the_message_says_enter_with_nothing_ticked_skips(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    assert "Enter with nothing ticked = skip, install nothing" in rig.asked[0][0]
    assert not [c for c in rig.asked[0][1] if "skip" in c.title.lower()], "skip is not a row"


def test_a_cockpit_outside_the_catalog_still_appears_with_its_version(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=[])
    extra = {"zed": detection.Detected("Zed Agent", True, "0.9", "/bin/zed")}
    tools.offer(state.SetupState(), cockpits=extra)
    row = _by_value(rig)["info:zed"]
    assert "Zed Agent" in row.title and "v0.9" in row.title and row.disabled == "installed"


def test_nothing_ticked_installs_nothing_and_a_subset_installs_that_subset(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=[])
    assert tools.offer(state.SetupState()) == [] and rig.installed == []
    rig = Rig(monkeypatch, pick=["gemini", "aider"])
    assert tools.offer(state.SetupState()) == ["gemini", "aider"]


def test_the_preselect_is_the_only_thing_ticked(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=None)
    tools.offer(state.SetupState(), preselect=("claude", "agy", "openclaw"))
    assert rig.asked[0][2] == ["claude", "agy", "openclaw"]


# --- the scan list is not printed twice -----------------------------------------------------------

def test_no_separate_scan_when_the_selector_is_shown(monkeypatch):
    _detect(monkeypatch)
    Rig(monkeypatch, pick=[])
    printed = []
    tools.offer(state.SetupState(), print_scan=lambda: printed.append(1))
    assert printed == []
    printed.clear()
    tools.offer(state.SetupState(), dry_run=True, print_scan=lambda: printed.append(1))   # the preview selector too
    assert printed == []


@pytest.mark.parametrize("case", ["yes", "non_interactive", "no_terminal", "dry_no_terminal", "dry_yes", "all_installed"])
def test_the_scan_is_printed_on_every_path_without_a_selector(monkeypatch, case):
    _detect(monkeypatch, installed=tuple(DETECTORS) if case == "all_installed" else ())
    rig = Rig(monkeypatch)
    kw = {}
    if case == "yes":
        kw["assume_yes"] = True
    if case == "non_interactive":
        monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    if case in ("no_terminal", "dry_no_terminal"):
        monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    if case.startswith("dry"):
        kw["dry_run"] = True
    if case == "dry_yes":
        kw.update(assume_yes=True)
    printed = []
    tools.offer(state.SetupState(), print_scan=lambda: printed.append(1), **kw)
    assert printed == [1] and rig.asked == []


def _wizard(monkeypatch, scan):
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: scan)
    rows = []
    monkeypatch.setattr(ui, "detected_table", lambda r: rows.extend(r))
    for name in ("section", "info", "warn", "detail"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": staticmethod(lambda *a, **k: None)})())
    monkeypatch.setattr(wizard, "_ASSUME_YES", False)
    monkeypatch.setattr(wizard, "_DRY_RUN", False)
    return rows


def test_the_wizard_prints_the_scan_only_when_no_selector_is_shown(monkeypatch):
    _detect(monkeypatch, installed=("claude",), versions={"claude": "2.1.300"})
    scan = {"claude": detection.Detected("Claude Code", True, "2.1.300", "/bin/claude")}
    rows = _wizard(monkeypatch, scan)
    Rig(monkeypatch, pick=[])
    assert wizard._step2_cockpits(state.SetupState()) == 0
    assert rows == []                                          # the selector carried the information
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert wizard._step2_cockpits(state.SetupState()) == 0
    assert rows == [("✔", "Claude Code", "v2.1.300  /bin/claude")]   # plain scan, with the version, as before


# --- sign-in is a separate screen -----------------------------------------------------------------

def test_the_selector_has_no_sign_in_group(monkeypatch):
    _detect(monkeypatch, installed=("claude", "codex"))
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    assert not [c for c in rig.asked[0][1] if str(c.value).startswith("login:") or "Sign in" in c.title]


def test_sign_in_is_asked_only_for_newly_installed_tools_with_a_login_flow_and_is_preticked(monkeypatch):
    _detect(monkeypatch, installed=("codex",))
    rig = Rig(monkeypatch, pick=["gemini", "aider"], login_pick=["gemini"])
    assert tools.offer(state.SetupState()) == ["gemini", "aider"]
    login = [a for a in rig.asked if a[0].startswith("Sign in now")]
    assert len(login) == 1
    assert [c.value for c in login[0][1]] == ["gemini"] and login[0][2] == ["gemini"]   # aider: none; codex: not new
    assert rig.logins == ["gemini"]


def test_installed_tools_get_one_dim_line_with_the_command_and_no_checkbox(monkeypatch):
    _detect(monkeypatch, installed=("codex",))
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    lines = [n for n in rig.notes if "sign in later" in n]
    assert len(lines) == 1 and "`codex login`" in lines[0]
    assert rig.logins == [] and len(rig.asked) == 1


def test_the_sign_in_choice_is_asked_once_per_run(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch)
    s = state.SetupState()
    tools._sign_in_flow(s, ["gemini"], interactive=True)
    tools._sign_in_flow(s, ["gemini"], interactive=True)
    assert len([a for a in rig.asked if a[0].startswith("Sign in now")]) == 1
    assert rig.logins == ["gemini"]


def test_the_dry_run_preview_is_asked_once_per_run(monkeypatch):
    _detect(monkeypatch)
    rig = Rig(monkeypatch, pick=["gemini"])
    s = state.SetupState()
    tools.offer(s, dry_run=True)
    tools.offer(s, dry_run=True)
    assert len(rig.asked) == 1 and rig.installed == []


# --- the gaps ---------------------------------------------------------------------------------------

def test_cursor_login_is_refused_when_the_sibling_agent_is_missing(monkeypatch, tmp_path):
    (tmp_path / "cursor-agent").write_text("")
    monkeypatch.setattr(detection, "detect_cursor_agent",
                        lambda: detection.Detected("Cursor Agent CLI", True, "1", str(tmp_path / "cursor-agent")))
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
    assert tools._run_login("cursor") is False
    assert ran == [] and any("agent login" in w for w in warned)


def test_cursor_login_is_refused_when_cursor_is_not_detected_at_all(monkeypatch):
    monkeypatch.setattr(detection, "detect_cursor_agent", lambda: detection.Detected("Cursor Agent CLI", False, "", ""))
    monkeypatch.setattr(ui, "warn", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
    assert tools._run_login("cursor") is False and ran == []


@pytest.mark.parametrize("tid", ["claude", "agy"])
def test_a_bare_tool_sign_in_is_recorded_as_started_not_signed_in(monkeypatch, tid):
    _detect(monkeypatch, installed=(tid,))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    done, said = [], []
    monkeypatch.setattr(ui, "record_action", done.append)
    monkeypatch.setattr(ui, "info", said.append)
    assert tools._run_login(tid) is True
    assert len(done) == 1 and done[0].startswith("started sign-in to") and "signed in to" not in done[0]
    assert any("finish signing in inside the tool" in m for m in said)


def test_a_login_subcommand_exit_zero_is_still_recorded_as_signed_in(monkeypatch):
    _detect(monkeypatch, installed=("codex",))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    done = []
    monkeypatch.setattr(ui, "record_action", done.append)
    assert tools._run_login("codex") is True and done == ["signed in to Codex CLI"]


def test_the_legacy_copilot_extension_is_shown_distinctly_and_never_runs_bare_copilot(monkeypatch):
    _detect(monkeypatch, installed=())
    legacy = detection.Detected("GitHub Copilot CLI", True, "1.2", "/usr/bin/gh", variant="gh-extension")
    monkeypatch.setattr(detection, "detect_copilot", lambda: legacy)
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    title = _by_value(rig)["copilot"].title
    assert "gh copilot extension, v1.2" in title


def test_the_legacy_copilot_extension_never_runs_bare_copilot_for_sign_in(monkeypatch):
    legacy = detection.Detected("GitHub Copilot CLI", True, "1.2", "/usr/bin/gh", variant="gh-extension")
    monkeypatch.setattr(detection, "detect_copilot", lambda: legacy)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(ui, "info", lambda *a, **k: None)
    assert tools._run_login("copilot") is False and ran == []


def test_the_copilot_later_sign_in_line_names_gh_for_the_extension(monkeypatch):
    _detect(monkeypatch)
    legacy = detection.Detected("GitHub Copilot CLI", True, "1.2", "/usr/bin/gh", variant="gh-extension")
    monkeypatch.setattr(detection, "detect_copilot", lambda: legacy)
    rig = Rig(monkeypatch, pick=[])
    tools.offer(state.SetupState())
    line = [n for n in rig.notes if "sign in later" in n][0]
    assert "`gh auth login`" in line and "`copilot`" not in line


def test_detect_copilot_marks_the_gh_extension_variant(monkeypatch):
    monkeypatch.setattr(detection, "_which", lambda b: "/usr/bin/gh" if b == "gh" else "")
    monkeypatch.setattr(detection, "_run", lambda cmd, timeout=5: (0, "gh copilot 1.2.3"))
    found = detection.detect_copilot()
    assert found.installed and found.variant == "gh-extension"
    monkeypatch.setattr(detection, "_which", lambda b: "/usr/bin/copilot" if b == "copilot" else "")
    assert detection.detect_copilot().variant == ""


def test_the_agy_doc_url_is_a_human_readable_vendor_page_not_the_script():
    url = tools.TOOLS["agy"].doc_url
    assert url.startswith("https://antigravity.google/docs/") and not url.endswith(".sh")
