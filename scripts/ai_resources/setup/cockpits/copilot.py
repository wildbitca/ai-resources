"""GitHub Copilot (VS Code) cockpit — writes copilot-instructions.md."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import state
from ..detection import detect_copilot
from ... import repo_root
from . import _shared


NAME = "GitHub Copilot"
ID = "copilot"
CONFIG_ROOT = Path.home() / ".vscode"
INSTRUCTIONS_PATH = CONFIG_ROOT / "copilot-instructions.md"


def detect():
    return detect_copilot()


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(repo_root())
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    written: list[Path] = []

    multimodel = _shared.multimodel_protocol_md(ak_path, gateway_url, s.mode)
    md = (
        f"# ai-resources (GitHub Copilot)\n\n"
        f"**Refresh:** After updating the kit, run `ai-resources generate`.\n\n"
        f"{multimodel}"
        f"## Skill Discovery\n\n"
        f"Skills: `{ak_path}/skills/` — index at `{ak_path}/skills-index.json`.\n"
        f"Workflows: `{ak_path}/workflows/`.\n"
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
