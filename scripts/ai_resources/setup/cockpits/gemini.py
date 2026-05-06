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
    refresh = (
        f"After updating the kit, run `ai-resources generate`."
    )
    multimodel = _shared.multimodel_protocol_md(ak_path, gateway_url, mode)

    return (
        f"# ai-resources (Gemini CLI)\n\n"
        f"**Refresh:** {refresh}\n\n"
        f"{multimodel}"
        f"## Skill Discovery Protocol\n\n"
        f"This environment uses a centralized AI skills library.\n\n"
        f"1. **Read the catalog** — `{ak_path}/skills-index.json`\n"
        f"2. **Match** — Compare your task against each skill's description and triggers.\n"
        f"3. **Load** — For each match, read its SKILL.md.\n"
        f"4. **Apply** — Skill authority overrides generic patterns.\n\n"
        f"### Key paths\n\n"
        f"| Resource | Path |\n"
        f"|----------|------|\n"
        f"| Skills index | `{ak_path}/skills-index.json` |\n"
        f"| Skills root | `{ak_path}/skills/` |\n"
        f"| Workflows | `{ak_path}/workflows/` |\n"
        f"| Kit docs | `{ak_path}/AGENTS.md` |\n\n"
        f"## Memory (Engram MCP)\n\n"
        f"Engram MCP is configured at `~/.gemini/settings.json` and provides:\n\n"
        f"- `mem_search` — find past decisions, patterns, observations\n"
        f"- `mem_save` — persist decisions and discoveries proactively\n"
        f"- `mem_context` — recall recent session history\n"
        f"- `mem_session_summary` — close sessions with structured summaries\n\n"
        f"Save proactively after decisions, bug fixes, conventions discovered.\n"
    )


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(repo_root())
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    mode = s.mode

    auth_method = ctx.get("gemini_auth_method", "gemini-api-key")
    written: list[Path] = []

    _shared.deep_merge_json(SETTINGS_PATH, _settings_patch(auth_method))
    written.append(SETTINGS_PATH)

    md = _gemini_md(ak_path, gateway_url, mode)
    if _shared.write_text(GEMINI_MD_PATH, md):
        written.append(GEMINI_MD_PATH)

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written
