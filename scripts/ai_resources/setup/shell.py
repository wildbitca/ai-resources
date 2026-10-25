"""Multi-shell environment and credential synchronization for ai-resources.

Manages environment variable blocks in ~/.zshrc and ~/.bashrc, as well as the
OpenClaw service environment file (~/.openclaw/gateway.systemd.env), with atomic
writes, managed markers, and stale credential purges.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import ui

BEGIN_MARKER = "# BEGIN ai-resources env (managed by ai-resources setup; do not edit)"
END_MARKER = "# END ai-resources env"
_BLOCK_REGEX = re.compile(
    r"^[ \t]*# BEGIN ai-resources env.*?\n(.*?)\n[ \t]*# END ai-resources env",
    re.MULTILINE | re.DOTALL,
)

RC_CANDIDATES = ("~/.zshrc", "~/.bashrc")


def get_existing_rc_files() -> List[Path]:
    """Return all supported rc files that exist in the user's home directory."""
    existing = []
    for rc in RC_CANDIDATES:
        p = Path(rc).expanduser()
        if p.exists() and p.is_file():
            existing.append(p)
    # If neither exists, default to the user's current shell rc or .zshrc
    if not existing:
        default_rc = Path("~/.zshrc" if "zsh" in os.environ.get("SHELL", "") else "~/.bashrc").expanduser()
        existing.append(default_rc)
    return existing


def render_shell_block(env_vars: Dict[str, str]) -> str:
    """Render the managed export block for shell rc files."""
    lines = [BEGIN_MARKER]
    for key in sorted(env_vars.keys()):
        val = env_vars[key]
        # Escape double quotes and backslashes for bash/zsh export
        safe_val = val.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'export {key}="{safe_val}"')
    lines.append(END_MARKER)
    return "\n".join(lines)


def planned_content(content: str, env_vars: Dict[str, str]) -> str:
    """The whole rc text after the managed block is written into `content`: everything else is kept as is."""
    rendered = render_shell_block(env_vars)
    if not env_vars:
        # If env_vars is empty, remove the block if present
        return _BLOCK_REGEX.sub("", content).strip() + "\n" if _BLOCK_REGEX.search(content) else content
    if _BLOCK_REGEX.search(content):
        return _BLOCK_REGEX.sub(rendered, content)            # replace the existing block
    nl = "\n" if content and not content.endswith("\n") else ""
    return content + nl + rendered + "\n"                   # append the block


def write_shell_managed_block(rc_path: Path, env_vars: Dict[str, str], *, dry_run: bool = False) -> bool:
    """Write or replace the managed block in `rc_path`. Returns True if changed."""
    rc_path = rc_path.expanduser()
    content = ""
    if rc_path.exists():
        try:
            content = rc_path.read_text(encoding="utf-8")
        except OSError as e:
            ui.warn(f"Could not read {rc_path}: {e}")
            return False

    new_content = planned_content(content, env_vars)
    if new_content == content:
        return False
    if dry_run:
        return True

    # Atomic write preserving permissions
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(rc_path.stat().st_mode) if rc_path.exists() else 0o644
    except OSError:
        mode = 0o644

    fd, tmp = tempfile.mkstemp(prefix=f".{rc_path.name}.", suffix=".tmp", dir=rc_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.chmod(tmp, mode)
        os.replace(tmp, rc_path)
    except Exception as e:
        ui.error(f"Failed to write {rc_path}: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False

    return True


def sync_all_shell_envs(env_vars: Dict[str, str], *, dry_run: bool = False) -> List[Path]:
    """Sync environment variables across existing rc files (.zshrc, .bashrc)."""
    updated = []
    for rc in get_existing_rc_files():
        if write_shell_managed_block(rc, env_vars, dry_run=dry_run):
            updated.append(rc)
    return updated


def sync_openclaw_systemd_env(
    env_vars: Dict[str, str],
    *,
    purge_keys: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Optional[Path]:
    """Sync model and runtime variables into ~/.openclaw/gateway.systemd.env."""
    env_path = Path("~/.openclaw/gateway.systemd.env").expanduser()
    if not env_path.parent.exists():
        return None

    current: Dict[str, str] = {}
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    current[k.strip()] = v.strip().strip('"').strip("'")
        except OSError as e:
            ui.warn(f"Could not read {env_path}: {e}")
            return None

    # Apply purges
    if purge_keys:
        for pk in purge_keys:
            current.pop(pk, None)

    # Apply updates
    current.update(env_vars)

    lines = [f"{k}={v}" for k, v in sorted(current.items())]
    content = "\n".join(lines) + "\n"

    if dry_run:
        return env_path

    fd, tmp = tempfile.mkstemp(prefix=".gateway.systemd.env.", suffix=".tmp", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except Exception as e:
        ui.error(f"Failed to write {env_path}: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None

    return env_path


def purge_stale_credentials(strategy: str, *, dry_run: bool = False) -> List[str]:
    """Purge conflicting or obsolete tokens depending on the chosen strategy.

    It never touches the Antigravity OAuth token (~/.gemini/antigravity-cli/antigravity-oauth-token).
    Every cockpit owns its own sign-in, and agy keeps its session in that file when it runs over SSH
    or without a secret service. Deleting it here signed agy out on every setup run, which broke the
    agy lanes until the operator logged in again (2026-10-25).
    """
    purged: List[str] = []

    # If strategy is purely Vertex, remove ANTHROPIC_API_KEY from shell env to let ADC / WIF take precedence
    if strategy == "vertex":
        purged.append("ANTHROPIC_API_KEY (purged from shell/openclaw env in favor of Vertex ADC)")

    return purged
