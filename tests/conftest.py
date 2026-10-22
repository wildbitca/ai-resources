"""Keep the suite away from the operator's real ~/.claude.

The kit writes hook registrations into ~/.claude/settings.json. A test that reaches that file with a
fake kit path (the suite uses "/kit") leaves a hook pointing at a file that does not exist, and a
UserPromptSubmit hook that fails blocks every prompt in every Claude Code session on the host.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Before the tmpdir plugin reads the option: a bare run must not fill the /tmp tmpfs.
    from basetemp_default import apply_default
    apply_default(config)


@pytest.fixture(autouse=True)
def _isolate_claude_settings(tmp_path_factory, monkeypatch):
    try:
        from ai_resources.setup.cockpits import claude
    except Exception:  # a test that does not import the package needs no isolation
        return
    root = tmp_path_factory.mktemp("claude-home")
    # The setup state (and its lock file) resolve through XDG_CONFIG_HOME: a test that does not set it itself
    # must never reach the operator's real ~/.config/ai-resources.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-config"))
    monkeypatch.setattr(claude, "CONFIG_ROOT", root, raising=False)
    monkeypatch.setattr(claude, "SETTINGS_PATH", root / "settings.json", raising=False)
    # CLAUDE_MD_PATH, AGENTS_DIR and WORKFLOWS_DIR are also computed from the real HOME at import time, so a test that
    # called `claude.configure()` rewrote the operator's real ~/.claude/CLAUDE.md managed block on every full run.
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", root / "CLAUDE.md", raising=False)
    monkeypatch.setattr(claude, "AGENTS_DIR", root / "agents", raising=False)
    monkeypatch.setattr(claude, "WORKFLOWS_DIR", root / "workflows", raising=False)
    # The same for ~/.openclaw: the host section writes kit-host.env and the drained doctor
    # creates watchdog.off, and both paths are resolved from the real HOME at import time.
    from ai_resources import openclaw_host
    monkeypatch.setattr(openclaw_host, "HOST_ENV_PATH", root / "openclaw" / "kit-host.env", raising=False)
    monkeypatch.setattr(openclaw_host, "WATCHDOG_OFF", root / "openclaw" / "watchdog.off", raising=False)
    # ADR-0004: the graceful restart's state, lock and snapshots live under ~/.openclaw too.
    monkeypatch.setattr(openclaw_host, "STATE_PATH", root / "openclaw" / "health-restart.state", raising=False)
    monkeypatch.setattr(openclaw_host, "LOCK_PATH", root / "openclaw" / "health-restart.lock", raising=False)
    # Whether the test run itself sits in the gateway cgroup must not decide the outcome of a restart test:
    # the self-cgroup refusal (T29) reads this path, and the tests that exercise it pass their own file.
    monkeypatch.setattr(openclaw_host, "SELF_CGROUP_PATH", str(root / "openclaw" / "self-cgroup"), raising=False)
    monkeypatch.setattr(openclaw_host, "SNAPSHOT_ROOT", root / "openclaw" / "logs" / "restart-snapshots",
                        raising=False)
    # And the OpenClaw cockpit's own root. A teardown test whose config names no
    # `agents.defaults.workspace` resolves the workspace to `CONFIG_ROOT / "workspace"`, which was
    # the operator's real ~/.openclaw/workspace: its teardown removed the kit block from the live
    # main AGENTS.md on every test run, and that was the "block missing from main" regression
    # v1.9.7 reported as fixed (1.10.2).
    from ai_resources.setup.cockpits import openclaw as openclaw_cockpit
    monkeypatch.setattr(openclaw_cockpit, "CONFIG_ROOT", root / "openclaw-home", raising=False)
    # The kit's trim of the Engram plugin hook rewrites a file inside ~/.claude/plugins: a test that calls
    # `claude.configure()` must never reach the operator's real plugin directory. Tests of the trim pass `home`.
    # `claude.configure()` also runs `claude plugin marketplace add` and `claude plugin install engram` through the host's
    # real `claude` when both binaries exist (a network call and a write into ~/.claude/plugins): no test may do that.
    # A test that exercises the installer itself passes its own stub.
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False, raising=False)
    from ai_resources.setup import engram_patch
    _real_plugin_dirs = engram_patch.plugin_dirs
    monkeypatch.setattr(engram_patch, "plugin_dirs",
                        lambda home=None: _real_plugin_dirs(home) if home is not None else [])
    # And the model pins overlay (~/.config/ai-resources/model-pins.json): a host overlay must never
    # leak into a test, and a test must never write the operator's real one.
    from ai_resources import agy_gate_evidence, model_pins
    monkeypatch.setattr(model_pins, "overlay_path", lambda: root / "model-pins.json")
    monkeypatch.setattr(agy_gate_evidence, "HOST_OVERLAY_ENABLED", False)
    # And the restart guard's two systemd reads: an unstubbed `restart_gateway()` would ask the
    # live unit whether agent turns are running, and a busy host would defer a restart a test
    # expects to happen. The guard's own tests pass a fake probe explicitly.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_busy", lambda: (0, []))
    # And the restart-required gate (ADR-0003): it asks `openclaw --version` and the cgroup. Unstubbed, a fake
    # `_openclaw` would answer no version (so every key reads restart-required) and a real probe would read the
    # live gateway. test_openclaw_restart_gate.py sets both itself.
    from ai_resources import openclaw_reload_rules
    from reload_rules_fixture import FIXTURE_VERSION, fixture_ruleset
    monkeypatch.setattr(openclaw_reload_rules, "installed_openclaw_version",
                        lambda runner, **kw: FIXTURE_VERSION)
    # The rules come from a checked-in dist excerpt, never from the host's OpenClaw (C5). A test that needs the
    # "cannot read the rules" case overrides this; the version is whatever `installed_openclaw_version` says now.
    monkeypatch.setattr(
        openclaw_reload_rules, "current_ruleset",
        lambda runner, **kw: fixture_ruleset(version=openclaw_reload_rules.installed_openclaw_version(runner, **kw)))
    # E3: the package.json lookup would read the operator's installed OpenClaw; a test never depends on it.
    monkeypatch.setattr(openclaw_reload_rules, "_find_package_json", lambda: None)
    from ai_resources.setup.cockpits import _openclaw_host as host_section
    monkeypatch.setattr(host_section, "_in_flight", lambda: (0, "0"))
    monkeypatch.setattr(openclaw_cockpit, "_live_reload_mode", lambda: None)
    monkeypatch.setattr(openclaw_cockpit, "_gateway_main_pid", lambda: "")
    # The image question lists which providers OpenClaw can already sign in to by running `openclaw models status
    # --json`; a test never runs the host's openclaw. The image tests pass their own accounts.
    monkeypatch.setattr(openclaw_cockpit, "_image_accounts", lambda: ({}, []))
    # And the start-time read behind `plugin_is_stale`'s time rule: a tmp plugin dir is always newer
    # than the live gateway, so an unstubbed call would make every staleness test report stale.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_started_at", lambda: None)


@pytest.fixture
def previous_generation_pins(monkeypatch):
    """Kit defaults pinned to the generation the recorded model fixtures were written against."""
    from pin_generation import pin_previous_generation
    pin_previous_generation(monkeypatch)


@pytest.fixture(autouse=True)
def _no_host_command_snapshots(monkeypatch):
    """`lanes` snapshots `crontab -l` and the user timer list: tests never run the host's own commands."""
    try:
        from ai_resources import lanes_cmd
    except Exception:
        return
    monkeypatch.setattr(lanes_cmd, "SNAPSHOT_RUNNER", lambda argv: (127, "", ""), raising=False)


