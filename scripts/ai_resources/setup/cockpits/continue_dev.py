"""Continue.dev cockpit configurator."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import state
from ..detection import detect_continue
from ... import repo_root
from . import _shared


NAME = "Continue.dev"
ID = "continue"
CONFIG_ROOT = Path.home() / ".continue"
INSTRUCTIONS_PATH = CONFIG_ROOT / "AGENT_KIT.md"


def detect():
    return detect_continue()


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(repo_root())
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    written: list[Path] = []

    multimodel = _shared.multimodel_protocol_md(ak_path, gateway_url, s.mode)
    md = (
        f"# ai-resources (Continue.dev)\n\n"
        f"**Refresh:** After updating the kit, run `ai-resources generate`.\n\n"
        f"{multimodel}"
        f"## Skill Discovery\n\n"
        f"- Skills index: `{ak_path}/skills-index.json`\n"
        f"- Workflows: `{ak_path}/workflows/`\n"
    )
    if _shared.write_text(INSTRUCTIONS_PATH, md):
        written.append(INSTRUCTIONS_PATH)

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written
