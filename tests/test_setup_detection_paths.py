"""Detection finds tools that are installed off the process PATH (service unit, minimal PATH) and never calls them
"not installed" when the binary exists. Offline: the binaries are tiny fake scripts in a tmp dir; no real tool runs."""
from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import detection  # noqa: E402

DETECTORS = {
    "claude": ("detect_claude_code", "claude"), "openclaw": ("detect_openclaw", "openclaw"),
    "cursor-agent": ("detect_cursor_agent", "cursor-agent"), "codex": ("detect_codex", "codex"),
    "aider": ("detect_aider", "aider"), "cn": ("detect_continue", "cn"), "opencode": ("detect_opencode", "opencode"),
    "agy": ("detect_agy", "agy"), "copilot": ("detect_copilot", "copilot"), "gemini": ("detect_gemini_cli", "gemini"),
}


def _fake(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def minimal_env(tmp_path, monkeypatch):
    """HOME is a tmp dir and PATH has no tool or Homebrew dir at all (the operator's `PATH=/usr/bin:/bin` case)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for var in ("HOMEBREW_PREFIX", "NPM_CONFIG_PREFIX", "npm_config_prefix"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(detection, "_SYSTEM_BIN_DIRS", ())      # the host's own Homebrew must not answer
    return home


@pytest.mark.parametrize("sub", [".local/bin", ".npm-global/bin", ".local/share/pnpm", ".cargo/bin", ".bun/bin",
                                 ".local/share/fnm/node-versions/v22.1.0/installation/bin"])
@pytest.mark.parametrize("binary", sorted(DETECTORS))
def test_every_detector_finds_a_tool_in_a_home_bin_dir_off_path(minimal_env, binary, sub):
    fn, name = DETECTORS[binary]
    if binary in ("gemini",) and sub != ".local/bin":
        pytest.skip("same lookup; covered once")
    _fake(minimal_env / sub / name, 'echo "tool 3.4.5"')
    found = getattr(detection, fn)()
    assert found.installed and found.binary_path == str(minimal_env / sub / name)
    assert found.version == "3.4.5"


def test_homebrew_prefix_from_the_environment_is_searched(minimal_env, tmp_path, monkeypatch):
    prefix = tmp_path / "brew"
    _fake(prefix / "bin" / "codex", 'echo "codex-cli 0.9.1"')
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    found = detection.detect_codex()
    assert found.installed and found.binary_path == str(prefix / "bin" / "codex") and found.version == "0.9.1"


def test_the_fixed_homebrew_dirs_are_candidates(minimal_env, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(minimal_env))
    dirs = {str(d) for d in detection._extra_bin_dirs()}
    assert {"/home/linuxbrew/.linuxbrew/bin", "/opt/homebrew/bin", "/usr/local/bin"} <= dirs


def test_a_missing_tool_is_reported_missing_with_its_hint(minimal_env):
    found = detection.detect_openclaw()
    assert not found.installed and found.binary_path == "" and "openclaw.ai/install.sh" in found.install_hint


def test_openclaw_version_line_with_a_commit_sha_is_parsed(minimal_env):
    _fake(minimal_env / ".local/bin/openclaw", 'echo "OpenClaw 2026.10.4 (383c124)"')
    found = detection.detect_openclaw()
    assert found.installed and found.version == "2026.10.4"


@pytest.mark.parametrize("body", ["echo boom >&2; exit 3", "exit 127"])
def test_a_binary_that_fails_its_version_probe_is_installed_with_an_unknown_version(minimal_env, body):
    _fake(minimal_env / ".local/bin/openclaw", body)
    found = detection.detect_openclaw()
    assert found.installed is True and found.version == "" and found.binary_path.endswith("/openclaw")


def test_a_probe_timeout_is_installed_with_an_unknown_version(minimal_env, monkeypatch):
    _fake(minimal_env / ".local/bin/openclaw", "echo OpenClaw 2026.10.4")
    monkeypatch.setattr(detection, "_run", lambda cmd, timeout=5: (-1, ""))
    found = detection.detect_openclaw()
    assert found.installed is True and found.version == ""


def test_the_version_probe_uses_the_resolved_absolute_path_and_the_binarys_dir_on_path(minimal_env):
    """A shebang `env node` must find `node` beside the binary even when PATH lacks that directory."""
    bin_dir = minimal_env / ".npm-global" / "bin"
    _fake(bin_dir / "node", 'echo "node ok"')
    script = bin_dir / "openclaw"
    script.write_text("#!/usr/bin/env node\n")
    script.chmod(0o755)
    # `node` here is a shell script that ignores its argument and prints a banner, standing in for the interpreter.
    _fake(bin_dir / "node", 'echo "OpenClaw 2026.10.4 (abc1234)"')
    found = detection.detect_openclaw()
    assert found.installed and found.version == "2026.10.4"


def test_gemini_is_detected_for_the_info_row_but_hints_at_agy(minimal_env):
    found = detection.detect_gemini_cli()
    assert not found.installed and "agy" in found.install_hint and "npm" not in found.install_hint


def test_openclaw_reload_rules_locates_the_package_json_off_path(minimal_env):
    from ai_resources import openclaw_reload_rules as rr
    pkg = minimal_env / ".npm-global" / "lib" / "node_modules" / "openclaw"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "openclaw", "version": "2026.10.4"}')
    _fake(pkg / "openclaw.mjs", "true")
    (minimal_env / ".npm-global" / "bin").mkdir(parents=True)
    os.symlink(pkg / "openclaw.mjs", minimal_env / ".npm-global" / "bin" / "openclaw")
    assert rr.locate_package_json() == pkg / "package.json"
