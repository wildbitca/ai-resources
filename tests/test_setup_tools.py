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


def _installed(name: str, path: str = "/usr/bin/x") -> detection.Detected:
    return detection.Detected(name, True, "1.0", path)


def _missing(name: str) -> detection.Detected:
    return detection.Detected(name, False, "", "", "install hint")


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

    s = state.SetupState()
    installed = tools.offer(s)
    assert installed == []
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


def test_non_interactive_with_a_saved_yes_installs_the_missing_tools_and_tracks_them(monkeypatch, tmp_path):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _missing("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _missing("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _missing("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(tools, "_rc_paths", lambda: [tmp_path / "bashrc"])

    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "installed ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    s = state.SetupState()
    s.tracking.install_tools_answer = "yes"
    installed = tools.offer(s)
    assert set(installed) == {"claude", "agy", "openclaw"}
    assert set(s.tracking.tools_installed_by_us) == {"claude", "agy", "openclaw"}
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
    monkeypatch.setattr(ui, "confirm", lambda msg, default=True: (asked.append(msg), True)[1])

    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    s = state.SetupState()
    installed = tools.offer(s)
    assert installed == ["agy"]
    assert len(asked) == 1
    assert "agy" in asked[0].lower()
    assert s.tracking.install_tools_answer == "yes"


def test_a_declined_offer_installs_nothing_and_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(detection, "detect_claude_code", lambda: _missing("Claude Code"))
    monkeypatch.setattr(detection, "detect_agy", lambda: _installed("agy"))
    monkeypatch.setattr(detection, "detect_openclaw", lambda: _installed("OpenClaw"))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)

    s = state.SetupState()
    installed = tools.offer(s)
    assert installed == []
    assert s.tracking.install_tools_answer == "no"

    # A second call in the same run must not prompt again.
    calls = []
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: calls.append(1) or True)
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

    def fake_run(cmd, capture_output, text, timeout):
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

    def fake_run(cmd, capture_output, text, timeout):
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
