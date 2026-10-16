"""`ai-resources setup --dry-run` does not offer or run an installer, and `--dry-run --yes` installs nothing and
says so (kit 2.2c, 3b). Every installer and every prompt is a stub that fails the test when it is reached."""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import detection, litellm, state, tools, ui, wizard  # noqa: E402

pytestmark = pytest.mark.usefixtures("extra_tools_present")   # the selector now knows more CLIs


@pytest.fixture(autouse=True)
def _nothing_real(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real subprocess.run was invoked")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(wizard, "_DRY_RUN", True, raising=False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a prompt was shown")))
    monkeypatch.setattr(ui, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a menu was shown")))
    monkeypatch.setattr(state, "save", lambda s: (_ for _ in ()).throw(AssertionError("state was saved")))
    said: list[str] = []
    monkeypatch.setattr(ui, "info", lambda msg, *a, **k: said.append(str(msg)))
    return said


@pytest.fixture
def said(_nothing_real):
    return _nothing_real


def _all_missing(monkeypatch):
    for fn, name in (("detect_claude_code", "Claude Code"), ("detect_agy", "agy"), ("detect_openclaw", "OpenClaw")):
        monkeypatch.setattr(detection, fn, lambda n=name: detection.Detected(n, False, "", "", "hint"))


@pytest.mark.parametrize("assume_yes", [False, True])
def test_the_tools_offer_of_a_dry_run_asks_nothing_and_installs_nothing(monkeypatch, said, assume_yes):
    _all_missing(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(tools, "install_one", lambda t, s: (_ for _ in ()).throw(AssertionError("installed " + t)))
    assert tools.offer(state.SetupState(), assume_yes=assume_yes, dry_run=True) == []
    [line] = said
    assert "DRY RUN" in line and "nothing is installed" in line and "Claude Code" in line
    assert ("--yes does not install" in line) is assume_yes


def test_the_wizard_tools_step_passes_the_dry_run_to_the_offer(monkeypatch):
    seen = []
    monkeypatch.setattr(tools, "offer", lambda s, *a, assume_yes=False, dry_run=False, **k:
                        seen.append((assume_yes, dry_run)) or [])
    monkeypatch.setattr(detection, "detect_all_cockpits",
                        lambda: {"claude": detection.Detected("Claude Code", True, "1", "/x")})
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": staticmethod(lambda *a, **k: None)})())
    monkeypatch.setattr(wizard, "_ASSUME_YES", True)
    assert wizard._step2_cockpits(state.SetupState()) == 0
    assert seen == [(True, True)]


def test_a_dry_run_with_yes_says_it_installs_nothing(monkeypatch, said):
    monkeypatch.setattr(wizard, "_run", lambda args: 0)
    monkeypatch.setattr(wizard, "_DRY_RUN", False, raising=False)
    assert wizard.run(argparse.Namespace(dry_run=True, yes=True)) == 0
    assert any("--yes installs nothing in a dry run" in line for line in said)
    said.clear()
    assert wizard.run(argparse.Namespace(dry_run=True, yes=False)) == 0
    assert said == []
    assert wizard.run(argparse.Namespace(dry_run=False, yes=True)) == 0
    assert said == [] and wizard._DRY_RUN is False


def test_a_missing_pipx_is_not_offered_in_a_dry_run(monkeypatch, said):
    monkeypatch.setattr(detection, "detect_pipx", lambda: detection.Detected("pipx", False, "", ""))
    monkeypatch.setattr(wizard, "_try_install_pipx", lambda: (_ for _ in ()).throw(AssertionError("offered pipx")))
    assert wizard._step3_pipx(state.SetupState()) == 0
    assert any("DRY RUN" in line and "pipx" in line for line in said)


def test_litellm_is_not_installed_or_reinstalled_via_pipx_in_a_dry_run(monkeypatch, said):
    monkeypatch.setattr(detection, "detect_pipx", lambda: detection.Detected("pipx", True, "1.5", "/usr/bin/pipx"))
    monkeypatch.setattr(detection, "find_compatible_python",
                        lambda *a, **k: detection.Detected("python", True, "3.12", "/usr/bin/python3.12"))
    monkeypatch.setattr(litellm, "install_litellm_pipx",
                        lambda **k: (_ for _ in ()).throw(AssertionError("pipx install ran")))
    monkeypatch.setattr(detection, "detect_litellm_binary", lambda: detection.Detected("litellm", False, "", ""))
    assert wizard._step3_pipx(state.SetupState()) == 0
    monkeypatch.setattr(detection, "detect_litellm_binary",
                        lambda: detection.Detected("litellm", True, "1.0", "/usr/bin/litellm"))
    assert wizard._step3_pipx(state.SetupState()) == 0
    assert sum("DRY RUN" in line for line in said) == 2


def test_the_litellm_venv_is_not_created_or_reinstalled_in_a_dry_run(monkeypatch, said, tmp_path):
    monkeypatch.setattr(detection, "find_compatible_python",
                        lambda *a, **k: detection.Detected("python", True, "3.12", "/usr/bin/python3.12"))
    monkeypatch.setattr(litellm, "venv_dir", lambda: tmp_path / "venv")
    monkeypatch.setattr(litellm, "install_litellm_venv",
                        lambda **k: (_ for _ in ()).throw(AssertionError("venv install ran")))
    assert wizard._step3_pipvenv(state.SetupState()) == 0
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "litellm").write_text("", encoding="utf-8")
    assert wizard._step3_pipvenv(state.SetupState()) == 0
    assert sum("DRY RUN" in line for line in said) == 2


def test_the_image_is_not_pulled_and_the_state_is_not_saved_in_a_dry_run(monkeypatch, said):
    monkeypatch.setattr(detection, "detect_container_runtime",
                        lambda: ("docker", detection.Detected("docker", True, "27", "/usr/bin/docker")))
    monkeypatch.setattr(detection, "detect_compose", lambda cli: True)
    monkeypatch.setattr(wizard, "_ask_bind_and_port", lambda s: None)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "")
    monkeypatch.setattr(litellm, "pull_image", lambda image: (_ for _ in ()).throw(AssertionError("pulled")))
    assert wizard._step3_docker(state.SetupState()) == 0
    assert any("DRY RUN" in line and "pull" in line for line in said)
