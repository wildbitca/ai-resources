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
