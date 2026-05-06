"""LiteLLM gateway lifecycle (local container) and remote validation."""
from __future__ import annotations

import json
import platform
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
from .. import repo_root


CONTAINER_NAME = "ai-resources-litellm"
DEFAULT_IMAGE = "ghcr.io/berriai/litellm:main-stable"


def _runtime() -> str:
    return state.load().litellm.local.runtime or "docker"


def _compose_cmd() -> list[str]:
    runtime = _runtime()
    if runtime == "podman":
        return ["podman", "compose"]
    return ["docker", "compose"]


def _docker_cmd() -> list[str]:
    return [_runtime()]


def _run(cmd: list[str], timeout: int = 60, check: bool = False) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "command not found"


# --- template rendering -------------------------------------------------------
def _templates_dir() -> Path:
    return repo_root() / "templates"


def _render_litellm_yaml(executors: dict, providers: dict, master_key_env: str) -> str:
    """Build litellm.yaml content from executors.yaml + providers config."""
    model_list: list[dict] = []
    seen_models: set[str] = set()

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
                "api_base": "http://host.docker.internal:11434",
            }
        else:
            continue

        model_list.append({
            "model_name": model_name,
            "litellm_params": litellm_params,
        })

    # Deduplicate fallbacks per model — multiple roles may share a model
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
            "cache_params": {
                "type": "local",
                "ttl": 600,
            },
        },
        "general_settings": {
            "master_key": credentials.secret_ref(master_key_env),
        },
    }
    if yaml is None:
        raise RuntimeError("PyYAML required to render litellm.yaml")
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)


def _render_compose_yaml(image: str, bind_address: str, port: int) -> str:
    body = {
        "services": {
            CONTAINER_NAME: {
                "image": image,
                "container_name": CONTAINER_NAME,
                "command": ["--config", "/app/config.yaml", "--port", "4000"],
                "ports": [f"{bind_address}:{port}:4000"],
                "volumes": [
                    f"{state.litellm_path()}:/app/config.yaml:ro",
                    f"{state.env_path()}:/app/.env:ro",
                ],
                "env_file": [str(state.env_path())],
                "restart": "unless-stopped",
                "healthcheck": {
                    "test": ["CMD", "curl", "-fsS", "http://localhost:4000/health/liveliness"],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                    "start_period": "10s",
                },
            }
        }
    }
    if yaml is None:
        raise RuntimeError("PyYAML required to render docker-compose.yaml")
    return yaml.safe_dump(body, default_flow_style=False, sort_keys=False)


def write_configs(executors: dict, providers: dict, master_key_env: str = "LITELLM_MASTER_KEY") -> dict:
    """Write litellm.yaml + docker-compose.yaml. Returns dict of written paths."""
    state.config_root().mkdir(parents=True, exist_ok=True)
    paths = {}

    litellm_yaml = _render_litellm_yaml(executors, providers, master_key_env)
    state.litellm_path().write_text(litellm_yaml, encoding="utf-8")
    paths["litellm.yaml"] = state.litellm_path()

    s = state.load()
    compose_yaml = _render_compose_yaml(
        image=s.litellm.local.image or DEFAULT_IMAGE,
        bind_address=s.litellm.local.bind_address or "127.0.0.1",
        port=s.litellm.local.port or 4000,
    )
    state.compose_path().write_text(compose_yaml, encoding="utf-8")
    paths["docker-compose.yaml"] = state.compose_path()

    return paths


# --- container lifecycle -------------------------------------------------------
def pull_image(image: str = DEFAULT_IMAGE) -> bool:
    rc, out, err = _run([*_docker_cmd(), "pull", image], timeout=600)
    return rc == 0


def start_container() -> bool:
    rc, out, err = _run([*_compose_cmd(), "-f", str(state.compose_path()), "up", "-d"], timeout=120)
    if rc != 0:
        ui.error(f"Failed to start container: {err or out}")
        return False
    return True


def stop_container() -> bool:
    rc, _, err = _run([*_compose_cmd(), "-f", str(state.compose_path()), "down"], timeout=60)
    return rc == 0


