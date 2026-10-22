"""Hash-guarded trim of the Engram plugin's first-message ToolSearch directive.

The Engram Claude Code plugin (external, not part of the kit) injects on the first prompt of every session:
"CRITICAL FIRST ACTION - Execute this ToolSearch NOW ... select:mcp__engram__mem_save,..." which forces a ToolSearch
round trip loading 13 tool schemas. The core mem_* tools need no ToolSearch (the MCP server's own instructions say so),
so the directive only adds a turn and prefix tokens (2026-10-22 cost analysis).

Contract:
- the plugin directory is the only thing touched; nothing outside it is written;
- the line is replaced only when its SHA-256 equals the known original; unknown text is left alone with a warning;
- the original file is saved as `<file>.ai-resources-backup` before the first change;
- a marker `<scripts>/.ai-resources-engram-patch.json` records the plugin version and both hashes;
- a plugin update can restore the original text; the next `ai-resources setup` re-applies the trim;
- never raises: a failure is a warning and setup continues.

SessionStart: its context comes from the engram server (`/context?project=`), and the plugin script has no size
knob, so the SessionStart payload is not controllable from the kit.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import ui

SCRIPT_NAME = "user-prompt-submit.sh"
MARKER = ".ai-resources-engram-patch.json"
BACKUP_SUFFIX = ".ai-resources-backup"

ORIGINAL_LINE_SHA256 = "a862717b009c08396488e109d78a24b1508d52daa4fd9a837fa964d45fda9cf7"
_ORIGINAL_PREFIX = "  printf '%s\\n' '{\"systemMessage\":\"CRITICAL FIRST ACTION"
REPLACEMENT_LINE = (
    "  printf '%s\\n' '{\"systemMessage\":\"Engram: call mem_context to check for prior session history before "
    "responding. The core mem_* tools need no ToolSearch; load a deferred one (mem_update, mem_session_start, "
    "mem_session_end, ...) with ToolSearch only when you need it.\"}'"
)


def _sha(text: str) -> str:
    return hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()


def plugin_dirs(home: Path | None = None) -> list[Path]:
    """Install paths of the engram plugin that hold a prompt hook script (read from Claude Code's own record)."""
    home = home or Path.home()
    out: list[Path] = []
    record = home / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for entry in ((data.get("plugins") or {}).get("engram@engram") or []):
        base = Path(str((entry or {}).get("installPath", "")))
        for cand in (base / "scripts", base / "plugin" / "claude-code" / "scripts"):
            if (cand / SCRIPT_NAME).is_file() and cand not in out:
                out.append(cand)
    return out


def trim_script(script: Path, version: str = "") -> str:
    """One script: 'patched' | 'already' | 'unknown' | 'absent' | 'error'."""
    try:
        if not script.is_file():
            return "absent"
        text = script.read_text(encoding="utf-8")
        lines = text.split("\n")
        idx = [i for i, ln in enumerate(lines) if ln.startswith(_ORIGINAL_PREFIX)]
        if not idx:
            return "already" if REPLACEMENT_LINE in lines else "unknown"
        if len(idx) != 1 or _sha(lines[idx[0]]) != ORIGINAL_LINE_SHA256:
            return "unknown"
        backup = script.with_name(script.name + BACKUP_SUFFIX)
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        lines[idx[0]] = REPLACEMENT_LINE
        new = "\n".join(lines)
        tmp = script.with_name(script.name + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.chmod(script.stat().st_mode & 0o777)
        tmp.replace(script)
        (script.parent / MARKER).write_text(json.dumps(
            {"plugin_version": version, "original_line_sha256": ORIGINAL_LINE_SHA256,
             "patched_line_sha256": _sha(REPLACEMENT_LINE)}, indent=2) + "\n", encoding="utf-8")
        return "patched"
    except Exception:  # noqa: BLE001 - best-effort: never abort setup
        return "error"


def trim_engram_hooks(home: Path | None = None) -> dict[str, str]:
    """Trim every installed engram prompt hook. Returns {script path: outcome}; never raises."""
    results: dict[str, str] = {}
    try:
        dirs = plugin_dirs(home)
    except Exception:  # noqa: BLE001
        return results
    for d in dirs:
        version = d.parent.name if d.name == "scripts" else ""
        outcome = trim_script(d / SCRIPT_NAME, version)
        results[str(d / SCRIPT_NAME)] = outcome
        if outcome == "patched":
            ui.info(f"Engram plugin: removed the first-message ToolSearch directive ({d / SCRIPT_NAME}); "
                    "a plugin update may restore it and the next `ai-resources setup` re-applies the trim.")
        elif outcome == "unknown":
            ui.warn(f"Engram plugin: {d / SCRIPT_NAME} differs from the version the kit knows; left unchanged.")
        elif outcome == "error":
            ui.warn(f"Engram plugin: could not trim {d / SCRIPT_NAME}; left unchanged.")
    return results
