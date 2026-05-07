"""LiteLLM gateway — pipx-managed process via launchd (macOS) / systemd-user (Linux).

Default deployment: pipx install 'litellm[proxy]', auto-start via the platform's
native service manager. No Docker required.

Alternative modes (kept as escape hatches for users who prefer them):
  - pip-venv: dedicated venv at ~/.config/ai-resources/venv/
  - docker:   container via docker compose
  - remote:   apunte a un endpoint LiteLLM ya hospedado
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
    import httpx  # type: ignore
except ImportError:
    yaml = None  # type: ignore
    httpx = None  # type: ignore

from . import state, credentials, ui


SERVICE_LABEL = "com.ai-resources.litellm"
SERVICE_NAME = "ai-resources-litellm"
DEFAULT_LITELLM_PIP_SPEC = "litellm[proxy]"
DEFAULT_DOCKER_IMAGE = "ghcr.io/berriai/litellm:main-stable"


# ============================================================
# Path helpers
# ============================================================
def runtime_mode() -> str:
    return state.load().litellm.local.runtime or "pipx"


def litellm_binary() -> Path:
    """Resolve the litellm executable based on configured mode."""
    s = state.load()
    if s.litellm.local.binary_path:
        return Path(s.litellm.local.binary_path)
    p = shutil.which("litellm")
    return Path(p) if p else Path("litellm")


def log_dir() -> Path:
    d = state.config_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def venv_dir() -> Path:
    return state.config_root() / "venv"


def wrapper_script_path() -> Path:
    return state.config_root() / "bin" / "litellm-run"


# ============================================================
# Subprocess helper
# ============================================================
def _run(cmd: list[str], timeout: int = 60, check: bool = False,
         env: dict | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "command not found"


# ============================================================
# Config rendering (litellm.yaml — same regardless of runtime mode)
# ============================================================
# Claude models Claude Code uses internally (main session + background tasks).
# Added as passthrough entries whenever Anthropic is a configured provider so
# requests with these model names don't hit ProxyModelNotFoundError.
_CLAUDE_PASSTHROUGH_MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


def _render_litellm_yaml(executors: dict, providers: dict, master_key_env: str) -> str:
    """Build litellm.yaml content from executors.yaml + providers config.

    No master_key is set in general_settings: the gateway listens only on
    127.0.0.1 (localhost), so there is no external exposure and auth is
    unnecessary.  Claude Code in OAuth mode sends its session token rather than
    the API key — removing master_key avoids the key-mismatch 'db not found'
    error without requiring the user to logout.
    """
    model_list: list[dict] = []
    seen_models: set[str] = set()
    has_anthropic = providers.get("anthropic", {}).get("enabled", False)

    for role, cfg in executors.get("by_role", {}).items():
        model_name = cfg.get("model", "")
        provider = cfg.get("provider", "")
        if not model_name or model_name in seen_models:
            continue
        seen_models.add(model_name)

        litellm_params: dict = {}
        if provider == "anthropic":
            litellm_params = {
                "model": f"anthropic/{model_name}",
                "api_key": credentials.secret_ref("ANTHROPIC_API_KEY"),
            }
        elif provider == "google":
            litellm_params = {
                "model": f"gemini/{model_name}",
                "api_key": credentials.secret_ref("GEMINI_API_KEY"),
            }
        elif provider == "vertex":
            litellm_params = {
                "model": f"vertex_ai/{model_name}",
                "vertex_project": credentials.secret_ref("GOOGLE_CLOUD_PROJECT"),
                "vertex_location": credentials.secret_ref("GOOGLE_CLOUD_LOCATION"),
            }
        elif provider == "openai":
            litellm_params = {
                "model": f"openai/{model_name}",
                "api_key": credentials.secret_ref("OPENAI_API_KEY"),
            }
        elif provider == "ollama":
            litellm_params = {
                "model": f"ollama/{model_name}",
                "api_base": "http://127.0.0.1:11434",
            }
        else:
            continue

        model_list.append({
            "model_name": model_name,
            "litellm_params": litellm_params,
        })

    # Add passthrough entries for all current Claude models so Claude Code's
    # internal requests (haiku for background tasks, sonnet/opus for main)
    # always resolve even if the executor config only mentions some of them.
    if has_anthropic:
        for claude_model in _CLAUDE_PASSTHROUGH_MODELS:
            if claude_model not in seen_models:
                model_list.append({
                    "model_name": claude_model,
                    "litellm_params": {
                        "model": f"anthropic/{claude_model}",
                        "api_key": credentials.secret_ref("ANTHROPIC_API_KEY"),
                    },
                })
                seen_models.add(claude_model)

    fallback_map: dict[str, list[str]] = {}
    for role, cfg in executors.get("by_role", {}).items():
        fbs = cfg.get("fallbacks") or []
        if fbs and cfg.get("model"):
            fallback_map.setdefault(cfg["model"], list(fbs))
    fallbacks = [{m: fbs} for m, fbs in fallback_map.items()]

    doc = {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            "num_retries": 3,
            "timeout": 600,
            "fallbacks": fallbacks,
        },
        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False,
            "cache": True,
            "cache_params": {"type": "local", "ttl": 600},
        },
        "general_settings": {
            "master_key": credentials.secret_ref(master_key_env),
            # Claude Code in OAuth mode sends its session token instead of the
            # LITELLM_MASTER_KEY (the session token changes on every re-auth).
            # allow_requests_on_db_unavailable=true makes LiteLLM pass through
            # requests whose key doesn't match master_key and can't be looked up
            # in a DB (no DB is configured). The gateway is localhost-only so
            # this is safe — no external process can reach 127.0.0.1:4000.
            "allow_requests_on_db_unavailable": True,
        },
    }
    if yaml is None:
        raise RuntimeError("PyYAML required to render litellm.yaml")
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)


def write_configs(executors: dict, providers: dict,
                  master_key_env: str = "LITELLM_MASTER_KEY") -> dict:
    """Write litellm.yaml. Wrapper script + lifecycle written separately."""
    state.config_root().mkdir(parents=True, exist_ok=True)
    yaml_text = _render_litellm_yaml(executors, providers, master_key_env)
    state.litellm_path().write_text(yaml_text, encoding="utf-8")
    return {"litellm.yaml": state.litellm_path()}


# ============================================================
# Wrapper script — sources .env + execs litellm
# ============================================================
def _render_wrapper_script(litellm_bin: Path, port: int, host: str) -> str:
    return f"""#!/usr/bin/env bash