EXTRA_TOOL_DETECTORS = (("detect_cursor_agent", "Cursor Agent CLI"), ("detect_gemini_cli", "Gemini CLI"),
                        ("detect_codex", "Codex CLI"), ("detect_aider", "Aider"),
                        ("detect_copilot", "GitHub Copilot CLI"), ("detect_continue", "Continue.dev CLI"),
                        ("detect_opencode", "OpenCode"), ("detect_windsurf", "Windsurf"))


@pytest.fixture
def extra_tools_present(monkeypatch):
    """The tool selector knows more CLIs than claude/agy/openclaw. Tests written for those three keep the other
    detectors off the host (no real `--version` subprocess) by reporting them installed, so they never change
    what is selectable. Tests about the extra CLIs set their own detectors after this fixture."""
    from ai_resources.setup import detection
    for fn, name in EXTRA_TOOL_DETECTORS:
        monkeypatch.setattr(detection, fn, lambda n=name: detection.Detected(n, True, "1.0", "/usr/bin/x"))


@pytest.fixture(autouse=True)
def _agy_not_installed_by_default(monkeypatch):
    """Whether the host has `agy` must not decide a wizard test: the Antigravity visibility rows (Custom list and
    preset tables) read `detection.agy_installed`, which looks the binary up and never runs it. A test that
    needs the installed case sets it itself."""
    try:
        from ai_resources.setup import detection
    except Exception:
        return
    monkeypatch.setattr(detection, "agy_installed", lambda: False, raising=False)


