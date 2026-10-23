"""Gemini CLI cockpit configurator — multi-model protocol + Engram MCP."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .. import state
from ..detection import detect_gemini_cli
from ... import repo_root
from . import _shared
from .. import cockpit_auth, role_render


NAME = "Gemini CLI"
ID = "gemini"
CONFIG_ROOT = Path.home() / ".gemini"
SETTINGS_PATH = CONFIG_ROOT / "settings.json"
AGY_MCP_PATH = CONFIG_ROOT / "config" / "mcp_config.json"
GEMINI_MD_PATH = CONFIG_ROOT / "GEMINI.md"


def detect():
    return detect_gemini_cli()


def _settings_patch(auth_method: str | None = None) -> dict:
    """Patch ~/.gemini/settings.json with Engram MCP; the auth type only when one is asked for explicitly, because agy
    signs in with a Google account and a forced `gemini-api-key` would override it."""
    patch = {"mcpServers": _shared.mcp_engram_block(), "contextFileName": "GEMINI.md"}
    if auth_method:
        patch["selectedAuthType"] = auth_method
    return patch


def _agy_mcp_patch() -> dict:
    """Patch ~/.gemini/config/mcp_config.json with Engram MCP server for agy."""
    return {
        "mcpServers": _shared.mcp_engram_block(),
    }


def _gemini_md(ak_path: str, gateway_url: str, mode: str) -> str:
    roles = ""
    try:
        specs = role_render.load_specs(Path(ak_path))
        if specs:
            roles = "## Kit roles\n\n" + role_render.agy_role_block(specs, ak_path) + "\n"
    except OSError:
        roles = ""
    memory = (
        "Memory: Engram MCP is configured in `~/.gemini/settings.json` and `~/.gemini/config/mcp_config.json`. "
        "Search it before redoing past investigations; save decisions and gotchas that are not already in code or specs.\n\n"
        + roles
    )
    return _shared.kit_instructions_md("Gemini CLI", ak_path, gateway_url, mode,
                                       native_skills=True, extra=memory)


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    ak_path = str(_shared.stable_kit_root(repo_root()))
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    mode = s.mode

    auth_method = ctx.get("gemini_auth_method")
    written: list[Path] = []

    _shared.deep_merge_json(SETTINGS_PATH, _settings_patch(auth_method))
    written.append(SETTINGS_PATH)

    # ADR-0014 D3.4: the agy MCP file gets `engram` only if absent, with a backup and a state record; never a bare merge.
    from .. import agy_mcp
    try:
        if agy_mcp.add_servers(AGY_MCP_PATH, _shared.mcp_engram_block(), s.tracking) is not None or AGY_MCP_PATH.is_file():
            written.append(AGY_MCP_PATH)
    except OSError:
        pass

    from .. import operator_rules
    md = operator_rules.append_to(_gemini_md(ak_path, gateway_url, mode))     # ADR-0016 D4: one rules file, three cockpits
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


AUTH_PROMPT = "Antigravity sign-in:"


def ask_auth(ui_mod, s, detected=None, dry_run: bool = False) -> int:
    """Antigravity (agy) signs in with a Google account in its own app (ADR-0016 D1): show whether that account state is
    present (presence only, nothing in it is read), ask keep or change. Change hands the terminal to `agy` itself on an
    explicit choice, never in a non-interactive run or a dry run. The ADR-0007 Google terms acknowledgement is asked where
    the agy lanes are enabled (never here), so a stored tick cannot skip it."""
    from .. import live_auth
    cockpit_auth.set_method(s, "agy", "google_account")
    auth = live_auth.read_agy()
    cockpit_auth.set_live(s, "agy", auth.kind)
    ui_mod.detail(f"Antigravity: {auth.label}; its terms are confirmed when you enable agy lanes.")
    if ui_mod.is_non_interactive():
        return 0
    picked = ui_mod.select(AUTH_PROMPT, [ui_mod.Choice("Keep it", value="keep"),
                                         ui_mod.Choice("Change it with agy's own sign-in", value="change")],
                           default="keep")
    if picked is None:
        return 130
    if picked == "change":
        shown = " ".join(live_auth.AGY_LOGIN_ARGV)
        if dry_run:
            ui_mod.detail(f"Dry run: would hand this terminal to `{shown}`.")
            return 0
        ui_mod.info(f"Applies now: `{shown}` runs on this terminal; sign in there, then exit it. The kit sees nothing you type.")
        rc = live_auth.run_handoff(live_auth.AGY_LOGIN_ARGV)
        if rc != 0:
            ui_mod.warn(f"`{shown}` exited {rc}; the sign-in is whatever agy reports now.")
        else:
            ui_mod.record_action("opened agy for its own sign-in")
        cockpit_auth.set_live(s, "agy", live_auth.read_agy().kind)
    return 0
