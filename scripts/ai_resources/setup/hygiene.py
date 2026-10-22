"""Hygiene step (ADR-0014 D3.2): offer to disable user-level MCP servers and plugins nobody calls.

What a session loads at its start (every enabled MCP server's tool list, every enabled plugin's skills and hooks) is
paid on every turn. The usage index (`usage_index`) says which of them had no call in the last 30 days; this step offers
to disable exactly those, one by one:

- candidates come from the data, never from a hard-coded list of names; a kit-owned entry (the engram server and
  plugin, the kit plugin) is never offered;
- every item is its own confirmation, default No; a decline changes and records nothing;
- an MCP server is removed with `claude mcp remove <name> -s user`, a plugin disabled with
  `claude plugin disable <id> --scope user`: the CLI does the edit, never this module;
- before an MCP removal its full user-scope config (command, args, env, headers) is saved to a private copy
  (`<config>/ai-resources/hygiene/`, mode 0600, created exclusively) and a redacted copy goes to the setup state
  (`tracking.hygiene_disabled`), so no secret value lands in the state file. If the private copy cannot be written the
  server is not removed;
- `ai-resources setup --restore-hygiene` puts every recorded item back (`claude mcp add-json`, `claude plugin enable`)
  and drops its record; an item that cannot be restored stays recorded and is said out loud;
- never run headless (no terminal, `--non-interactive`, an OpenClaw child session): AGENTS.md forbids prompts there.

A plugin's slash commands and hooks are not counted as usage (only its tool, skill and subagent calls are), which is why
every item is confirmed by the operator and not decided from the index alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import billing, usage_index
from . import ui

WINDOW_DAYS = 30
KIT_PLUGIN_NAMES = ("engram", "ai-resources")
KIT_MCP_NAMES = ("engram",)
REDACTED = "<redacted>"
_SECRET_KEY = re.compile(r"(key|token|secret|passw|auth|bearer|credential|cookie)", re.IGNORECASE)

Runner = Callable[[list[str]], "tuple[int, str]"]


def default_runner(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def settings_path() -> Path:
    from .cockpits import claude
    return claude.SETTINGS_PATH


def backup_dir() -> Path:
    from . import state
    return state.config_root() / "hygiene"


def is_headless() -> bool:
    """True when nothing can answer a prompt: no terminal, `--non-interactive`, or an OpenClaw-spawned session."""
    return ui.is_non_interactive() or not ui.stdin_is_a_terminal() or os.environ.get("OPENCLAW_CLI") == "1"


def is_kit_owned_mcp(name: str) -> bool:
    return name in KIT_MCP_NAMES or "ai-resources" in name or usage_index.plugin_of_server(name) in KIT_PLUGIN_NAMES


def is_kit_owned_plugin(plugin_id: str) -> bool:
    return plugin_id.split("@", 1)[0] in KIT_PLUGIN_NAMES


@dataclass
class Candidate:
    kind: str                      # "mcp" | "plugin"
    name: str                      # server name, or plugin id (`name@marketplace`)
    config: dict = field(default_factory=dict)


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def user_mcp_servers(claude_json: Path | None = None) -> dict[str, dict]:
    """The user-scope MCP servers (`mcpServers` at the top of ~/.claude.json). Read-only."""
    doc = _read_json(claude_json or billing.claude_json_path())
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    return {k: v for k, v in servers.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(servers, dict) else {}


def enabled_plugins(settings: Path | None = None) -> list[str]:
    doc = _read_json(settings or settings_path())
    table = doc.get("enabledPlugins") if isinstance(doc, dict) else None
    return sorted(k for k, v in table.items() if v is True and isinstance(k, str)) if isinstance(table, dict) else []


def redact(config: dict) -> dict:
    """A copy of an MCP config with every env and header value that looks secret replaced by a marker."""
    out = json.loads(json.dumps(config))
    for field_name in ("env", "headers"):
        table = out.get(field_name)
        if isinstance(table, dict):
            for k, v in table.items():
                if field_name == "headers" or _SECRET_KEY.search(str(k)):
                    table[k] = REDACTED
    return out


def has_redactions(config: dict) -> bool:
    return REDACTED in json.dumps(config)


def find_candidates(index: "usage_index.Index", recorded: list[dict] | None = None, *, window_days: int = WINDOW_DAYS,
                    claude_json: Path | None = None, settings: Path | None = None,
                    now: datetime | None = None) -> list[Candidate]:
    """User-level MCP servers and enabled plugins with no call in the window, minus the kit's own and the ones already
    disabled by an earlier run."""
    done = {(r.get("kind"), r.get("name")) for r in (recorded or []) if isinstance(r, dict)}
    out: list[Candidate] = []
    for name, cfg in sorted(user_mcp_servers(claude_json).items()):
        if is_kit_owned_mcp(name) or ("mcp", name) in done:
            continue
        if index.unused("server", name, window_days, now):
            out.append(Candidate("mcp", name, cfg))
    for pid in enabled_plugins(settings):
        if is_kit_owned_plugin(pid) or ("plugin", pid) in done:
            continue
        if index.unused("plugin", pid.split("@", 1)[0], window_days, now):
            out.append(Candidate("plugin", pid))
    return out


def _write_private(path: Path, text: str) -> None:
    """Create `path` exclusively with mode 0600 (never through a planted link, never over an existing file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")


def disable(c: Candidate, *, runner: Runner, now: datetime | None = None, directory: Path | None = None) -> dict | None:
    """Disable one candidate through the Claude Code CLI. Returns the record for the state, or None when nothing
    changed (the CLI refused, or the private copy of an MCP config could not be written)."""
    when = (now or datetime.now(timezone.utc)).isoformat()
    if c.kind == "plugin":
        rc, _out = runner(["claude", "plugin", "disable", c.name, "--scope", "user"])
        if rc != 0:
            return None
        return {"kind": "plugin", "name": c.name, "scope": "user", "previous": {"enabled": True}, "timestamp": when,
                "secrets_recoverable": True}
    directory = directory or backup_dir()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", c.name)
    backup = directory / f"mcp-{safe}-{_stamp(now)}.json"
    blob = json.dumps(c.config, indent=2, sort_keys=True)
    try:
        _write_private(backup, blob)
    except OSError:
        return None                                   # no private copy, no removal: the secrets could not be recovered
    rc, _out = runner(["claude", "mcp", "remove", c.name, "-s", "user"])
    if rc != 0:
        try:
            backup.unlink()
        except OSError:
            pass
        return None
    return {"kind": "mcp", "name": c.name, "scope": "user", "previous": redact(c.config), "backup": str(backup),
            "backup_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(), "timestamp": when,
            "secrets_recoverable": True}


def restore(record: dict, *, runner: Runner) -> tuple[bool, str]:
    """Put one recorded item back. (True, "") on success; (False, reason) leaves the record in place."""
    kind, name = record.get("kind"), str(record.get("name") or "")
    if not name:
        return False, "the record has no name"
    if kind == "plugin":
        rc, out = runner(["claude", "plugin", "enable", name, "--scope", "user"])
        return (True, "") if rc == 0 else (False, f"`claude plugin enable` failed: {out.strip()[:120]}")
    if kind != "mcp":
        return False, f"unknown kind {kind!r}"
    cfg = None
    backup = Path(str(record.get("backup") or "")) if record.get("backup") else None
    if backup is not None and backup.is_file():
        try:
            text = backup.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == record.get("backup_sha256"):
            try:
                cfg = json.loads(text)
            except ValueError:
                cfg = None
    if cfg is None:
        previous = record.get("previous")
        if isinstance(previous, dict) and not has_redactions(previous):
            cfg = previous
        else:
            return False, (f"the private copy of '{name}' is missing or changed, and the recorded config has redacted "
                           "values: re-add it by hand with `claude mcp add-json`")
    rc, out = runner(["claude", "mcp", "add-json", name, json.dumps(cfg), "-s", "user"])
    if rc != 0:
        return False, f"`claude mcp add-json` failed: {out.strip()[:120]}"
    if backup is not None:
        try:
            backup.unlink()                            # it held secrets; the item is back where it was
        except OSError:
            pass
    return True, ""


def restore_all(records: list[dict], *, runner: Runner, say: Callable[[str], None] = print) -> list[dict]:
    """Restore every record; returns the records that could not be restored (they stay in the state)."""
    left: list[dict] = []
    for rec in records:
        ok, why = restore(rec, runner=runner)
        if ok:
            say(f"Restored {rec.get('kind')} {rec.get('name')}")
        else:
            say(f"Could not restore {rec.get('kind')} {rec.get('name')}: {why}")
            left.append(rec)
    return left


def skip_reason(s, dry_run: bool = False) -> str | None:
    """None when the step should run; else why it does not (the step is then skipped without a line of output)."""
    if is_headless():
        return "headless"
    selected = (s.profile.customizations.get("__targets__") or {}).get("selected") or []
    if "claude" not in selected and not (s.cockpits.get("claude") and s.cockpits["claude"].installed):
        return "claude not selected"
    if shutil.which("claude") is None:
        return "claude not installed"
    return None


def run_step(s, *, dry_run: bool = False, runner: Runner | None = None, index: "usage_index.Index | None" = None,
             now: datetime | None = None) -> int:
    """The wizard step. Asks one question per candidate (default No); a dry run lists them and asks nothing."""
    runner = runner or default_runner
    idx = index if index is not None else usage_index.build(say=ui.info)
    found = find_candidates(idx, s.tracking.hygiene_disabled, now=now)
    if not found:
        ui.detail(f"Hygiene: no user-level MCP server or plugin is unused in the last {WINDOW_DAYS} days.")
        return 0
    ui.section(5, 5, "Hygiene - unused tools")
    ui.info(f"{len(found)} user-level tool(s) had no call in the last {WINDOW_DAYS} days. Each one you disable stops "
            "loading into every session; you can bring them back with `ai-resources setup --restore-hygiene`.")
    for c in found:
        label = f"MCP server '{c.name}'" if c.kind == "mcp" else f"plugin '{c.name}'"
        if dry_run:
            ui.detail(f"DRY RUN: would offer to disable {label} (no call in {WINDOW_DAYS} days).")
            continue
        extra = ""
        if c.kind == "mcp" and has_redactions(redact(c.config)):
            extra = " Its settings hold secret values: a private copy (mode 0600) is kept so it can be restored."
        if c.kind == "plugin":
            extra = " Its slash commands and hooks are not counted as usage."
        if not ui.confirm(f"Disable {label}? (no call in {WINDOW_DAYS} days){extra}", default=False):
            continue
        record = disable(c, runner=runner, now=now)
        if record is None:
            ui.warn(f"Could not disable {label}; nothing was changed.")
            continue
        s.tracking.hygiene_disabled.append(record)
        ui.ok(f"Disabled {label}")
        ui.record_action(f"disabled {c.kind} {c.name}")
    return 0


def cmd_restore(*, runner: Runner | None = None, say: Callable[[str], None] = print) -> int:
    """`ai-resources setup --restore-hygiene`: restore every recorded item and drop the records that worked."""
    from . import state
    runner = runner or default_runner
    records = list(state.load().tracking.hygiene_disabled)
    if not records:
        say("Nothing to restore: the kit has not disabled any MCP server or plugin.")
        return 0
    left = restore_all(records, runner=runner, say=say)
    keep = {json.dumps(r, sort_keys=True) for r in left}

    def mutate(fresh) -> None:
        fresh.tracking.hygiene_disabled = [r for r in fresh.tracking.hygiene_disabled
                                           if json.dumps(r, sort_keys=True) in keep]

    if not state.update(mutate):
        say("The setup state is busy: the items were restored but their records were not updated. Run it again.")
        return 1
    return 1 if left else 0
