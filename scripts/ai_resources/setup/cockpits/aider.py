"""Aider cockpit configurator — uses LiteLLM for multi-provider support."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from .. import state, ui
from ..detection import detect_aider
from ... import model_pins, repo_root
from . import _shared


NAME = "Aider"
ID = "aider"
CONFIG_ROOT = Path.home() / ".aider"
CONVENTIONS_PATH = CONFIG_ROOT / "CONVENTIONS.md"
CONF_PATH = Path.home() / ".aider.conf.yml"


def detect():
    return detect_aider()


def _conf_yaml(executors: dict, gateway_url: str, master_key: str) -> str:
    """Generate ~/.aider.conf.yml with LiteLLM endpoint and per-role models.

    Aider supports 3 roles: architect (planner/thinker), editor (default model),
    weak (commits/summaries). We pick the most appropriate from executors.
    """
    by_role = executors.get("by_role", {})

    pins = model_pins.effective()
    architect_model = by_role.get("software-architect", {}).get("model") \
        or by_role.get("planner", {}).get("model") \
        or pins["opus"]
    editor_model = by_role.get("implementer", {}).get("model") \
        or pins["sonnet"]
    weak_model = by_role.get("verifier", {}).get("model") \
        or pins["haiku"]

    conf = {
        "openai-api-base": gateway_url,
        "openai-api-key": master_key,
        "model": editor_model,
        "architect-model": architect_model,
        "weak-model": weak_model,
        "read": [str(CONVENTIONS_PATH)],
        "auto-commits": True,
        "dirty-commits": False,
        "no-stream": False,
    }
    if yaml is None:
        # Minimal hand-written fallback
        return "\n".join(f"{k}: {v}" for k, v in conf.items())
    return yaml.safe_dump(conf, default_flow_style=False, sort_keys=False)


def _backup_private(src: Path, dest: Path) -> None:
    """Copy `src` to `dest` created 0600 from the start: the file holds the gateway master key, and a plain
    copy would take the umask (0644) instead of the source's mode. Never wider than 0600, whatever `src` is."""
    data = src.read_bytes()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)   # an older backup may exist with a wider mode: O_CREAT does not reset it
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(_shared.stable_kit_root(repo_root()))
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    master_key = ctx.get("master_key", "")
    executors = ctx.get("executors", {})
    written: list[Path] = []

    # Conventions file
    md = _shared.kit_instructions_md(
        "Aider", ak_path, gateway_url, s.mode, native_skills=False,
        route="via_gateway" if master_key else "instructions_only",
        extra="Loaded automatically via `~/.aider.conf.yml` (`read:` directive).\n\n",
    )
    if _shared.write_managed_block(CONVENTIONS_PATH, md):
        written.append(CONVENTIONS_PATH)

    # ~/.aider.conf.yml only when multi-model and the matrix wires Aider to the gateway
    route = ctx.get("route")
    wired = route is None or route.action == "via_gateway"
    if s.mode == "multi-model" and master_key and wired:
        base = ctx.get("openai_base") or gateway_url
        text = _conf_yaml(executors, base, master_key)
        if CONF_PATH.is_file() and CONF_PATH.read_text(encoding="utf-8") != text:
            # A changed base (OpenRouter's OpenAI base is /api/v1) alters an existing file: keep the old one.
            _backup_private(CONF_PATH, CONF_PATH.with_name(CONF_PATH.name + ".kit-bak"))
        if _shared.write_text(CONF_PATH, text):
            written.append(CONF_PATH)
    elif route is not None and route.action == "skip":
        ui.info(f"Aider: no model setting written ({route.reason})")

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written
