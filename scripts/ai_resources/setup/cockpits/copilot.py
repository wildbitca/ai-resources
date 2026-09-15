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
    ak_path = str(_shared.stable_kit_root(repo_root()))
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    written: list[Path] = []

    md = _shared.kit_instructions_md("GitHub Copilot", ak_path, gateway_url, s.mode, native_skills=True)
    if _shared.write_managed_block(INSTRUCTIONS_PATH, md):
        written.append(INSTRUCTIONS_PATH)
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
