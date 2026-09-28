"""Per-cockpit configurators.

Each module exposes:
    NAME: str            — display name
    BIN: list[str]       — binaries to detect via `which`
    CONFIG_ROOT: Path    — typical user config directory
    detect() -> dict     — installation detection
    configure(ctx)       — apply multi-model setup using context.SetupContext

A module MAY also expose:
    verify(ctx) -> list[ai_resources.verify.Finding]
                         — read-only check of what configure() left behind; ctx is {"state": ...}
                           plus optional extras. It must not write, must not touch the network and
                           must not import this package at module level. A cockpit without it gets
                           the generic check of `ai_resources.verify`, which only judges what the
                           state file recorded. `Finding.level` is `error` only for a genuinely
                           broken state; everything advisory is `warn`.
"""
from __future__ import annotations

from . import claude, gemini, cursor, codex, aider, copilot, windsurf, continue_dev, opencode, openclaw

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
    # Last on purpose: its engines reuse what the coding-CLI cockpits above install.
    "openclaw": openclaw,
}
