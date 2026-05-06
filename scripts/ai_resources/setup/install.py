"""Install commands for missing cockpits and CLIs.

Each installer prints the exact command and asks for confirmation; never
runs silently.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import NamedTuple

from . import ui


class InstallResult(NamedTuple):
    success: bool
    message: str


def _run(cmd: list[str], timeout: int = 300) -> InstallResult:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return InstallResult(True, r.stdout.strip())
        return InstallResult(False, (r.stderr or r.stdout).strip())
    except subprocess.TimeoutExpired:
        return InstallResult(False, "timeout")
    except FileNotFoundError:
        return InstallResult(False, "command not found")


def install_aider() -> InstallResult:
    if not shutil.which("pipx"):
        return InstallResult(False, "pipx not installed (try: brew install pipx)")
    return _run(["pipx", "install", "aider-chat"])


def install_codex() -> InstallResult:
    if not shutil.which("npm"):
        return InstallResult(False, "npm not installed (try: brew install node)")
    return _run(["npm", "install", "-g", "@openai/codex"])


def install_gemini_cli() -> InstallResult:
    if not shutil.which("npm"):
        return InstallResult(False, "npm not installed (try: brew install node)")
    return _run(["npm", "install", "-g", "@google/gemini-cli"])


def install_opencode() -> InstallResult:
    if not shutil.which("npm"):
        return InstallResult(False, "npm not installed (try: brew install node)")
    return _run(["npm", "install", "-g", "opencode-ai"])


# Map cockpit id -> (display name, install function or None for manual)
INSTALLERS = {
    "aider":    ("Aider",          install_aider,       "pipx install aider-chat"),
    "codex":    ("Codex CLI",      install_codex,       "npm i -g @openai/codex"),
    "gemini":   ("Gemini CLI",     install_gemini_cli,  "npm i -g @google/gemini-cli"),
    "opencode": ("OpenCode",       install_opencode,    "npm i -g opencode-ai"),
    "claude":   ("Claude Code",    None,                "Visit https://docs.anthropic.com/claude/docs/claude-code"),
    "cursor":   ("Cursor",         None,                "Download from https://cursor.sh"),
    "windsurf": ("Windsurf",       None,                "Download from https://codeium.com/windsurf"),
    "continue": ("Continue.dev",   None,                "https://docs.continue.dev/getting-started"),
    "copilot":  ("GitHub Copilot", None,                "gh extension install github/gh-copilot"),
}


def offer_install(cockpit_id: str) -> bool:
    """Ask user if they want to install. Run if confirmed. Return success."""
    spec = INSTALLERS.get(cockpit_id)
    if not spec:
        return False
    name, fn, hint = spec

    if fn is None:
        ui.info(f"{name} requires manual installation:")
        ui.detail(hint)
        return False

    if not ui.confirm(f"Install {name}? Will run: {hint}", default=True):
        ui.detail(f"Skipped. To install later: {hint}")
        return False

    with ui.spinner(f"Installing {name}"):
        result = fn()
    if result.success:
        ui.ok(f"{name} installed")
        return True
    ui.error(f"{name} install failed: {result.message[:200]}")
    ui.detail(f"Try manually: {hint}")
    return False
