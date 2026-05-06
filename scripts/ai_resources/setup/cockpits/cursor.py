"""Cursor cockpit configurator."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import state
from ..detection import detect_cursor
from ... import repo_root
from . import _shared


NAME = "Cursor"
ID = "cursor"
CONFIG_ROOT = Path.home() / ".cursor"
INSTRUCTIONS_PATH = CONFIG_ROOT / "AGENT_KIT.md"
MCP_PATH = CONFIG_ROOT / "mcp.json"


def detect():
    return detect_cursor()


def _instructions(ak_path: str, gateway_url: str, mode: str) -> str:
    multimodel = _shared.multimodel_protocol_md(ak_path, gateway_url, mode)
    return (
        f"# ai-resources (Cursor)\n\n"
        f"- **Repository:** `{ak_path}` — set `export AGENT_KIT={ak_path}` in shell if needed.\n"
        f"- **Rules:** `{ak_path}/rules/*.mdc` (loaded via alwaysApply / globs)\n"
        f"- **Refresh:** After updating the kit, run `ai-resources generate`.\n\n"
        f"{multimodel}"
        f"## Skill Discovery Protocol\n\n"
        f"1. Read `{ak_path}/skills-index.json`\n"
        f"2. Match task against skill triggers\n"
        f"3. Read matching SKILL.md files\n"
        f"4. Apply skill instructions (override generic patterns)\n\n"
        f"### Key paths\n\n"
        f"| Resource | Path |\n"
        f"|----------|------|\n"
        f"| Skills | `{ak_path}/skills/` |\n"
        f"| Workflows | `{ak_path}/workflows/` |\n"
        f"| Rules | `{ak_path}/rules/` |\n"
    )


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(repo_root())
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    written: list[Path] = []

    if _shared.write_text(INSTRUCTIONS_PATH, _instructions(ak_path, gateway_url, s.mode)):
        written.append(INSTRUCTIONS_PATH)

    # MCP servers (Engram)
    _shared.deep_merge_json(MCP_PATH, {"mcpServers": _shared.mcp_engram_block()})
    written.append(MCP_PATH)

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written
