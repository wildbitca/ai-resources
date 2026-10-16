"""Detect installed cockpits, container runtimes, and other tooling."""
from __future__ import annotations

import json
import os
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
    variant: str = ""           # "gh-extension" for the legacy `gh copilot` extension; "" otherwise


def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str]:
    env = None
    if cmd and os.path.isabs(cmd[0]):
        # An npm-installed CLI starts with `#!/usr/bin/env node`; `node` lives beside it, but a service unit's
        # minimal PATH does not list that directory, so the probe would fail on a tool that is installed.
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([os.path.dirname(cmd[0]), env.get("PATH", "")]).rstrip(os.pathsep)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1, ""


def _which(binary: str) -> str:
    return shutil.which(binary) or ""


def _version_from(output: str, pattern: str = r"(\d+\.\d+(?:\.\d+)?)") -> str:
    m = re.search(pattern, output)
    return m.group(1) if m else ""


_SYSTEM_BIN_DIRS: tuple[Path, ...] = (Path("/home/linuxbrew/.linuxbrew/bin"), Path("/opt/homebrew/bin"),
                                      Path("/usr/local/bin"))


def _extra_bin_dirs() -> list[Path]:
    """Directories a login shell would have on PATH but a systemd/launchd unit or a minimal PATH might not.

    Homebrew (Linuxbrew, Apple Silicon, Intel, `$HOMEBREW_PREFIX`), `~/.local/bin` (the official claude, agy and
    cursor installers), the npm/pnpm/bun/cargo global bins, and the active fnm Node's bin dir
    (~/.local/share/fnm/node-versions/<ver>/installation/bin).
    """
    home = Path.home()
    dirs: list[Path] = []
    prefix = os.environ.get("HOMEBREW_PREFIX", "").strip()
    if prefix:
        dirs.append(Path(prefix) / "bin")
    dirs += [*_SYSTEM_BIN_DIRS, home / ".local" / "bin"]
    npm_prefix = (os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix") or "").strip()
    if npm_prefix:
        dirs.append(Path(npm_prefix) / "bin")
    dirs += [home / ".npm-global" / "bin", home / ".local" / "share" / "pnpm", home / ".cargo" / "bin",
             home / ".bun" / "bin"]
    fnm_root = home / ".local" / "share" / "fnm" / "node-versions"
    if fnm_root.is_dir():
        for version_dir in sorted(fnm_root.iterdir(), reverse=True):
            bin_dir = version_dir / "installation" / "bin"
            if bin_dir.is_dir():
                dirs.append(bin_dir)
    seen: set[Path] = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def _which_extra(binary: str) -> str:
    """Like `_which`, but also checks common dirs a service unit's PATH might miss.

    Returns an absolute path either way (PATH hits from `shutil.which` already are).
    """
    found = _which(binary)
    if found:
        return found
    for d in _extra_bin_dirs():
        candidate = d / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def _probe(label: str, binary: str, hint: str, *, timeout: int = 5, pattern: str | None = None) -> Detected:
    """Resolve `binary` (PATH, then the extra dirs) and ask it for its version by absolute path.

    A binary that exists but does not answer `--version` (non-zero exit, timeout, a shebang interpreter off PATH) is
    still INSTALLED, with an unknown version: the tool is there, only the probe failed.
    """
    p = _which_extra(binary)
    if not p:
        return Detected(label, False, "", "", hint)
    rc, out = _run([p, "--version"], timeout=timeout)
    return Detected(label, True, _version_from(out, pattern) if pattern else _version_from(out), p)


# --- cockpits ------------------------------------------------------------------
def detect_claude_code() -> Detected:
    return _probe("Claude Code", "claude", "https://docs.anthropic.com/claude/docs/claude-code (downloads page)")


def detect_cursor_agent() -> Detected:
    """Cursor's terminal agent (`cursor-agent`, also linked as `agent`); the installer drops it in ~/.local/bin."""
    return _probe("Cursor Agent CLI", "cursor-agent", "curl https://cursor.com/install -fsS | bash")


def detect_cursor() -> Detected:
    # Cursor is a GUI app on macOS; check for its install path. The terminal agent counts as Cursor too.
    agent = detect_cursor_agent()
    if agent.installed:
        return Detected("Cursor", True, agent.version, agent.binary_path)
    p = _which_extra("cursor")
    macapp = Path("/Applications/Cursor.app")
    if not p and macapp.exists():
        return Detected("Cursor", True, "", str(macapp))
    if not p:
        return Detected("Cursor", False, "", "", "https://cursor.sh (download installer)")
    rc, out = _run([p, "--version"])
    return Detected("Cursor", True, _version_from(out), p)


def detect_gemini_cli() -> Detected:
    """Gemini CLI is RETIRED for personal accounts (2026-06-18; replaced by agy). Detected only so an existing
    install can be shown as an informational row; it is never offered for installation."""
    return _probe("Gemini CLI", "gemini", "retired for personal accounts: use the Antigravity CLI (agy)")


def detect_codex() -> Detected:
    return _probe("Codex CLI", "codex", "npm i -g @openai/codex")


def detect_aider() -> Detected:
    return _probe("Aider", "aider", "curl -LsSf https://aider.chat/install.sh | sh")


def detect_copilot() -> Detected:
    # GitHub Copilot CLI: the standalone `copilot` binary (npm @github/copilot) or the older `gh copilot` extension
    cp = _which_extra("copilot")
    if cp:
        rc, out = _run([cp, "--version"])
        if rc == 0:
            return Detected("GitHub Copilot CLI", True, _version_from(out), cp)
    p = _which_extra("gh")
    if not p:
        if cp:
            return Detected("GitHub Copilot CLI", True, "", cp)
        return Detected("GitHub Copilot CLI", False, "", "",
                        "npm install -g @github/copilot")
    rc, out = _run([p, "copilot", "--version"])
    if rc != 0:
        if cp:
            return Detected("GitHub Copilot CLI", True, "", cp)
        return Detected("GitHub Copilot CLI", False, "", p,
                        "npm install -g @github/copilot")
    return Detected("GitHub Copilot CLI", True, _version_from(out), p, variant="gh-extension")


def detect_windsurf() -> Detected:
    macapp = Path("/Applications/Windsurf.app")
    if macapp.exists():
        return Detected("Windsurf", True, "", str(macapp))
    return Detected("Windsurf", False, "", "",
                    "https://codeium.com/windsurf (download installer)")


def detect_continue() -> Detected:
    return _probe("Continue.dev CLI", "cn", "npm i -g @continuedev/cli")


def detect_opencode() -> Detected:
    return _probe("OpenCode", "opencode", "npm i -g opencode-ai")


def detect_openclaw() -> Detected:
    # `--version` prints "OpenClaw 2026.10.4 (383c124)"; a slow start is normal.
    return _probe("OpenClaw", "openclaw", "curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard",
                  timeout=30, pattern=r"(\d+\.\d+\.\d+)")


def detect_agy() -> Detected:
    """Detect the Antigravity CLI (agy), the antigravity OpenClaw engine's orchestrator."""
    return _probe("Antigravity CLI (agy)", "agy", "curl -fsSL https://antigravity.google/cli/install.sh | bash")


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
        "openclaw": detect_openclaw(),
        "agy":      detect_agy(),
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


def detect_compose(cli: str = "docker") -> bool:
    """Whether `cli` has compose available, as a subcommand or a standalone.

    Parameterised because podman ships its own compose; probing `docker compose`
    on a podman host answered for the wrong binary, so the check was skipped for
    every runtime but docker and podman users met the failure at apply time.
    """
    rc, _ = _run([cli, "compose", "version"])
    if rc == 0:
        return True
    rc, _ = _run([f"{cli}-compose", "--version"])
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
