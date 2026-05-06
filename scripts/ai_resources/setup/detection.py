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


def find_compatible_python(min_minor: int = 10, max_minor: int = 13) -> Detected:
    """Find a Python interpreter in the supported version range (3.10-3.13).

    LiteLLM's transitive deps (orjson via pyo3, etc.) currently support up to
    Python 3.13. Newer interpreters (3.14+) fail at build time. This function
    walks common locations and returns the highest minor version in range.

    Returns Detected(installed=False, ...) if nothing in range is found.
    """
    from pathlib import Path

    # Try in descending order — prefer newest compatible
    seen_paths: set[str] = set()
    candidates: list[str] = []
    for minor in range(max_minor, min_minor - 1, -1):
        # PATH lookups first
        candidates.append(f"python3.{minor}")
        # Common absolute locations
        candidates.extend([
            f"/opt/homebrew/bin/python3.{minor}",
            f"/opt/homebrew/opt/python@3.{minor}/bin/python3.{minor}",
            f"/usr/local/bin/python3.{minor}",
            f"/usr/local/opt/python@3.{minor}/bin/python3.{minor}",
            f"/Library/Frameworks/Python.framework/Versions/3.{minor}/bin/python3.{minor}",
            f"/usr/bin/python3.{minor}",
        ])

    for cmd in candidates:
        if cmd.startswith("/"):
            if cmd in seen_paths:
                continue
            seen_paths.add(cmd)
            if not Path(cmd).is_file():
                continue
            path = cmd
        else:
            resolved = _which(cmd)
            if not resolved or resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            path = resolved

        rc, out = _run([path, "--version"], timeout=5)
        if rc != 0:
            continue
        ver = _version_from(out)
        if not ver:
            continue
        try:
            parts = [int(x) for x in ver.split(".")[:2]]
        except ValueError:
            continue
        if len(parts) >= 2 and parts[0] == 3 and min_minor <= parts[1] <= max_minor:
            return Detected("python-compatible", True, ver, path)

    return Detected("python-compatible", False, "", "",
                    "brew install python@3.12  (or any python 3.10-3.13)")


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