# ai-resources LiteLLM wrapper — sources .env then exec's litellm
# Generated by `ai-resources setup`. Do not edit by hand.
set -euo pipefail

ENV_FILE="{state.env_path()}"
CONFIG="{state.litellm_path()}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE — run: ai-resources setup" >&2
  exit 1
fi
if [ ! -f "$CONFIG" ]; then
  echo "Missing $CONFIG — run: ai-resources setup" >&2
  exit 1
fi

# Export every key=value pair in .env
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

exec "{litellm_bin}" --config "$CONFIG" --port {port} --host {host}
"""


def write_wrapper_script(litellm_bin: Path, port: int = 4000,
                         host: str = "127.0.0.1") -> Path:
    path = wrapper_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_wrapper_script(litellm_bin, port, host), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ============================================================
# pipx install
# ============================================================
def install_litellm_pipx(python_path: str = "") -> tuple[bool, str]:
    """Install LiteLLM via pipx. Idempotent (re-runs are safe).

    Args:
      python_path: explicit Python interpreter to use (e.g. /opt/homebrew/bin/python3.12).
                   Required when system default is 3.14+ (pyo3 maxes at 3.13).

    On failure, dumps the full pipx output to ~/.config/ai-resources/logs/install.log
    and returns the path in the error message so the user can investigate.
    """
    if not shutil.which("pipx"):
        return False, "pipx not installed (try: brew install pipx — or python3 -m pip install --user pipx && pipx ensurepath)"

    cmd = ["pipx", "install", DEFAULT_LITELLM_PIP_SPEC, "--force", "--verbose"]
    if python_path:
        cmd.extend(["--python", python_path])
    rc, out, err = _run(cmd, timeout=900)
    if rc != 0 and "already installed" not in (out + err).lower():
        # Persist the full output for debugging
        log_path = log_dir() / "install.log"
        log_path.write_text(
            f"=== pipx install '{DEFAULT_LITELLM_PIP_SPEC}' --force --verbose ===\n"
            f"return code: {rc}\n\n"
            f"--- stdout ---\n{out}\n\n"
            f"--- stderr ---\n{err}\n",
            encoding="utf-8",
        )
        # Try to extract the most useful line from the error
        useful = _extract_pip_error(err + "\n" + out)
        return False, f"{useful}\n\nFull log: {log_path}"
    bin_path = shutil.which("litellm") or str(Path.home() / ".local" / "bin" / "litellm")
    return True, bin_path


def _extract_pip_error(text: str) -> str:
    """Pull the most-actionable line out of a pip/pipx error blob."""
    if not text:
        return "(no output captured)"
    lines = text.splitlines()
    # Common failure markers
    markers = (
        "error: ", "ERROR: ", "Fatal error", "RuntimeError:",
        "Could not build wheels",
        "Failed building wheel",
        "Requires-Python",
        "No matching distribution",
        "metadata-generation-failed",
        "command 'clang' failed",
        "command 'gcc' failed",
        "Connection error",
        "could not be installed",
        "Permission denied",
    )
    matches = [ln.strip() for ln in lines if any(m in ln for m in markers)]
    if matches:
        # Show last 3 marker matches (most relevant)
        return "\n".join(matches[-3:])
    # Fallback: last 5 non-empty lines
    nonempty = [ln for ln in lines if ln.strip()]
    return "\n".join(nonempty[-5:])


def install_litellm_venv(python_path: str = "") -> tuple[bool, str]:
    """Create a dedicated venv at ~/.config/ai-resources/venv/ and pip install LiteLLM.

    Args:
      python_path: explicit Python interpreter to use for venv creation.
                   Required when system default is 3.14+ (litellm deps max at 3.13).

    On failure, dumps full pip output to ~/.config/ai-resources/logs/install.log.
    """
    import sys
    venv = venv_dir()
    py = python_path or sys.executable
    if not venv.exists():
        rc, out, err = _run([py, "-m", "venv", str(venv)], timeout=60)
        if rc != 0:
            return False, f"venv creation failed: {err or out}"

    pip = venv / "bin" / "pip"
    if not pip.is_file():
        return False, f"pip not found inside venv at {pip}"

    # Upgrade pip + install build essentials
    rc, out, err = _run(
        [str(pip), "install", "--upgrade", "pip", "setuptools", "wheel"],
        timeout=120,
    )
    if rc != 0:
        log_path = log_dir() / "install.log"
        log_path.write_text(f"=== pip upgrade ===\nrc={rc}\n\nstdout:\n{out}\n\nstderr:\n{err}\n",
                            encoding="utf-8")
        return False, f"pip upgrade failed.\n{_extract_pip_error(err+out)}\n\nFull log: {log_path}"

    # Install LiteLLM
    rc, out, err = _run(
        [str(pip), "install", "--verbose", DEFAULT_LITELLM_PIP_SPEC],
        timeout=900,
    )
    if rc != 0:
        log_path = log_dir() / "install.log"
        log_path.write_text(
            f"=== pip install '{DEFAULT_LITELLM_PIP_SPEC}' --verbose ===\nrc={rc}\n\n"
            f"stdout:\n{out}\n\nstderr:\n{err}\n",
            encoding="utf-8",
        )
        return False, f"{_extract_pip_error(err + out)}\n\nFull log: {log_path}"

    bin_path = venv / "bin" / "litellm"
    if not bin_path.is_file():
        return False, f"litellm not found at {bin_path} after install (unusual — check log)"
    return True, str(bin_path)


# ============================================================
# Lifecycle: launchd (macOS) / systemd-user (Linux)
# ============================================================
def is_macos() -> bool:
    return platform.system() == "Darwin"


def lifecycle_path() -> Path:
    if is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _render_launchd_plist(wrapper: Path) -> str:
    logs = log_dir()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{wrapper}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
    <key>Crashed</key>
    <true/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>{logs}/litellm.log</string>
  <key>StandardErrorPath</key>
  <string>{logs}/litellm.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:{Path.home()}/.local/bin</string>
  </dict>
</dict>
</plist>
"""


