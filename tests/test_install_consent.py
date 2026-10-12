"""Consent for installers is per run, defaults to No, and is never stored or replayed (2.2b section 3).

Every installer prompt of the kit, with its default:

  1. tools.offer            `Install missing tools now?`        curl | bash, npm        No (was No since 2.2)
  2. wizard pipx menu       `How should I install pipx?`        brew/apt/dnf/pip        Skip (since 2.2)
  3. wizard LiteLLM pipx    `Install LiteLLM via pipx ...?`     pipx install            No (was Yes)
  4. wizard LiteLLM venv    `Create venv ... pip install ...?`  pip install              No (was Yes)
  5. wizard image pull      `Pull <image> now?`                 docker/podman pull      No (was Yes)
  6. voice local engine     `Install ffmpeg, whisper-cpp ...?`  brew install            No (new prompt)
  7. openclaw bootstrap     one confirm per change              brew/npm/systemd        No unless --yes (was Yes)

Nothing here runs a real installer: every subprocess or install function is stubbed.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import detection, litellm, state, tools, ui, wizard  # noqa: E402
from ai_resources.setup.cockpits import _openclaw_voice as voice  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real subprocess.run was invoked")
    monkeypatch.setattr(subprocess, "run", boom)


def _all_missing(monkeypatch):
    for fn, name in (("detect_claude_code", "Claude Code"), ("detect_agy", "agy"), ("detect_openclaw", "OpenClaw")):
        monkeypatch.setattr(detection, fn, lambda n=name: detection.Detected(n, False, "", "", "hint"))


def _enter(monkeypatch):
    """What pressing Enter does at every prompt: the default."""
    asked: list[tuple[str, bool]] = []

    def confirm(msg, default=True):
        asked.append((msg, default))
        return default
    monkeypatch.setattr(ui, "confirm", confirm)
    return asked


# --- 3a / 3c: tools.offer ----------------------------------------------------------------------------------------

def test_enter_at_the_tools_prompt_installs_nothing_and_stores_nothing(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    asked = _enter(monkeypatch)
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda t, s: ran.append(t) or True)
    s = state.SetupState()
    assert tools.offer(s) == [] and ran == []
    assert asked and asked[0][1] is False
    assert s.tracking.install_tools_answer is None


def test_an_old_saved_yes_installs_nothing_in_either_mode(monkeypatch):
    _all_missing(monkeypatch)
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda t, s: ran.append(t) or True)
    for non_interactive in (True, False):
        monkeypatch.setattr(ui, "is_non_interactive", lambda ni=non_interactive: ni)
        asked = _enter(monkeypatch)
        s = state.SetupState()
        s.tracking.install_tools_answer = "yes"
        assert tools.offer(s) == [] and ran == []
        assert (len(asked) == 1) is (not non_interactive), "an interactive run asks again; an unattended one skips"
        assert s.tracking.install_tools_answer is None


def test_a_saved_yes_in_a_state_file_is_dropped_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    s = state.SetupState()
    s.tracking.install_tools_answer = "yes"
    state.save(s)
    assert "install_tools_answer: 'yes'" in state.state_path().read_text(encoding="utf-8")
    assert state.load().tracking.install_tools_answer is None


def test_a_saved_no_does_not_stop_an_interactive_run_from_asking_again(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    asked: list[str] = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=True: asked.append(msg) or True)
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda t, s: ran.append(t) or True)
    s = state.SetupState()
    s.tracking.install_tools_answer = "no"
    assert tools.offer(s) == ["claude", "agy", "openclaw"] and len(asked) == 1
    assert s.tracking.install_tools_answer is None


def test_non_interactive_installs_nothing_and_yes_installs_and_persists_nothing(monkeypatch):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    ran = []
    monkeypatch.setattr(tools, "install_one", lambda t, s: ran.append(t) or True)
    s = state.SetupState()
    assert tools.offer(s) == [] and ran == []
    assert tools.offer(s, assume_yes=True) == ["claude", "agy", "openclaw"]
    assert s.tracking.install_tools_answer is None
    ran.clear()
    assert tools.offer(s) == [] and ran == [], "the yes of that run does not carry to the next one"


def test_the_wizard_passes_the_yes_flag_to_the_tools_offer(monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(tools, "offer", lambda s, *a, assume_yes=False, **k: seen.append(assume_yes) or [])
    monkeypatch.setattr(detection, "detect_all_cockpits",
                        lambda: {"claude": detection.Detected("Claude Code", True, "1", "/x")})
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": staticmethod(lambda *a, **k: None)})())
    for flag in (False, True):
        monkeypatch.setattr(wizard, "_ASSUME_YES", flag)
        assert wizard._step2_cockpits(state.SetupState()) == 0
    assert seen == [False, True]


# --- 3b: the other installer prompts ----------------------------------------------------------------------------

def test_the_litellm_pipx_prompt_defaults_to_skip_and_never_installs_unattended(monkeypatch):
    monkeypatch.setattr(detection, "detect_pipx", lambda: detection.Detected("pipx", True, "1.5", "/usr/bin/pipx"))
    monkeypatch.setattr(detection, "find_compatible_python",
                        lambda *a, **k: detection.Detected("python", True, "3.12", "/usr/bin/python3.12"))
    monkeypatch.setattr(detection, "detect_litellm_binary", lambda: detection.Detected("litellm", False, "", ""))
    installs: list = []
    monkeypatch.setattr(litellm, "install_litellm_pipx", lambda **k: installs.append(k) or (True, ""))
    asked = _enter(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert wizard._step3_pipx(state.SetupState()) == 1
    assert installs == [] and asked and all(default is False for _m, default in asked)


def test_the_litellm_venv_and_image_pull_prompts_default_to_no(monkeypatch, tmp_path):
    monkeypatch.setattr(detection, "find_compatible_python",
                        lambda *a, **k: detection.Detected("python", True, "3.12", "/usr/bin/python3.12"))
    monkeypatch.setattr(litellm, "venv_dir", lambda: tmp_path / "venv")
    ran: list = []
    monkeypatch.setattr(litellm, "install_litellm_venv", lambda **k: ran.append("venv") or (True, ""))
    monkeypatch.setattr(litellm, "pull_image", lambda image: ran.append("pull") or (True, ""))
    asked = _enter(monkeypatch)
    assert wizard._step3_pipvenv(state.SetupState()) == 1
    monkeypatch.setattr(detection, "detect_container_runtime",
                        lambda: ("docker", detection.Detected("docker", True, "27", "/usr/bin/docker")))
    monkeypatch.setattr(detection, "detect_compose", lambda cli: True)
    monkeypatch.setattr(wizard, "_ask_bind_and_port", lambda s: None)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "")
    monkeypatch.setattr(state, "save", lambda s: None)
    assert wizard._step3_docker(state.SetupState()) == 0
    assert ran == [] and len(asked) == 2 and all(default is False for _m, default in asked)


def test_the_voice_local_engine_install_has_its_own_confirm_and_never_runs_unattended(monkeypatch):
    calls: list = []
    monkeypatch.setattr(voice.transcriber, "find_binary", lambda b: "/bin/brew" if b == "brew" else "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
        AssertionError("brew install ran")))
    asked = _enter(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    assert voice.ensure_binaries(state.SetupState()) is False
    assert asked and asked[0][1] is False and "brew install" in asked[0][0]
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert voice.ensure_binaries(state.SetupState()) is False
    assert calls == []


# --- 3b: bootstrap -----------------------------------------------------------------------------------------------

def _bootstrap_args(**kw):
    base = dict(dry_run=False, only="", upgrade=False, memory_high="12G", yes=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_bootstrap_confirms_each_change_with_default_no_unless_yes(monkeypatch):
    captured: dict = {}

    def fake_bootstrap(**kw):
        captured["confirm"] = kw["confirm"]
        return [], None
    monkeypatch.setattr(host, "bootstrap", fake_bootstrap)

    assert host.cmd_bootstrap(_bootstrap_args(yes=True)) == 0
    assert captured["confirm"]("units", "install the units") is True

    asked = _enter(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert host.cmd_bootstrap(_bootstrap_args()) == 0
    assert captured["confirm"]("units", "install the units") is False and asked == []

    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert captured["confirm"]("units", "x") is False and asked == []        # a piped y is not a confirmation

    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    assert captured["confirm"]("units", "install the units") is False        # Enter
    assert asked == [("install the units?", False)]


def test_the_bootstrap_verb_has_a_yes_flag_and_it_is_off_by_default():
    ap = argparse.ArgumentParser()
    host.add_subparser(ap.add_subparsers(dest="cmd"))
    assert ap.parse_args(["openclaw", "bootstrap"]).yes is False
    assert ap.parse_args(["openclaw", "bootstrap", "--yes"]).yes is True
