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
    monkeypatch.setattr(openclaw_cockpit, "_gateway_main_pid", lambda: "")
    # And the start-time read behind `plugin_is_stale`'s time rule: a tmp plugin dir is always newer
    # than the live gateway, so an unstubbed call would make every staleness test report stale.
    monkeypatch.setattr(openclaw_cockpit, "_gateway_started_at", lambda: None)
