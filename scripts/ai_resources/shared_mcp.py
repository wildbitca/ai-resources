"""Shared-token MCP servers (2.8.0): one token per service, stored once, used by every cockpit.

Sentry and Figma offer hosted MCP endpoints that need a browser OAuth sign-in per client, so OpenClaw, Claude Code
and agy would each need their own. Instead the kit keeps one personal token per service in
the kit's credential file `~/.config/ai-resources/.env` (mode 0600, never in setup state, never printed) and registers the same
local stdio server in each cockpit:

    {"command": "<ai-resources>", "args": ["mcp-run", "sentry"]}

`ai-resources mcp-run NAME` reads the token and execs the upstream server with it in its environment. No cockpit
config holds a secret, so the entry is identical everywhere and the agy mirror's "no env values" rule holds.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# name -> (npm package pinned to a version, extra args, the env var the server reads, what to paste)
SERVERS: dict[str, tuple[str, tuple[str, ...], str, str]] = {
    "sentry": ("@sentry/mcp-server@0.42.0", (), "SENTRY_ACCESS_TOKEN",
               "Sentry user auth token (sentry.io > Settings > Personal Tokens)"),
    "figma": ("figma-developer-mcp@0.13.2", ("--stdio",), "FIGMA_API_KEY",
              "Figma personal access token (figma.com > Settings > Security)"),
}


def tokens_path() -> Path:
    """The kit's credential file (`~/.config/ai-resources/.env`, mode 0600), which already holds the gateway keys."""
    from .setup import state
    return state.env_path()


def read_tokens() -> dict[str, str]:
    """The stored shared-MCP tokens by env var name."""
    from .setup import credentials
    stored = credentials.load_env()
    return {var: stored[var] for _, _, var, _ in SERVERS.values() if stored.get(var)}


def store_token(var: str, value: str) -> None:
    """Add or replace one token in the credential file (written atomically, mode 0600)."""
    from .setup import credentials
    if var not in {v for _, _, v, _ in SERVERS.values()} or not value or any(c in value for c in "\n\r\0 \"'"):
        raise ValueError(f"refusing a malformed value for {var}")
    credentials.update_env({var: value})


def launcher() -> str:
    """A command that survives kit upgrades: the `ai-resources` on PATH (Homebrew's bin link), else the bare name."""
    return shutil.which("ai-resources") or "ai-resources"


def entry(name: str) -> dict:
    return {"command": launcher(), "args": ["mcp-run", name]}


def configured() -> list[str]:
    """The servers whose token is stored, in a stable order."""
    tokens = read_tokens()
    return [n for n, (_, _, var, _) in SERVERS.items() if tokens.get(var)]


def entries() -> dict[str, dict]:
    return {n: entry(n) for n in configured()}


def is_shared_entry(spec: object) -> bool:
    """True for an entry the kit writes (any launcher path: a moved Homebrew prefix is still ours)."""
    return (isinstance(spec, dict) and isinstance(spec.get("args"), list) and len(spec["args"]) == 2
            and spec["args"][0] == "mcp-run" and spec["args"][1] in SERVERS
            and Path(str(spec.get("command", ""))).name == "ai-resources")


def _found_elsewhere(var: str, openclaw_env: Path | None = None) -> str:
    """A token the host already has: exported in this shell, or in the gateway's `~/.openclaw/.env`."""
    from .setup import credentials
    if os.environ.get(var):
        return os.environ[var]
    path = openclaw_env or Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw").expanduser() / ".env"
    return credentials._read_env_file(path).get(var, "") if path.is_file() else ""


def in_use_here() -> set[str]:
    """The shared servers some cockpit already defines (by name, e.g. OpenClaw's hosted OAuth `sentry`): only these are
    worth a token question. Best-effort: an unreadable config counts as defining none."""
    names: set[str] = set()
    try:
        from .setup.cockpits import _openclaw_mcp, openclaw
        names |= {src.name for src in _openclaw_mcp.collect()}
        # The config file itself, not `openclaw config file`: detection must not start the CLI.
        path = Path(os.environ.get("OPENCLAW_CONFIG_PATH") or _openclaw_mcp.state_dir() / "openclaw.json").expanduser()
        names |= set(((openclaw.read_config(path).get("mcp") or {}).get("servers") or {}))
    except Exception:   # noqa: BLE001 - detection only; never blocks setup
        pass
    return names & set(SERVERS)


def pending(s, in_use: set[str] | None = None) -> list[str]:
    """Servers worth handling now: no stored token, not asked before, and either defined by a cockpit or with the token
    already on the host (imported silently)."""
    stored = read_tokens()
    in_use = in_use_here() if in_use is None else in_use
    return [n for n, (_, _, var, _) in SERVERS.items()
            if not stored.get(var) and n not in s.tracking.shared_mcp_asked
            and (n in in_use or _found_elsewhere(var))]


def collect(s, *, dry_run: bool = False, openclaw_env: Path | None = None, in_use: set[str] | None = None) -> list[str]:
    """The setup step: store each shared-MCP token once. A token already on the host is imported without a question;
    a server some cockpit defines gets one password prompt (Enter skips it), asked only once ever
    (`tracking.shared_mcp_asked`). Returns the servers whose token is stored now."""
    from .setup import ui
    stored = read_tokens()
    in_use = in_use_here() if in_use is None else in_use
    for name, (_, _, var, hint) in SERVERS.items():
        if stored.get(var) or name in s.tracking.shared_mcp_asked:
            continue
        value = _found_elsewhere(var, openclaw_env)
        if not value and name not in in_use:
            continue
        if value:
            how = "found on this host"
        elif ui.is_non_interactive() or dry_run:
            continue
        else:
            value = ui.password(f"{hint}, shared by Claude Code, OpenClaw and agy (Enter to skip)").strip()
            how = "stored"
            s.tracking.shared_mcp_asked.append(name)
        if not value:
            ui.detail(f"{name} MCP: skipped (add {var} to {tokens_path()} and re-run setup to enable it)")
            continue
        if dry_run:
            ui.detail(f"DRY RUN: would import {var} ({how}) for the {name} MCP server")
            continue
        try:
            store_token(var, value)
        except ValueError as e:
            ui.warn(f"{name} MCP: {e}")
            continue
        stored[var] = value
        ui.ok(f"{name} MCP token {how} → {tokens_path()} (used through `ai-resources mcp-run {name}`)")
    return [n for n, (_, _, var, _) in SERVERS.items() if stored.get(var)]


def cmd_mcp_run(name: str) -> int:
    """`ai-resources mcp-run NAME`: exec the upstream server with its token. Never prints the token."""
    if name not in SERVERS:
        print(f"unknown shared MCP server {name!r} (known: {', '.join(SERVERS)})", file=sys.stderr)
        return 2
    package, extra, var, _ = SERVERS[name]
    token = read_tokens().get(var) or os.environ.get(var, "")
    if not token:
        print(f"no {var} stored: run `ai-resources setup` to add it ({tokens_path()})", file=sys.stderr)
        return 1
    npx = shutil.which("npx")
    if not npx:
        print("npx is not on PATH: install Node.js for the shared MCP servers", file=sys.stderr)
        return 1
    env = dict(os.environ, **{var: token})
    os.execvpe(npx, [npx, "-y", package, *extra], env)
    return 1   # not reached


def add_subparser(sub) -> None:
    p = sub.add_parser("mcp-run", help="Run a shared-token MCP server (sentry, figma) over stdio; cockpits call this")
    p.add_argument("server", help=f"one of: {', '.join(SERVERS)}")
    p.set_defaults(func=lambda a: cmd_mcp_run(a.server))