def restart_container() -> bool:
    rc, _, _ = _run([*_compose_cmd(), "-f", str(state.compose_path()), "restart"], timeout=60)
    return rc == 0


def container_status() -> str:
    """Return 'running' | 'stopped' | 'absent' | 'unhealthy'."""
    rc, out, _ = _run([*_docker_cmd(), "inspect", "--format", "{{.State.Status}}", CONTAINER_NAME])
    if rc != 0:
        return "absent"
    state_str = out.strip()
    if state_str == "running":
        rc, healthy, _ = _run([*_docker_cmd(), "inspect", "--format", "{{.State.Health.Status}}", CONTAINER_NAME])
        if rc == 0 and healthy.strip() and healthy.strip() != "healthy":
            return "unhealthy"
        return "running"
    return state_str or "absent"


def container_logs(tail: int = 100) -> str:
    rc, out, _ = _run([*_docker_cmd(), "logs", "--tail", str(tail), CONTAINER_NAME], timeout=10)
    return out if rc == 0 else ""


# --- health & probes -----------------------------------------------------------
def health_check(url: str = "http://127.0.0.1:4000/health/liveliness", timeout: int = 5) -> bool:
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
    """Test reachability of a remote LiteLLM endpoint."""
    if httpx is None:
        # Fallback to urllib
        try:
            req = urllib.request.Request(
                url.rstrip("/") + "/health/liveliness",
                headers={"Authorization": f"Bearer {master_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status == 200, ""
        except urllib.error.URLError as e:
            return False, str(e)
        except OSError as e:
            return False, str(e)
    try:
        r = httpx.get(
            url.rstrip("/") + "/health/liveliness",
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=5.0,
        )
        if r.status_code == 200:
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.HTTPError as e:
        return False, str(e)


def smoke_test_model(model: str, gateway_url: str, master_key: str) -> tuple[bool, str]:
    """Round-trip a tiny prompt through the gateway. Returns (ok, detail)."""
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


# --- lifecycle scripts (launchd / systemd) ------------------------------------
def lifecycle_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.ai-resources.litellm.plist"
    return Path.home() / ".config" / "systemd" / "user" / "ai-resources-litellm.service"


def _render_launchd_plist() -> str:
    compose = state.compose_path()
    runtime = _runtime()
    log_dir = state.config_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ai-resources.litellm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>while ! {runtime} info &gt;/dev/null 2&gt;&amp;1; do sleep 5; done; {runtime} compose -f {compose} up -d --wait || true; tail -f /dev/null</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_dir}/litellm.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/litellm.err</string>
</dict>
</plist>
"""


def _render_systemd_unit() -> str:
    compose = state.compose_path()
    runtime = _runtime()
    return f"""[Unit]
Description=ai-resources LiteLLM gateway
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/sh -c 'until {runtime} info >/dev/null 2>&1; do sleep 5; done'
ExecStart=/usr/bin/env {runtime} compose -f {compose} up -d --wait
ExecStop=/usr/bin/env {runtime} compose -f {compose} down

[Install]
WantedBy=default.target
"""


def install_lifecycle() -> Path:
    """Write platform-appropriate lifecycle file. Returns path."""
    path = lifecycle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        path.write_text(_render_launchd_plist(), encoding="utf-8")
        _run(["launchctl", "unload", str(path)])
        _run(["launchctl", "load", str(path)])
    else:
        path.write_text(_render_systemd_unit(), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", path.name])
        _run(["systemctl", "--user", "start", path.name])
    return path


def uninstall_lifecycle() -> None:
    path = lifecycle_path()
    if not path.exists():
        return
    if platform.system() == "Darwin":
        _run(["launchctl", "unload", str(path)])
    else:
        _run(["systemctl", "--user", "stop", path.name])
        _run(["systemctl", "--user", "disable", path.name])
    path.unlink(missing_ok=True)


def update_image(image: str = DEFAULT_IMAGE) -> bool:
    if not pull_image(image):
        return False
    return restart_container()
