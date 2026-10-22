"""The agy MCP server file `~/.gemini/config/mcp_config.json` (ADR-0014 D3.4).

agy loads every server in this file into each session, so each one adds its tool schemas to every agy prompt. The kit used
to only print a hint. ADR-0014 lets it keep the file lean, on confirmation, without ever removing what is there:

- the kit's own entry is `engram`, written only if absent (fill-only) after one confirm;
- other servers are opt-in: a multiselect (default none) over the Claude Code user-level servers that are plain stdio
  commands with no environment values (nothing secret is copied into this file); each chosen one is written only if absent;
- merge-preserving: every other key and server of the file stays byte-for-value as it was; the file is replaced atomically;
- an exclusive backup of the file is made before the first edit of a run;
- every added server is recorded in `tracking.agy_mcp_added` with the definition written; teardown removes only an entry that
  still equals what the kit wrote;
- never headless and never in a dry run. `--strict-mcp-config` is not involved (ADR-0007 section 9 keeps it forbidden).
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import billing
from . import ui
from .cockpits import _shared

KIT_SERVER = "engram"


def mcp_path() -> Path:
    return Path.home() / ".gemini" / "config" / "mcp_config.json"


def _read(path: Path) -> tuple[dict | None, str]:
    """(document, raw text). (None, raw) when the file exists but is not a JSON object: it is then left alone."""
    raw = _shared._read_raw(path)
    if not raw.strip():
        return {}, raw
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, raw
    return (doc, raw) if isinstance(doc, dict) else (None, raw)


def servers_of(doc: dict) -> dict:
    servers = doc.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def portable_candidates(claude_json: Path | None = None) -> dict[str, dict]:
    """Claude Code user-level servers the kit can offer for agy: stdio commands with no env values. Name -> definition
    written to the file (command and args only)."""
    from . import hygiene
    out: dict[str, dict] = {}
    for name, cfg in hygiene.user_mcp_servers(claude_json or billing.claude_json_path()).items():
        if name == KIT_SERVER or hygiene.is_kit_owned_mcp(name):
            continue
        if cfg.get("type", "stdio") != "stdio" or not isinstance(cfg.get("command"), str) or cfg.get("env"):
            continue
        args = cfg.get("args")
        out[name] = {"command": cfg["command"], "args": list(args) if isinstance(args, list) else []}
    return out


def plan(doc: dict, wanted: dict[str, dict]) -> dict[str, dict]:
    """The servers that will be added: wanted ones the file does not have (fill-only)."""
    have = servers_of(doc)
    return {name: copy.deepcopy(defn) for name, defn in wanted.items() if name not in have}


def add_servers(path: Path, additions: dict[str, dict], tracking, *, now: datetime | None = None) -> Path | None:
    """Merge `additions` into the file (creating it if absent), back it up first, and record each one. Returns the
    backup path (None when there was nothing to back up). Raises OSError when the file cannot be read as an object."""
    doc, raw = _read(path)
    if doc is None:
        raise OSError(f"{path} is not a JSON object; left as it is")
    fresh = plan(doc, additions)
    if not fresh:
        return None
    backup = None
    if raw:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        backup = _shared._create_backup(path, raw, stamp)
    merged = copy.deepcopy(doc)
    merged.setdefault("mcpServers", {})
    if not isinstance(merged["mcpServers"], dict):
        raise OSError(f"{path}: mcpServers is not an object; left as it is")
    merged["mcpServers"].update(fresh)
    target = _shared._write_target(path)
    if target is None:
        return backup
    path.parent.mkdir(parents=True, exist_ok=True)
    _shared._replace_atomically(target, json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    for name, defn in fresh.items():
        tracking.agy_mcp_added[name] = copy.deepcopy(defn)
    return backup


def remove_recorded(path: Path, tracking) -> list[str]:
    """Teardown: remove the recorded servers that still equal what the kit wrote. Returns the names removed."""
    if not tracking.agy_mcp_added:
        return []
    doc, raw = _read(path)
    removed: list[str] = []
    if doc is not None:
        servers = servers_of(doc)
        for name, written in list(tracking.agy_mcp_added.items()):
            if name in servers and servers[name] == written:
                del servers[name]
                removed.append(name)
        if removed:
            target = _shared._write_target(path)
            if target is not None:
                _shared._replace_atomically(target, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    tracking.agy_mcp_added = {}                 # an entry the operator changed or removed is theirs now
    return removed


def agy_in_use(s) -> bool:
    from . import team_view
    selected = (s.profile.customizations.get("__targets__") or {}).get("selected") or []
    return "agy" in selected or s.openclaw.engine == "antigravity" or team_view.agy_lanes_on(s.profile.lanes)


def run_step(s, *, dry_run: bool = False, path: Path | None = None, candidates: dict[str, dict] | None = None,
             now: datetime | None = None) -> bool:
    """Ask, then write. True when the file changed. Never headless, never in a dry run."""
    from . import hygiene
    if not agy_in_use(s):
        return False
    path = path or mcp_path()
    doc, _raw = _read(path)
    if doc is None:
        ui.warn(f"{path} is not a JSON object; the kit leaves it as it is.")
        return False
    have = servers_of(doc)
    missing_engram = KIT_SERVER not in have
    cands = portable_candidates() if candidates is None else candidates
    offers = {n: d for n, d in cands.items() if n not in have and n not in s.tracking.agy_mcp_added}
    if hygiene.is_headless() or dry_run:
        if missing_engram or offers:
            ui.detail(("DRY RUN: " if dry_run else "") + f"agy loads every server in {path} into each session; "
                      "run setup in a terminal to add `engram` and pick any others (default none).")
        return False
    additions: dict[str, dict] = {}
    if missing_engram and ui.confirm(f"Add the engram MCP server to {path} (agy)? Nothing already in the file is "
                                     "changed or removed.", default=True):
        additions.update(_shared.mcp_engram_block())
    if offers:
        chosen = ui.checkbox("Also add these Claude Code MCP servers to agy? Each one loads its tool schemas into every "
                             "agy session; leave empty to add none.", [ui.Choice(n, value=n) for n in sorted(offers)],
                             default=[]) or []
        additions.update({n: offers[n] for n in sorted(offers) if n in chosen})
    if not additions:
        return False
    try:
        backup = add_servers(path, additions, s.tracking, now=now)
    except OSError as e:
        ui.warn(f"Could not update {path}: {e}")
        return False
    ui.ok(f"agy MCP: added {', '.join(sorted(additions))} to {path}" + (f" (backup: {backup})" if backup else ""))
    ui.record_action("updated the agy MCP file")
    return True


def cmd_restore(*, path: Path | None = None, say: Callable[[str], None] = print) -> int:
    """`ai-resources setup --restore-agy-mcp`: take back the servers the kit added, where they are still as written."""
    from . import state
    path = path or mcp_path()
    recorded = dict(state.load().tracking.agy_mcp_added)
    if not recorded:
        say("Nothing to restore: the kit has not added any server to the agy MCP file.")
        return 0
    probe = state.InstallTracking()
    probe.agy_mcp_added = dict(recorded)
    removed = remove_recorded(path, probe)
    for name in removed:
        say(f"Removed {name} from {path}")
    for name in sorted(set(recorded) - set(removed)):
        say(f"Left {name}: it is gone or you changed it since the kit added it.")

    def mutate(fresh) -> None:
        fresh.tracking.agy_mcp_added = {}

    if not state.update(mutate):
        say("The setup state is busy: the servers were handled but their record was not cleared. Run it again.")
        return 1
    return 0
