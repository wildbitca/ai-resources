"""Detect installed cockpits, container runtimes, and other tooling."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple


class Detected(NamedTuple):
    name: str
    installed: bool
    version: str
    binary_path: str
    install_hint: str = ""


def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1, ""


def _which(binary: str) -> str:
    return shutil.which(binary) or ""


def _version_from(output: str, pattern: str = r"(\d+\.\d+(?:\.\d+)?)") -> str:
    m = re.search(pattern, output)
    return m.group(1) if m else ""


# --- cockpits ------------------------------------------------------------------
def detect_claude_code() -> Detected:
    p = _which("claude")
    if not p:
        return Detected("Claude Code", False, "", "",
                        "https://docs.anthropic.com/claude/docs/claude-code (downloads page)")
    rc, out = _run(["claude", "--version"])
    return Detected("Claude Code", rc == 0, _version_from(out), p)


def detect_cursor() -> Detected:
    # Cursor is a GUI app on macOS; check for its install path
    p = _which("cursor")
    macapp = Path("/Applications/Cursor.app")
    if not p and macapp.exists():
        return Detected("Cursor", True, "", str(macapp))
    if not p:
        return Detected("Cursor", False, "", "",
                        "https://cursor.sh (download installer)")
    rc, out = _run(["cursor", "--version"])
    return Detected("Cursor", rc == 0, _version_from(out), p)


def detect_gemini_cli() -> Detected:
    p = _which("gemini")
    if not p:
        return Detected("Gemini CLI", False, "", "",
                        "npm i -g @google/gemini-cli")
    rc, out = _run(["gemini", "--version"])
    return Detected("Gemini CLI", rc == 0, _version_from(out), p)


def detect_codex() -> Detected:
    p = _which("codex")
    if not p:
        return Detected("Codex CLI", False, "", "",
                        "npm i -g @openai/codex")
    rc, out = _run(["codex", "--version"])
    return Detected("Codex CLI", rc == 0, _version_from(out), p)


def detect_aider() -> Detected:
    p = _which("aider")
    if not p:
        return Detected("Aider", False, "", "",
                        "pipx install aider-chat")
    rc, out = _run(["aider", "--version"])
    return Detected("Aider", rc == 0, _version_from(out), p)


def detect_copilot() -> Detected:
    # GitHub Copilot CLI
    p = _which("gh")
    if not p:
        return Detected("GitHub Copilot CLI", False, "", "",
                        "brew install gh && gh extension install github/gh-copilot")
    rc, out = _run(["gh", "copilot", "--version"])
    if rc != 0:
        return Detected("GitHub Copilot CLI", False, "", p,
                        "gh extension install github/gh-copilot")
    return Detected("GitHub Copilot CLI", True, _version_from(out), p)


def detect_windsurf() -> Detected:
    macapp = Path("/Applications/Windsurf.app")
    if macapp.exists():
        return Detected("Windsurf", True, "", str(macapp))
    return Detected("Windsurf", False, "", "",
                    "https://codeium.com/windsurf (download installer)")


def detect_continue() -> Detected:
    p = _which("cn")
    if p:
        rc, out = _run(["cn", "--version"])
        return Detected("Continue.dev CLI", True, _version_from(out), p)
    return Detected("Continue.dev CLI", False, "", "",
                    "https://docs.continue.dev/getting-started")


def detect_opencode() -> Detected:
    p = _which("opencode")
    if not p:
        return Detected("OpenCode", False, "", "",
                        "npm i -g opencode-ai")
    rc, out = _run(["opencode", "--version"])
    return Detected("OpenCode", rc == 0, _version_from(out), p)


def detect_all_cockpits() -> dict[str, Detected]:
    return {
        "claude":   detect_claude_code(),
        "cursor":   detect_cursor(),
        "gemini":   detect_gemini_cli(),
        "codex":    detect_codex(),
        "aider":    detect_aider(),
        "copilot":  detect_copilot(),
        "windsurf": detect_windsurf(),
        "continue": detect_continue(),
        "opencode": detect_opencode(),
    }


# --- container runtimes --------------------------------------------------------
def detect_docker() -> Detected:
    p = _which("docker")
    if not p:
        return Detected("docker", False, "", "",
                        "https://docs.docker.com/get-docker/")
    rc, out = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if rc != 0:
        # Daemon not running but binary exists
        return Detected("docker", True, "", p,
                        "Daemon not running — start Docker Desktop or `colima start`")
    return Detected("docker", True, out.strip(), p)


def detect_podman() -> Detected:
    p = _which("podman")
    if not p:
        return Detected("podman", False, "", "")
    rc, out = _run(["podman", "version", "--format", "json"])
    ver = ""
    if rc == 0:
        try:
            ver = json.loads(out).get("Client", {}).get("Version", "")
        except json.JSONDecodeError:
            pass
    return Detected("podman", True, ver, p)


def detect_colima() -> Detected:
    p = _which("colima")
    if not p:
        return Detected("colima", False, "", "")
    rc, out = _run(["colima", "version"])
    return Detected("colima", rc == 0, _version_from(out), p)


def detect_container_runtime() -> tuple[str, Detected]:
    """Return (preferred_runtime_name, Detected). Picks first available."""
    docker = detect_docker()
    if docker.installed and docker.version:
        return ("docker", docker)
    podman = detect_podman()
    if podman.installed and podman.version:
        return ("podman", podman)
    colima = detect_colima()
    if colima.installed:
        return ("colima", colima)
    if docker.installed:  # binary present but daemon down
        return ("docker", docker)
    return ("", Detected("none", False, "", ""))


def detect_compose() -> bool:
    rc, _ = _run(["docker", "compose", "version"])
    if rc == 0:
        return True
    rc, _ = _run(["docker-compose", "--version"])
    return rc == 0


# --- other tooling --------------------------------------------------------------
def detect_pipx() -> Detected:
    p = _which("pipx")
    if not p:
        return Detected("pipx", False, "", "",
                        "brew install pipx (or python3 -m pip install --user pipx)")
    rc, out = _run(["pipx", "--version"])
    return Detected("pipx", rc == 0, out.strip(), p)


def detect_npm() -> Detected:
    p = _which("npm")
    if not p:
        return Detected("npm", False, "", "", "Install Node.js: brew install node")
    rc, out = _run(["npm", "--version"])
    return Detected("npm", rc == 0, out.strip(), p)


def detect_brew() -> Detected:
    p = _which("brew")
    if not p:
        return Detected("brew", False, "", "")
    rc, out = _run(["brew", "--version"])
    return Detected("brew", rc == 0, _version_from(out), p)


def detect_gcloud() -> Detected:
    p = _which("gcloud")
    if not p:
        return Detected("gcloud", False, "", "",
                        "https://cloud.google.com/sdk/docs/install")
    rc, out = _run(["gcloud", "--version"])
    return Detected("gcloud", rc == 0, _version_from(out), p)


def detect_litellm_binary() -> Detected:
    """Detect a working `litellm` command (installed via pipx, pip, or system)."""
    p = _which("litellm")
    if not p:
        return Detected("litellm", False, "", "",
                        "pipx install 'litellm[proxy]'")
    rc, out = _run([p, "--version"], timeout=10)
    return Detected("litellm", rc == 0, _version_from(out), p)


def detect_python() -> Detected:
    """Detect python3 binary for venv-based install."""
    import sys
    p = _which("python3") or sys.executable
    if not p:
        return Detected("python3", False, "", "")
    rc, out = _run([p, "--version"])
    return Detected("python3", rc == 0, _version_from(out), p)


def detect_launchctl() -> Detected:
    """macOS launchctl for service management."""
    p = _which("launchctl")
    if not p:
        return Detected("launchctl", False, "", "")
    return Detected("launchctl", True, "", p)


def detect_systemctl_user() -> Detected:
    """Linux systemctl --user for service management."""
    p = _which("systemctl")
    if not p:
        return Detected("systemctl", False, "", "")
    rc, _ = _run(["systemctl", "--user", "is-system-running"])
    return Detected("systemctl", True, "", p)