@pytest.fixture(autouse=True)
def _reset_assume_yes():
    """`wizard.run` stores `--yes` in ui._ASSUME_YES (and a test may set it directly): no test may leak it to the next."""
    yield
    try:
        from ai_resources.setup import ui
    except Exception:
        return
    ui._ASSUME_YES = False
    ui.set_assume_yes(False)


@pytest.fixture(autouse=True)
def _isolate_home_derived_paths(tmp_path_factory, monkeypatch):
    """Round 2 (ADR-0014): every reader or writer that resolves a path from the real HOME is pointed at a scratch
    directory before its first test: the billing reader (~/.claude.json, credits.json, the statusline cache)."""
    root = tmp_path_factory.mktemp("home-derived")
    # The cost snapshots (doctor's poll-share check reads the newest one) live under XDG_STATE_HOME.
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    try:
        from ai_resources import billing
    except Exception:
        return
    monkeypatch.setattr(billing, "claude_json_path", lambda: root / ".claude.json")
    monkeypatch.setattr(billing, "credits_path", lambda: root / "config" / "ai-resources" / "credits.json")
    monkeypatch.setattr(billing, "sidecar_path", lambda: root / "cache" / "claude-rate-limits.json")
    monkeypatch.setattr(billing, "sidecar_candidates", lambda: [root / "cache" / "claude-rate-limits.json"])
    from ai_resources import audit_snapshot
    monkeypatch.setattr(audit_snapshot, "openclaw_json_path", lambda home=None: (home or root / "home") / ".openclaw" / "openclaw.json")


@pytest.fixture(autouse=True)
def _isolate_usage_index(tmp_path_factory, monkeypatch):
    """The usage index reads every Claude Code transcript and caches under ~/.cache/ai-resources (ADR-0014 S5)."""
    root = tmp_path_factory.mktemp("usage-index")
    try:
        from ai_resources import usage_index
    except Exception:
        return
    monkeypatch.setattr(usage_index, "cache_path", lambda: root / "cache" / "ai-resources" / "usage-index.json")
    monkeypatch.setattr(usage_index, "default_roots", lambda: [])


@pytest.fixture(autouse=True)
def _isolate_hygiene(tmp_path_factory, monkeypatch):
    """The hygiene step runs the `claude` CLI and reads ~/.claude.json (ADR-0014 D3.2): a test never reaches either."""
    root = tmp_path_factory.mktemp("hygiene")
    try:
        from ai_resources.setup import hygiene
    except Exception:
        return
    monkeypatch.setattr(hygiene, "default_runner", lambda argv: (127, "claude is not run by tests"))
    monkeypatch.setattr(hygiene, "backup_dir", lambda: root / "hygiene")


@pytest.fixture(autouse=True)
def _isolate_gemini_home(tmp_path_factory, monkeypatch):
    """~/.gemini (agy's settings and MCP file) is resolved from the real HOME at import time and by `agy_mcp.mcp_path`:
    ADR-0014 D3.4 lets the kit write the MCP file, so no test may reach it."""
    root = tmp_path_factory.mktemp("gemini-home")
    try:
        from ai_resources.setup import agy_mcp
        from ai_resources.setup.cockpits import gemini
    except Exception:
        return
    monkeypatch.setattr(agy_mcp, "mcp_path", lambda: root / ".gemini" / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "CONFIG_ROOT", root / ".gemini", raising=False)
    monkeypatch.setattr(gemini, "SETTINGS_PATH", root / ".gemini" / "settings.json", raising=False)
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", root / ".gemini" / "config" / "mcp_config.json", raising=False)
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", root / ".gemini" / "GEMINI.md", raising=False)
