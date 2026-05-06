"""Per-cockpit configurators.

Each module exposes:
    NAME: str            — display name
    BIN: list[str]       — binaries to detect via `which`
    CONFIG_ROOT: Path    — typical user config directory
    detect() -> dict     — installation detection
    configure(ctx)       — apply multi-model setup using context.SetupContext
"""
from __future__ import annotations

from . import claude, gemini, cursor, codex, aider, copilot, windsurf, continue_dev, opencode

ALL = {
    "claude": claude,
    "gemini": gemini,
    "cursor": cursor,
    "codex": codex,
    "aider": aider,
    "copilot": copilot,
    "windsurf": windsurf,
    "continue": continue_dev,
    "opencode": opencode,
}
