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


@pytest.fixture(autouse=True)
def _isolate_claude_settings(tmp_path_factory, monkeypatch):
    try:
        from ai_resources.setup.cockpits import claude
    except Exception:  # a test that does not import the package needs no isolation
        return
    root = tmp_path_factory.mktemp("claude-home")
    monkeypatch.setattr(claude, "CONFIG_ROOT", root, raising=False)
    monkeypatch.setattr(claude, "SETTINGS_PATH", root / "settings.json", raising=False)
    # The same for ~/.openclaw: the host section writes kit-host.env and the drained doctor
    # creates watchdog.off, and both paths are resolved from the real HOME at import time.
    from ai_resources import openclaw_host
    monkeypatch.setattr(openclaw_host, "HOST_ENV_PATH", root / "openclaw" / "kit-host.env", raising=False)
    monkeypatch.setattr(openclaw_host, "WATCHDOG_OFF", root / "openclaw" / "watchdog.off", raising=False)
    # ADR-0004: the graceful restart's state, lock and snapshots live under ~/.openclaw too.
    monkeypatch.setattr(openclaw_host, "STATE_PATH", root / "openclaw" / "health-restart.state", raising=False)
    monkeypatch.setattr(openclaw_host, "LOCK_PATH", root / "openclaw" / "health-restart.lock", raising=False)
    monkeypatch.setattr(openclaw_host, "SNAPSHOT_ROOT", root / "openclaw" / "logs" / "restart-snapshots",
                        raising=False)
    # And the OpenClaw cockpit's own root. A teardown test whose config names no
    # `agents.defaults.workspace` resolves the workspace to `CONFIG_ROOT / "workspace"`, which was
    # the operator's real ~/.openclaw/workspace: its teardown removed the kit block from the live
    # main AGENTS.md on every test run, and that was the "block missing from main" regression
    # v1.9.7 reported as fixed (1.10.2).
    from ai_resources.setup.cockpits import openclaw as openclaw_cockpit
    monkeypatch.setattr(openclaw_cockpit, "CONFIG_ROOT", root / "openclaw-home", raising=False)
    # And the model pins overlay (~/.config/ai-resources/model-pins.json): a host overlay must never
    # leak into a test, and a test must never write the operator's real one.
    from ai_resources import model_pins
    monkeypatch.setattr(model_pins, "overlay_path", lambda: root / "model-pins.json")
    # And the restart guard's two systemd reads: an unstubbed `restart_gateway()` would ask the
    # live unit whether agent turns are running, and a busy host would defer a restart a test
    # expects to happen. The guard's own tests pass a fake probe explicitly.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_busy", lambda: (0, []))
    # And the restart-required gate (ADR-0003): it asks `openclaw --version` and the cgroup. Unstubbed, a fake
    # `_openclaw` would answer no version (so every key reads restart-required) and a real probe would read the
    # live gateway. test_openclaw_restart_gate.py sets both itself.
    from ai_resources import openclaw_reload_rules
    monkeypatch.setattr(openclaw_reload_rules, "installed_openclaw_version",
                        lambda runner, **kw: openclaw_reload_rules.PINNED_OPENCLAW_VERSION)
    # E3: the package.json lookup would read the operator's installed OpenClaw; a test never depends on it.
    monkeypatch.setattr(openclaw_reload_rules, "_find_package_json", lambda: None)
    from ai_resources.setup.cockpits import _openclaw_host as host_section
    monkeypatch.setattr(host_section, "_in_flight", lambda: (0, "0"))
    monkeypatch.setattr(openclaw_cockpit, "_live_reload_mode", lambda: None)
    monkeypatch.setattr(openclaw_cockpit, "_gateway_main_pid", lambda: "")
    # And the start-time read behind `plugin_is_stale`'s time rule: a tmp plugin dir is always newer
    # than the live gateway, so an unstubbed call would make every staleness test report stale.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_started_at", lambda: None)
