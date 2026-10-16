"""claude/agy/openclaw install offers: skip what's installed, ask once, track rc lines.

Every test mocks subprocess.run and detection — a real `curl | bash` must never run
from this suite. Run with:
    pytest tests/test_setup_tools.py -q
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import detection, state, tools, ui  # noqa: E402

pytestmark = pytest.mark.usefixtures("extra_tools_present")   # the selector now knows more CLIs


def _installed(name: str, path: str = "/usr/bin/x") -> detection.Detected:
    return detection.Detected(name, True, "1.0", path)


def _missing(name: str) -> detection.Detected:
    return detection.Detected(name, False, "", "", "install hint")


@pytest.fixture(autouse=True)
def _a_terminal(monkeypatch):
    """The interactive path of `tools.offer` needs a real terminal (2.2d, 6d); pytest's stdin is not one."""
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Guard rail: fail loudly if a test forgets to mock the installer command."""
    def _boom(*a, **k):
        raise AssertionError("a real subprocess.run was invoked — mock it in the test")
    monkeypatch.setattr(subprocess, "run", _boom)
    yield


def test_an_already_installed_tool_is_never_offered_or_reinstalled(monkeypatch):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _installed("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _installed("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _installed("OpenClaw"))
    assert tools.missing_tools() == []

    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    seen = {}

    def box(msg, choices, default=None, **k):
        seen["selectable"] = [c.value for c in choices if not c.disabled]
        return []
    monkeypatch.setattr(ui, "checkbox", box)
    s = state.SetupState()
    installed = tools.offer(s)
    assert installed == []
    assert seen == {}                                    # nothing is missing, so there is nothing to tick: no selector
    assert s.tracking.tools_installed_by_us == []


def test_only_the_missing_tools_are_reported(monkeypatch):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _installed("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _missing("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _missing("OpenClaw"))
    assert tools.missing_tools() == ["agy", "openclaw"]


def test_non_interactive_with_no_saved_answer_installs_nothing(monkeypatch):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _missing("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _missing("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _missing("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)

    s = state.SetupState()
    assert s.tracking.install_tools_answer is None
    installed = tools.offer(s)
    assert installed == []
    assert s.tracking.tools_installed_by_us == []


def test_non_interactive_with_a_saved_yes_no_longer_installs_anything(monkeypatch, tmp_path):
    """2.2b 3a: a saved yes (an old state file) is never replayed; before 2.2b this test asserted the replay."""
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _missing("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _missing("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _missing("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(tools, "_rc_paths", lambda: [tmp_path / "bashrc"])

    calls = []

    def fake_run(cmd, capture_output, text, timeout, stdin=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "installed ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    s = state.SetupState()
    s.tracking.install_tools_answer = "yes"
    installed = tools.offer(s)
    assert installed == [] and calls == []
    assert s.tracking.tools_installed_by_us == []
    assert s.tracking.install_tools_answer is None, "the stale answer is cleared, not carried forward"
    # `--yes` is the only unattended yes, and it runs the installers through bash -c
    assert set(tools.offer(s, assume_yes=True)) == {"claude", "agy", "openclaw"}
    assert len(calls) == 3
    for cmd in calls:
        assert cmd[0] == "bash" and cmd[1] == "-c"


def test_interactive_run_asks_once_and_does_not_reinstall_agy(monkeypatch, tmp_path):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _installed("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _missing("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _installed("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(tools, "_rc_paths", lambda: [tmp_path / "bashrc"])

    asked = []
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: (
        asked.append([getattr(c, "value", c) for c in choices]) or ["agy"]) if msg.startswith("Select") else [])

    def fake_run(cmd, capture_output, text, timeout, stdin=None):
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    s = state.SetupState()
    installed = tools.offer(s)
    assert installed == ["agy"]
    assert len(asked) == 1
    assert "agy" in asked[0]
    assert s.tracking.install_tools_answer is None, "2.2b 8b: the answer belongs to this run and is not stored"


def test_a_declined_offer_installs_nothing_and_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _missing("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _installed("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _installed("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: [])

    s = state.SetupState()
    installed = tools.offer(s)
    assert installed == []
    assert s.tracking.install_tools_answer is None, "2.2b 8b: a refusal is not stored either"

    # A second call in the same run must not prompt again.
    calls = []
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: calls.append(1) or [])
    tools.offer(s)
    assert calls == []


# --- AC-11: rc-file de-duplication -------------------------------------------------

def test_diff_new_lines_drops_a_line_already_present_before_install():
    before = {"/home/u/.bashrc": 'export PATH="$HOME/.local/bin:$PATH"\n# existing\n'}
    after = {"/home/u/.bashrc": (
        'export PATH="$HOME/.local/bin:$PATH"\n# existing\n'
        'export PATH="$HOME/.local/bin:$PATH"\n'  # agy's installer appended a duplicate
        'export AGY_HOME="$HOME/.agy"\n'           # a genuinely new line
    )}
    added = tools.diff_new_lines(before, after)
    assert added == {"/home/u/.bashrc": ['export AGY_HOME="$HOME/.agy"']}


def test_agy_install_records_only_the_new_rc_lines_it_appended(monkeypatch, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text('export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8")
    monkeypatch.setattr(tools, "_rc_paths", lambda: [bashrc])

    def fake_run(cmd, capture_output, text, timeout, stdin=None):
        # Simulate the real installer: appends a duplicate PATH line plus one new line.
        with bashrc.open("a", encoding="utf-8") as fh:
            fh.write('export PATH="$HOME/.local/bin:$PATH"\n')
            fh.write('export AGY_HOME="$HOME/.agy"\n')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    s = state.SetupState()
    ok = tools.install_one("agy", s)
    assert ok
    assert s.tracking.rc_lines_added["agy"] == ['export AGY_HOME="$HOME/.agy"']
    # AC-11: only tracking a duplicate as "not ours" isn't enough — the file itself
    # must end up with exactly one copy of the line the installer redundantly appended.
    final = bashrc.read_text(encoding="utf-8")
    assert final.count('export PATH="$HOME/.local/bin:$PATH"') == 1
    assert final == 'export PATH="$HOME/.local/bin:$PATH"\nexport AGY_HOME="$HOME/.agy"\n'


def test_dedupe_appended_lines_drops_only_the_duplicate_and_keeps_the_new_line(tmp_path):
    bashrc = tmp_path / ".bashrc"
    before_text = 'export PATH="$HOME/.local/bin:$PATH"\n# existing\n'
    bashrc.write_text(
        before_text
        + 'export PATH="$HOME/.local/bin:$PATH"\n'  # duplicate the installer appended
        + 'export AGY_HOME="$HOME/.agy"\n',          # genuinely new line
        encoding="utf-8",
    )
    tools._dedupe_appended_lines(bashrc, before_text)
    assert bashrc.read_text(encoding="utf-8") == before_text + 'export AGY_HOME="$HOME/.agy"\n'


def test_dedupe_appended_lines_is_a_no_op_when_nothing_was_appended(tmp_path):
    bashrc = tmp_path / ".bashrc"
    before_text = 'export PATH="$HOME/.local/bin:$PATH"\n'
    bashrc.write_text(before_text, encoding="utf-8")
    tools._dedupe_appended_lines(bashrc, before_text)
    assert bashrc.read_text(encoding="utf-8") == before_text


def test_dedupe_appended_lines_leaves_the_file_alone_when_the_prefix_no_longer_matches(tmp_path):
    """If the installer did something other than a clean append (rewrote the file, for
    instance), guessing at what to remove is worse than doing nothing."""
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("completely different content\n", encoding="utf-8")
    tools._dedupe_appended_lines(bashrc, 'export PATH="$HOME/.local/bin:$PATH"\n')
    assert bashrc.read_text(encoding="utf-8") == "completely different content\n"


def test_claude_install_does_not_inspect_rc_files_at_all(monkeypatch, tmp_path):
    """Only agy's official installer is documented to touch PATH; claude's and
    openclaw's installers are not diffed, so a coincidental edit to an rc file by
    something else during the run is never misattributed to them."""
    calls = {"rc_reads": 0}
    real_rc_paths = tools._rc_paths
    def counting_rc_paths():
        calls["rc_reads"] += 1
        return real_rc_paths()
    monkeypatch.setattr(tools, "_rc_paths", counting_rc_paths)

    def fake_run(cmd, capture_output, text, timeout, stdin=None):
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    s = state.SetupState()
    tools.install_one("claude", s)
    assert calls["rc_reads"] == 0
    assert "claude" not in s.tracking.rc_lines_added


# --- AC-12: teardown only touches tools the kit installed --------------------------

def test_teardown_offers_only_the_tools_the_kit_installed(monkeypatch, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text('export PATH="$HOME/.local/bin:$PATH"\nexport AGY_HOME="$HOME/.agy"\n',
                       encoding="utf-8")
    monkeypatch.setattr(tools, "_rc_paths", lambda: [bashrc])
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)

    offered = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=False: (offered.append(msg), True)[1])

    s = state.SetupState()
    s.tracking.tools_installed_by_us = ["agy"]
    s.tracking.rc_lines_added = {"agy": ['export AGY_HOME="$HOME/.agy"']}
    # A pre-existing tool the user installed themselves must never be offered.
    s.cockpits["claude"] = state.CockpitState(installed=True)

    removed = tools.teardown(s)
    assert removed == ["agy"]
    assert len(offered) == 1
    assert "agy" in offered[0].lower() or "Antigravity" in offered[0]
    assert "claude" not in offered[0].lower()
    assert s.tracking.tools_installed_by_us == []
    assert "agy" not in s.tracking.rc_lines_added
    assert bashrc.read_text(encoding="utf-8") == 'export PATH="$HOME/.local/bin:$PATH"\n'


def test_teardown_with_nothing_tracked_does_nothing_and_never_prompts(monkeypatch):
    asked = []
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: asked.append(1) or True)
    s = state.SetupState()
    assert tools.teardown(s) == []
    assert asked == []


def test_teardown_declined_leaves_tracking_untouched(monkeypatch, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text('export AGY_HOME="$HOME/.agy"\n', encoding="utf-8")
    monkeypatch.setattr(tools, "_rc_paths", lambda: [bashrc])
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)

    s = state.SetupState()
    s.tracking.tools_installed_by_us = ["agy"]
    s.tracking.rc_lines_added = {"agy": ['export AGY_HOME="$HOME/.agy"']}
    removed = tools.teardown(s)
    assert removed == []
    assert s.tracking.tools_installed_by_us == ["agy"]
    assert bashrc.read_text(encoding="utf-8") == 'export AGY_HOME="$HOME/.agy"\n'


def test_teardown_under_non_interactive_removes_nothing(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    s = state.SetupState()
    s.tracking.tools_installed_by_us = ["agy"]
    assert tools.teardown(s) == []
    assert s.tracking.tools_installed_by_us == ["agy"]


# --- kit 2.2 E1: an installer prompt defaults to No; `--yes` is the explicit way to say yes ---------------------

def _all_missing(monkeypatch):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _missing("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _missing("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _missing("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)


def test_pressing_enter_at_the_install_prompt_installs_nothing(monkeypatch):
    _all_missing(monkeypatch)
    seen = {}

    def enter(msg, choices, default=None, **k):
        seen["default"] = default
        return list(default or [])                       # what Enter does: accept the pre-ticked rows
    monkeypatch.setattr(ui, "checkbox", enter)
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda tool_id, s: ran.append(tool_id) or True)
    s = state.SetupState()
    assert tools.offer(s) == []
    assert seen["default"] == [] and ran == []        # nothing is pre-ticked, so Enter installs nothing
    assert s.tracking.install_tools_answer is None


def test_ticking_a_tool_installs_only_that_tool(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: ["agy"])
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda tool_id, s: ran.append(tool_id) or True)
    monkeypatch.setattr(tools, "_sign_in_flow", lambda *a, **k: None)
    assert tools.offer(state.SetupState()) == ["agy"] and ran == ["agy"]


def test_no_sign_in_group_is_offered_in_the_selector_when_every_tool_is_installed(monkeypatch):
    for fn, name in (("detect_claude_code", "Claude Code"), ("detect_agy", "agy"), ("detect_openclaw", "OpenClaw")):
        monkeypatch.setattr(detection, fn, lambda n=name: detection.Detected(n, True, "1", "/x/" + n))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    seen = {"titles": []}

    def box(msg, choices, default=None, **k):
        seen["titles"] += [c.title for c in choices]
        return []
    monkeypatch.setattr(ui, "checkbox", box)
    assert tools.offer(state.SetupState()) == []
    assert not any(t.startswith("Sign in to installed tools") for t in seen["titles"])


def test_yes_installs_without_asking_and_does_not_persist_the_answer(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda tool_id, s: ran.append(tool_id) or True)
    s = state.SetupState()
    assert tools.offer(s, assume_yes=True) == ["claude", "agy", "openclaw"] and ran == ["claude", "agy", "openclaw"]
    assert s.tracking.install_tools_answer is None


def test_a_yes_run_does_not_make_a_later_unattended_run_install(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda tool_id, s: ran.append(tool_id) or True)
    s = state.SetupState()
    assert tools.offer(s, assume_yes=True) and ran
    ran.clear()
    assert tools.offer(s) == [] and ran == []
    assert s.tracking.install_tools_answer is None


def test_yes_also_answers_a_non_interactive_run_that_has_no_saved_answer(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda tool_id, s: ran.append(tool_id) or True)
    assert tools.offer(state.SetupState(), assume_yes=True) and ran


def test_the_setup_command_has_a_yes_flag_and_it_is_off_by_default():
    from ai_resources import cli
    assert cli.build_parser().parse_args(["setup"]).yes is False
    assert cli.build_parser().parse_args(["setup", "--yes"]).yes is True


def test_the_pipx_installer_menu_defaults_to_skip(monkeypatch):
    from ai_resources.setup import wizard
    picked = {}

    def select(message, choices, default=0):
        picked["default"], picked["choices"] = default, choices
        return None
    monkeypatch.setattr(ui, "select", select)
    assert wizard._try_install_pipx() is False
    chosen = [c for c in picked["choices"] if c.value == picked["default"]]
    assert len(chosen) == 1 and "Skip" in chosen[0].title
