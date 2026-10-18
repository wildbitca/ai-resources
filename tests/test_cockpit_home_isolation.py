"""The Claude cockpit's path constants never point a test at the operator's real ~/.claude (managed-block leak)."""
from __future__ import annotations

from pathlib import Path

from ai_resources.setup.cockpits import claude


def test_claude_paths_are_rerooted_during_a_test():
    real = Path.home() / ".claude"
    for name in ("CONFIG_ROOT", "SETTINGS_PATH", "CLAUDE_MD_PATH", "AGENTS_DIR", "WORKFLOWS_DIR"):
        value = getattr(claude, name)
        assert value != real and real not in value.parents, f"{name}={value}"
