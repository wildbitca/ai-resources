"""Credential storage in ~/.config/ai-resources/.env (chmod 600).

Provides:
- read existing keys (mask for display)
- write/update single keys
- atomic file replacement
- secret_ref() for safe rendering in templates (uses ${env:VAR})
"""
from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path

from . import state


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def _write_env_file(path: Path, kv: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [
        f"# ai-resources credentials — managed file, chmod 600",
        f"# Generated: {datetime.now(timezone.utc).isoformat()}",
        f"# DO NOT commit this file. Add to .gitignore.",
        "",
    ]
    for k in sorted(kv):
        v = kv[k].replace("\n", "")
        body.append(f"{k}={v}")
    body.append("")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(body), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_env() -> dict[str, str]:
    return _read_env_file(state.env_path())


def save_env(kv: dict[str, str]) -> Path:
    """Write env file with chmod 600, return path."""
    path = state.env_path()
    _write_env_file(path, kv)
    return path


def update_env(updates: dict[str, str]) -> Path:
    """Merge updates into existing env file, write back."""
    current = load_env()
    current.update(updates)
    return save_env(current)


def update_env_tracked(updates: dict[str, str]) -> tuple[Path, list[str]]:
    """Merge updates and return (env_path, newly_added_keys).

    A key is "newly added" only if it wasn't present before — letting callers
    record exactly what the wizard introduced (vs pre-existing user values).
    """
    current = load_env()
    newly_added = [k for k in updates if k not in current]
    current.update(updates)
    return save_env(current), newly_added


def remove_keys(keys: list[str]) -> tuple[Path, list[str]]:
    """Delete the given keys from .env (if present). Returns (env_path, keys_removed)."""
    current = load_env()
    removed = [k for k in keys if k in current]
    for k in removed:
        current.pop(k, None)
    return save_env(current), removed


def mask(value: str, visible: int = 4) -> str:
    if not value:
        return "(empty)"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}{'•' * (len(value) - visible * 2)}{value[-visible:]}"


def has_key(key: str) -> bool:
    return bool(load_env().get(key))


def get_key(key: str) -> str:
    return load_env().get(key, "")


def generate_master_key(prefix: str = "sk-litellm-master-") -> str:
    """Generate a high-entropy master key for LiteLLM."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(40))
    return f"{prefix}{body}"


def get_claude_code_keychain_key() -> str:
    """Return the Anthropic API key Claude Code stores in the macOS Keychain.

    Claude Code reads its API key from the "Claude Code" Keychain service
    and sends it as x-api-key, ignoring ANTHROPIC_API_KEY in settings.json env
    block. The gateway master key must match this key for authentication to work.

    Returns the key string, or "" if not macOS / not found / not an Anthropic key.
    """
    import platform
    if platform.system() != "Darwin":
        return ""
    try:
        import subprocess
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            key = r.stdout.strip()
            if key.startswith("sk-ant-"):
                return key
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def write_claude_code_keychain_key(key: str, account: str = "") -> bool:
    """Write an API key to the 'Claude Code' Keychain service (macOS only).

    Used to restore the key after `claude logout` clears the Keychain entry so
    Claude Code starts in API key mode instead of prompting for OAuth login.
    Returns True on success.
    """
    import platform, subprocess
    if platform.system() != "Darwin":
        return False
    if not account:
        account = Path.home().name
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", "Claude Code", "-a", account, "-w", key],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def secret_ref(env_var: str) -> str:
    """Return the placeholder used in YAML configs to reference an env var."""
    return f"os.environ/{env_var}"


PROVIDER_KEYS: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "google":    ["GEMINI_API_KEY"],
    "openai":    ["OPENAI_API_KEY"],
    "vertex":    ["GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"],
}