def _render_systemd_unit(wrapper: Path) -> str:
    return f"""[Unit]
Description=ai-resources LiteLLM gateway (pipx-managed)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={wrapper}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def install_lifecycle(wrapper: Path) -> Path:
    """Write platform-appropriate lifecycle file and load/enable it."""
    path = lifecycle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_macos():
        path.write_text(_render_launchd_plist(wrapper), encoding="utf-8")
        # Reload (unload then load — bootstrap is the modern way but bootstrap requires gui domain)
        _run(["launchctl", "unload", str(path)])
        rc, _, err = _run(["launchctl", "load", str(path)])
        if rc != 0 and "already loaded" not in err.lower():
            raise RuntimeError(f"launchctl load failed: {err}")
    else:
        path.write_text(_render_systemd_unit(wrapper), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        rc, _, err = _run(["systemctl", "--user", "enable", "--now", path.name])
        if rc != 0:
            raise RuntimeError(f"systemctl enable failed: {err}")
    return path


def uninstall_lifecycle() -> None:
    path = lifecycle_path()
    if not path.exists():
        return
    if is_macos():
        _run(["launchctl", "unload", str(path)])
    else:
        _run(["systemctl", "--user", "stop", path.name])
        _run(["systemctl", "--user", "disable", path.name])
    path.unlink(missing_ok=True)


# ============================================================
# Uninstall helpers — remove what we put in place
# ============================================================
def pipx_uninstall_litellm() -> tuple[bool, str]:
    """`pipx uninstall litellm` — best-effort, returns (ok, message)."""
    if not shutil.which("pipx"):
        return False, "pipx not on PATH"
    rc, out, err = _run(["pipx", "uninstall", "litellm"], timeout=120)
    return rc == 0, (err or out).strip()[:300]


def remove_venv() -> bool:
    """Delete the dedicated venv at ~/.config/ai-resources/venv/."""
    venv = venv_dir()
    if not venv.exists():
        return True
    try:
        shutil.rmtree(venv)
        return True
    except OSError:
        return False


def docker_compose_down(remove_image: bool = False) -> bool:
    """`docker compose down` — optionally also remove the image we pulled."""
    s = state.load()
    runtime = s.litellm.local.runtime if s.litellm.local.runtime in ("docker", "podman") else "docker"
    cmd = [runtime, "compose", "-f", str(state.compose_path()), "down"]
    if remove_image:
        cmd += ["--rmi", "local", "--volumes"]
    rc, _, _ = _run(cmd, timeout=120)
    return rc == 0


def remove_wrapper_script() -> bool:
    """Delete the wrapper script + its parent bin/ if empty."""
    path = wrapper_script_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            return False
    bin_dir = path.parent
    if bin_dir.is_dir():
        try:
            bin_dir.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return True


def remove_config_files(paths: list[str]) -> list[str]:
    """Delete each given config file path. Returns paths that were actually removed."""
    removed: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_file():
            try:
                path.unlink()
                removed.append(p)
            except OSError:
                pass
    return removed


def plan_multi_model_teardown(prev: state.SetupState) -> list[tuple[str, str]]:
    """Compute teardown actions from the previous state's tracking record.

    Returns list of (action_id, human_description) tuples. Pure — performs no I/O.
    Caller is expected to display these, get confirmation, then call execute_multi_model_teardown().
    """
    t = prev.tracking
    actions: list[tuple[str, str]] = []

    if t.lifecycle_installed and t.lifecycle_path_written:
        actions.append(("lifecycle",
                        f"Unload + remove background runner: {t.lifecycle_path_written}"))

    runtime = prev.litellm.local.runtime
    if runtime in ("docker", "podman"):
        actions.append(("docker_down", "Stop LiteLLM container (docker compose down)"))
        if t.docker_image_pulled_by_us and t.docker_image:
            actions.append(("docker_image",
                            f"Remove pulled image: {t.docker_image}"))
    if t.litellm_installed_by_us:
        if t.litellm_install_method == "pipx":
            actions.append(("pipx_uninstall", "Uninstall LiteLLM (pipx uninstall litellm)"))
        elif t.litellm_install_method == "pip-venv":
            actions.append(("remove_venv", f"Delete venv: {venv_dir()}"))

    if t.wrapper_path:
        actions.append(("wrapper", f"Delete wrapper script: {t.wrapper_path}"))

    if t.config_files_written:
        actions.append(("configs",
                        f"Delete {len(t.config_files_written)} generated config file(s)"))

    if t.env_keys_added:
        actions.append(("env_keys",
                        f"Remove {len(t.env_keys_added)} key(s) from .env: "
                        + ", ".join(t.env_keys_added)))

    if t.cockpit_env_keys_added:
        total = sum(len(v) for v in t.cockpit_env_keys_added.values())
        cockpits = ", ".join(t.cockpit_env_keys_added.keys())
        actions.append(("cockpit_env",
                        f"Remove {total} env key(s) from cockpit configs ({cockpits})"))

    return actions


def execute_multi_model_teardown(prev: state.SetupState) -> list[tuple[str, bool, str]]:
    """Perform all teardown actions from `plan_multi_model_teardown()`.

    Returns list of (action_id, ok, message) for each action attempted.
    Caller is responsible for displaying results and resetting/saving state.
    """
    t = prev.tracking
    results: list[tuple[str, bool, str]] = []
    runtime = prev.litellm.local.runtime

    # Order matters: stop runners first, then remove their backing artifacts.
    if runtime in ("docker", "podman"):
        ok = docker_compose_down(remove_image=t.docker_image_pulled_by_us)
        results.append(("docker_down", ok,
                        "container stopped" if ok else "compose down failed"))

    if t.lifecycle_installed and t.lifecycle_path_written:
        try:
            uninstall_lifecycle()
            results.append(("lifecycle", True, "background runner removed"))
        except (OSError, RuntimeError) as e:
            results.append(("lifecycle", False, str(e)))

    if t.litellm_installed_by_us:
        if t.litellm_install_method == "pipx":
            ok, msg = pipx_uninstall_litellm()
            results.append(("pipx_uninstall", ok, msg or "litellm uninstalled"))
        elif t.litellm_install_method == "pip-venv":
            ok = remove_venv()
            results.append(("remove_venv", ok,
                            f"deleted {venv_dir()}" if ok else "rmtree failed"))

    if t.wrapper_path:
        ok = remove_wrapper_script()
        results.append(("wrapper", ok,
                        f"deleted {t.wrapper_path}" if ok else "delete failed"))

    if t.config_files_written:
        removed = remove_config_files(t.config_files_written)
        results.append(("configs", True, f"removed {len(removed)} file(s)"))

    if t.env_keys_added:
        _, removed_keys = credentials.remove_keys(t.env_keys_added)
        results.append(("env_keys", True,
                        f"removed {len(removed_keys)} key(s) from .env"))

    # Per-cockpit settings.json env keys (e.g. ANTHROPIC_BASE_URL set by claude.py)
    if t.cockpit_env_keys_added:
        from .cockpits import ALL as _ALL_COCKPITS
        for cid, keys in t.cockpit_env_keys_added.items():
            mod = _ALL_COCKPITS.get(cid)
            if not mod or not hasattr(mod, "teardown"):
                continue
            try:
                removed = mod.teardown(keys)
                results.append((f"cockpit:{cid}", True,
                                f"removed {len(removed)} key(s) from {cid} settings"))
            except Exception as e:  # noqa: BLE001
                results.append((f"cockpit:{cid}", False, str(e)))

    return results


# ============================================================
# Service control (mode-aware: pipx/pip-venv via launchd/systemd, docker via compose)
# ============================================================
def start_service() -> bool:
    mode = runtime_mode()
    if mode in ("pipx", "pip-venv"):
        path = lifecycle_path()
        if not path.exists():
            ui.error(f"Lifecycle file missing: {path}. Re-run setup.")
            return False
        if is_macos():
            rc, _, err = _run(["launchctl", "load", str(path)])
            return rc == 0 or "already loaded" in err.lower()
        rc, _, _ = _run(["systemctl", "--user", "start", path.name])
        return rc == 0
    if mode == "docker":
        rc, _, _ = _run(["docker", "compose", "-f", str(state.compose_path()),
                         "up", "-d"], timeout=120)
        return rc == 0
    return False


def stop_service() -> bool:
    mode = runtime_mode()
    if mode in ("pipx", "pip-venv"):
        path = lifecycle_path()
        if is_macos():
            _run(["launchctl", "unload", str(path)])
            return True
        rc, _, _ = _run(["systemctl", "--user", "stop", path.name])
        return rc == 0
    if mode == "docker":
        rc, _, _ = _run(["docker", "compose", "-f", str(state.compose_path()),
                         "down"], timeout=60)
        return rc == 0
    return False


def restart_service() -> bool:
    if not stop_service():
        return False
    time.sleep(1)
    return start_service()


def service_status() -> str:
    """Return 'running' | 'stopped' | 'absent' | 'unhealthy'."""
    mode = runtime_mode()
    if mode in ("pipx", "pip-venv"):
        path = lifecycle_path()
        if not path.exists():
            return "absent"
        if is_macos():
            rc, out, _ = _run(["launchctl", "list", SERVICE_LABEL])
            if rc != 0:
                return "stopped"
            # Output format: PID Status Label
            parts = out.splitlines()[0].split() if out else []
            if parts and parts[0] != "-" and parts[0].isdigit():
                return "running"
            return "stopped"
        rc, out, _ = _run(["systemctl", "--user", "is-active", path.name])
        s = out.strip()
        if s == "active":
            return "running"
        if s == "inactive":
            return "stopped"
        return s or "absent"
    if mode == "docker":
        rc, out, _ = _run(["docker", "inspect", "--format",
                           "{{.State.Status}}", "ai-resources-litellm"])
        return out.strip() or "absent"
    return "absent"


def service_logs(tail: int = 100) -> str:
    mode = runtime_mode()
    if mode in ("pipx", "pip-venv"):
        if is_macos():
            log_file = log_dir() / "litellm.log"
            err_file = log_dir() / "litellm.err"
            out_lines = []
            for f in (err_file, log_file):
                if f.exists():
                    rc, out, _ = _run(["tail", "-n", str(tail), str(f)])
                    if rc == 0 and out:
                        out_lines.append(f"--- {f.name} ---\n{out}")
            return "\n".join(out_lines)
        rc, out, _ = _run(["journalctl", "--user", "-u", lifecycle_path().name,
                           "-n", str(tail), "--no-pager"])
        return out
    if mode == "docker":
        rc, out, _ = _run(["docker", "logs", "--tail", str(tail),
                           "ai-resources-litellm"], timeout=10)
        return out
    return ""


# ============================================================
# Health check
# ============================================================
def health_check(url: str = "http://127.0.0.1:4000/health/liveliness",
                 timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for_health(url: str = "http://127.0.0.1:4000/health/liveliness",
                    max_attempts: int = 30, delay: float = 1.0) -> bool:
    for _ in range(max_attempts):
        if health_check(url, timeout=2):
            return True
        time.sleep(delay)
    return False


def validate_remote(url: str, master_key: str) -> tuple[bool, str]:
    """Test a remote LiteLLM endpoint."""
    if httpx is None:
        try:
            req = urllib.request.Request(
                url.rstrip("/") + "/health/liveliness",
                headers={"Authorization": f"Bearer {master_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200, ""
        except (urllib.error.URLError, OSError) as e:
            return False, str(e)
    try:
        r = httpx.get(
            url.rstrip("/") + "/health/liveliness",
            headers={"Authorization": f"Bearer {master_key}"} if master_key else {},
            timeout=5.0,
        )
        return r.status_code == 200, ("" if r.status_code == 200
                                       else f"HTTP {r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as e:
        return False, str(e)


def smoke_test_model(model: str, gateway_url: str, master_key: str) -> tuple[bool, str]:
    """Round-trip a tiny prompt through the gateway."""
    import json
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with just OK"}],
        "max_tokens": 5,
    }
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    url = gateway_url.rstrip("/") + "/v1/chat/completions"

    if httpx is not None:
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=30.0)
            if r.status_code == 200:
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as e:
            return False, str(e)

    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200, ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except (urllib.error.URLError, OSError) as e:
        return False, str(e)


# ============================================================
# Update (re-pull pipx package or docker image)
# ============================================================
def _render_compose_yaml() -> str:
    """Generate docker-compose.yaml for the LiteLLM container."""
    s = state.load()
    bind = s.litellm.local.bind_address or "127.0.0.1"
    port = s.litellm.local.port or 4000
    image = s.litellm.local.image or DEFAULT_DOCKER_IMAGE
    body = {
        "services": {
            "ai-resources-litellm": {
                "image": image,
                "container_name": "ai-resources-litellm",
                "command": ["--config", "/app/config.yaml", "--port", "4000",
                            "--host", "0.0.0.0"],
                # Bind only to the requested host:port — no LAN exposure, no
                # collision with other containers running on different ports.
                "ports": [f"{bind}:{port}:4000"],
                "volumes": [
                    f"{state.litellm_path()}:/app/config.yaml:ro",
                    f"{state.env_path()}:/app/.env:ro",
                ],
                "env_file": [str(state.env_path())],
                # `always` covers the user's case where Docker daemon restarts
                # without us — no LaunchAgent / systemd unit needed for docker mode.
                "restart": "always",
                # Dedicated network so this stack stays isolated from any other
                # compose project on the same host.
                "networks": ["ai-resources"],
                # Use python3 (guaranteed in Python-based images) — curl is absent
                # from slim images and causes the container to be marked unhealthy
                # even when LiteLLM is serving normally.
                "healthcheck": {
                    "test": ["CMD-SHELL",
                             "python3 -c \"import urllib.request; "
                             "urllib.request.urlopen("
                             "'http://localhost:4000/health/liveliness', timeout=3)"
                             "\" 2>/dev/null || exit 1"],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                    "start_period": "30s",
                },
            }
        },
        "networks": {
            "ai-resources": {"driver": "bridge"},
        },
    }
    if yaml is None:
        raise RuntimeError("PyYAML required to render docker-compose.yaml")
    return yaml.safe_dump(body, default_flow_style=False, sort_keys=False)


def write_compose_yaml() -> Path:
    state.compose_path().parent.mkdir(parents=True, exist_ok=True)
    state.compose_path().write_text(_render_compose_yaml(), encoding="utf-8")
    return state.compose_path()


def pull_image(image: str = DEFAULT_DOCKER_IMAGE) -> tuple[bool, str]:
    s = state.load()
    runtime = s.litellm.local.runtime if s.litellm.local.runtime in ("docker", "podman") else "docker"
    rc, out, err = _run([runtime, "pull", image], timeout=900)
    return rc == 0, (err or out)[:1000]


def install_docker_lifecycle(compose_path: Path) -> Path:
    """Write platform plist/systemd unit that runs `docker compose up -d` at login."""
    path = lifecycle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    s = state.load()
    runtime = s.litellm.local.runtime if s.litellm.local.runtime in ("docker", "podman") else "docker"

    if is_macos():
        # plist runs `docker compose up -d` and exits — Docker's restart policy keeps it alive
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>until {runtime} info &gt;/dev/null 2&gt;&amp;1; do sleep 5; done; {runtime} compose -f {compose_path} up -d --wait</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_dir()}/litellm.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir()}/litellm.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:{Path.home()}/.docker/bin</string>
  </dict>
</dict>
</plist>
"""
        path.write_text(plist, encoding="utf-8")
        _run(["launchctl", "unload", str(path)])
        rc, _, err = _run(["launchctl", "load", str(path)])
        if rc != 0 and "already loaded" not in err.lower():
            raise RuntimeError(f"launchctl load failed: {err}")
    else:
        unit = f"""[Unit]
Description=ai-resources LiteLLM gateway (docker)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/sh -c 'until {runtime} info >/dev/null 2>&1; do sleep 5; done'
ExecStart=/usr/bin/env {runtime} compose -f {compose_path} up -d --wait
ExecStop=/usr/bin/env {runtime} compose -f {compose_path} down

[Install]
WantedBy=default.target
"""
        path.write_text(unit, encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        rc, _, err = _run(["systemctl", "--user", "enable", "--now", path.name])
        if rc != 0:
            raise RuntimeError(f"systemctl enable failed: {err}")
    return path


def update_litellm() -> tuple[bool, str]:
    mode = runtime_mode()
    if mode == "pipx":
        rc, out, err = _run(["pipx", "upgrade", "litellm"], timeout=600)
        return rc == 0, out + err
    if mode == "pip-venv":
        pip = venv_dir() / "bin" / "pip"
        rc, out, err = _run([str(pip), "install", "--upgrade", DEFAULT_LITELLM_PIP_SPEC],
                            timeout=600)
        return rc == 0, out + err
    if mode == "docker":
        rc, _, err = _run(["docker", "pull", DEFAULT_DOCKER_IMAGE], timeout=600)
        if rc != 0:
            return False, err
        return restart_service(), ""
    return False, "unknown mode"
