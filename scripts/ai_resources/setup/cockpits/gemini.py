"""Gemini CLI cockpit configurator — multi-model protocol + Engram MCP."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .. import state
from ..detection import detect_gemini_cli
from ... import repo_root
from . import _shared


NAME = "Gemini CLI"
ID = "gemini"
CONFIG_ROOT = Path.home() / ".gemini"
SETTINGS_PATH = CONFIG_ROOT / "settings.json"
GEMINI_MD_PATH = CONFIG_ROOT / "GEMINI.md"


def detect():
    return detect_gemini_cli()


def _settings_patch(auth_method: str = "gemini-api-key") -> dict:
    """Patch ~/.gemini/settings.json with Engram MCP + auth selection."""
    return {
        "selectedAuthType": auth_method,
        "mcpServers": _shared.mcp_engram_block(),
        "contextFileName": "GEMINI.md",
    }


def _gemini_md(ak_path: str, gateway_url: str, mode: str) -> str:
    memory = (
        "Memory: Engram MCP is configured in `~/.gemini/settings.json`. Search it before redoing "
        "past investigations; save decisions and gotchas that are not already in code or specs.\n\n"
    )
    return _shared.kit_instructions_md("Gemini CLI", ak_path, gateway_url, mode,
                                       native_skills=True, extra=memory)


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(_shared.stable_kit_root(repo_root()))
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    mode = s.mode

    auth_method = ctx.get("gemini_auth_method", "gemini-api-key")
    written: list[Path] = []

    _shared.deep_merge_json(SETTINGS_PATH, _settings_patch(auth_method))
    written.append(SETTINGS_PATH)

    md = _gemini_md(ak_path, gateway_url, mode)
    if _shared.write_managed_block(GEMINI_MD_PATH, md):
        written.append(GEMINI_MD_PATH)
    _shared.link_agents_skills(ak_path)

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written
