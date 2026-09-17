"""OpenClaw MCP servers: give the bot the same MCP servers as the user's Claude Code.

OpenClaw starts Claude Code with `--strict-mcp-config`, so only `mcp.servers` in openclaw.json
reach the bot. This module collects what Claude Code loads at user level, turns it into
OpenClaw entries and applies the ones the user picks:

    ~/.claude.json            user-scope `mcpServers` (project scopes are skipped: the bot
                              runs in its own workspace)
    ~/.claude/settings.json   `mcpServers`
    enabled plugins           each plugin's `.mcp.json`
    claude.ai connectors      cannot be exported; the ones with a public MCP endpoint are
                              pointed at it, the rest are reported

A server is never mirrored with a literal credential in it: openclaw.json is not a secret
store. `${VAR}` references are copied as they are (OpenClaw resolves them from the gateway's
environment); anything that looks like an inline secret marks the server as needing attention,
with advice that names the variable to use and never the value. Nothing here prints a value.

HTTP servers get `auth: oauth`; authorising them needs a browser, so setup lists the
`openclaw mcp login` commands instead of running them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from .. import state, ui

Runner = Callable[..., tuple[int, str]]

# claude.ai connector name → (OpenClaw server name, public MCP endpoint).
CONNECTORS = {"claude.ai ClickUp": ("clickup", "https://mcp.clickup.com/mcp")}
# Declared by the engine step (see openclaw.build_patch), which owns its snapshot.
MANAGED_ELSEWHERE = {"engram": "declared by the kit's engine setup"}

REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
                         r"CREDENTIAL|AUTH", re.IGNORECASE)
SECRET_PREFIXES = ("glsa_", "sk-", "sk_live_", "sk_test_", "rk_live_", "rk_test_", "ghp_", "gho_",
                   "ghs_", "ghu_", "github_pat_", "sbp_", "glpat-", "xoxb-", "xoxp-", "AKIA",
                   "eyJ")
ENV_FLAGS = ("-e", "--env")
BUSY = "did not stabilize"

ACTION_LABELS = {
    "add": "add",
    "update": "update",
    "adopt": "adopt (already identical)",
    "unchanged": "up to date",
    "remove": "remove (gone from Claude Code)",
}
WRITES = ("add", "update", "remove")
ACTIONABLE = ("add", "update", "adopt", "remove")


@dataclass
class Source:
    name: str
    kind: str       # stdio | http | connector | unsupported
    origin: str     # where Claude Code declares it, for the report
    spec: dict      # the Claude Code definition; never printed


@dataclass
class Item:
    name: str
    kind: str
    origin: str
    action: str     # add | update | adopt | unchanged | remove | skip | attention
    reason: str = ""
    entry: dict | None = None


# --- collection --------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _kind(spec: dict) -> str:
    if spec.get("type") in ("http", "sse") or ("url" in spec and "command" not in spec):
        return "http"
    if spec.get("type") in (None, "stdio") and spec.get("command"):
        return "stdio"
    return "unsupported"


def _expand_root(value: Any, root: str) -> Any:
    if isinstance(value, str):
        return value.replace("${CLAUDE_PLUGIN_ROOT}", root)
    if isinstance(value, list):
        return [_expand_root(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _expand_root(v, root) for k, v in value.items()}
    return value


def _servers_of(data: dict) -> dict:
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _plugin_sources(home: Path, settings: dict) -> list[Source]:
    installed = _read_json(home / ".claude/plugins/installed_plugins.json").get("plugins") or {}
    out: list[Source] = []
    for plugin_id, enabled in (settings.get("enabledPlugins") or {}).items():
        installs = installed.get(plugin_id) or []
        if enabled is not True or not installs:
            continue
        install = next((i for i in installs if i.get("scope") == "user"), installs[0])
        root = install.get("installPath") or ""
        if not root:
            continue
        mcp_json = _read_json(Path(root) / ".mcp.json")
        servers = _servers_of(mcp_json) or {k: v for k, v in mcp_json.items() if isinstance(v, dict)}
        manifest = _servers_of(_read_json(Path(root) / ".claude-plugin/plugin.json"))
        for name, spec in {**manifest, **servers}.items():
            if isinstance(spec, dict):
                spec = _expand_root(spec, root)
                out.append(Source(name, _kind(spec), f"plugin {plugin_id}", spec))
    return out


def collect(home: Path | None = None) -> list[Source]:
    """Every user-level MCP server Claude Code loads, in Claude Code's order of precedence."""
    home = home or Path.home()
    claude_json = _read_json(home / ".claude.json")
    settings = _read_json(home / ".claude/settings.json")
    out: list[Source] = []
    for origin, data in (("~/.claude.json", claude_json), ("~/.claude/settings.json", settings)):
        for name, spec in _servers_of(data).items():
            if isinstance(spec, dict):
                out.append(Source(name, _kind(spec), origin, spec))
    out.extend(_plugin_sources(home, settings))
    for connector in claude_json.get("claudeAiMcpEverConnected") or []:
        if connector in CONNECTORS:
            name, url = CONNECTORS[connector]
            out.append(Source(name, "http", f"{connector} connector", {"type": "http", "url": url}))
        elif isinstance(connector, str):
            out.append(Source(connector, "connector", "claude.ai connector", {}))
    return out


# --- secret hygiene ----------------------------------------------------------------

def _is_ref(value: str) -> bool:
    return bool(REF.fullmatch(value.strip()))


def looks_secret(value: str) -> bool:
    """A literal that reads like a credential. `${VAR}` references never do."""
    v = value.strip()
    if not v or _is_ref(v):
        return False
    if v.lower().startswith(("bearer ", "basic ", "token ")) and not REF.search(v):
        return True
    if v.startswith(SECRET_PREFIXES):
        return True
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", v, re.IGNORECASE))


def _var(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def _literal_secret(key: str, value: str) -> bool:
    if not isinstance(value, str) or not value or REF.search(value):
        return False
    return bool(SECRET_NAME.search(key)) or looks_secret(value)


def _move_to_env(var: str) -> str:
    return f'set env {var}: "${{{var}}}" in Claude Code\'s config and export {var}'


def stdio_problems(name: str, spec: dict) -> list[str]:
    problems: list[str] = []
    for key, value in (spec.get("env") or {}).items():
        if _literal_secret(key, value):
            problems.append(f"env {key} holds a literal value; {_move_to_env(key)}")
    args = [a for a in (spec.get("args") or []) if isinstance(a, str)]
    for i, arg in enumerate(args):
        pos = f"arg {i + 1}"
        prev = args[i - 1] if i else ""
        if "=" in arg:
            key, value = arg.split("=", 1)
            if _literal_secret(key, value):
                var = _var(key)
                if prev in ENV_FLAGS:
                    problems.append(f"{pos} passes {var} with a literal value; pass `{prev} {var}` "
                                    f"(name only) and {_move_to_env(var)}")
                else:
                    problems.append(f"{pos} ({key}) holds a literal secret; use \"${{{var}}}\" "
                                    f"and export {var}")
                continue
        if prev.startswith("-") and "=" not in prev and SECRET_NAME.search(prev) \
                and not arg.startswith("-") and not REF.search(arg):
            var = _var(f"{name}_{prev}")
            problems.append(f"{pos} (value of {prev}) is a literal secret; use \"${{{var}}}\" "
                            f"and export {var}")
        elif looks_secret(arg):
            var = _var(f"{name}_token")
            problems.append(f"{pos} looks like a literal token; move it to an env var such as {var}")
    return problems


def http_problems(name: str, spec: dict) -> list[str]:
    problems: list[str] = []
    url = spec.get("url") or ""
    if looks_secret(url):
        problems.append("the URL embeds credentials; use an env var instead")
    for key, value in parse_qsl(urlsplit(url).query):
        if _literal_secret(key, value):
            var = _var(key)
            problems.append(f"URL parameter {key} holds a literal secret; use \"${{{var}}}\" "
                            f"and export {var}")
    for key, value in (spec.get("headers") or {}).items():
        if _literal_secret(key, value) or (key.lower() == "authorization" and isinstance(value, str)
                                           and not REF.search(value)):
            var = _var(f"{name}_{key}")
            problems.append(f"header {key} holds a literal secret; use \"${{{var}}}\" "
                            f"and export {var}")
    if spec.get("headersHelper"):
        problems.append("uses headersHelper, which OpenClaw has no equivalent for")
    return problems


def build_entry(src: Source) -> tuple[dict | None, list[str]]:
    """The OpenClaw mcp.servers entry for `src`, or the reasons it cannot be mirrored safely."""
    spec = src.spec
    if src.kind == "stdio":
        problems = stdio_problems(src.name, spec)
        entry: dict[str, Any] = {"command": spec["command"]}
        if spec.get("args"):
            entry["args"] = list(spec["args"])
        if spec.get("env"):
            entry["env"] = dict(spec["env"])
    else:
        problems = http_problems(src.name, spec)
        entry = {"url": spec.get("url", ""),
                 "transport": "sse" if spec.get("type") == "sse" else "streamable-http"}
        if spec.get("headers"):
            entry["headers"] = dict(spec["headers"])
        else:
            entry["auth"] = "oauth"
    return (None, problems) if problems else (entry, [])


def env_refs(entries: list[dict]) -> list[str]:
    """Names of the `${VAR}` references in `entries`, sorted."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(REF.findall(value))
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)

    for entry in entries:
        walk(entry)
    return sorted(found)


# --- plan ----------------------------------------------------------------------------

def plan(sources: list[Source], servers: dict, mirrored: dict) -> list[Item]:
    """What mirroring would do to OpenClaw's `servers`, given the kit's earlier `mirrored` record.

    A server the user defined in OpenClaw is never overwritten: when it already matches it is
    adopted, otherwise it is left alone. A server the kit wrote is updated only while it still
    holds what the kit wrote.
    """
    items: list[Item] = []
    seen: dict[str, str] = {}
    for src in sources:
        item = Item(src.name, src.kind, src.origin, "skip")
        items.append(item)
        if src.name in seen:
            item.reason = f"same name as the server from {seen[src.name]}"
            continue
        seen[src.name] = src.origin
        if src.name in MANAGED_ELSEWHERE:
            item.reason = MANAGED_ELSEWHERE[src.name]
            continue
        if src.kind == "connector":
            item.reason = "claude.ai connector with no public MCP endpoint; cannot be mirrored"
            continue
        if src.kind == "unsupported":
            item.reason = "transport OpenClaw cannot use"
            continue
        entry, problems = build_entry(src)
        if problems:
            item.action, item.reason = "attention", "; ".join(dict.fromkeys(problems))
            continue
        item.entry = entry
        current = servers.get(src.name)
        record = mirrored.get(src.name)
        if record is not None:
            if current != record.get("applied"):
                item.reason = "changed in OpenClaw by hand since the kit wrote it; left alone"
            else:
                item.action = "unchanged" if current == entry else "update"
        elif current is None:
            item.action = "add"
        elif current == entry:
            item.action = "adopt"
        else:
            item.reason = "already defined in OpenClaw with different settings; left alone"

    for name, record in mirrored.items():
        if name in seen:
            continue
        current = servers.get(name)
        if current == record.get("applied"):
            items.append(Item(name, "", "kit", "remove"))
        else:
            items.append(Item(name, "", "kit", "skip",
                              reason="gone from Claude Code, but changed in OpenClaw by hand"))
    return items


# --- OpenClaw writes --------------------------------------------------------------------

def run_with_retry(run: Runner, args: list[str], stdin: str | None = None,
                   attempts: int = 4) -> tuple[int, str]:
    """OpenClaw's CLI sometimes fails while its state database settles; that is worth a retry."""
    rc, out = run(args, stdin=stdin)
    for attempt in range(1, attempts):
        if rc == 0 or BUSY not in out:
            break
        time.sleep(2 ** attempt)
        rc, out = run(args, stdin=stdin)
    return rc, out


def _patch(run: Runner, servers: dict, *, dry_run: bool = False) -> tuple[int, str]:
    args = ["config", "patch", "--stdin"]
    for name, value in servers.items():
        if value is not None:
            args += ["--replace-path", f"mcp.servers.{name}"]
    if dry_run:
        args.append("--dry-run")
    return run_with_retry(run, args, json.dumps({"mcp": {"servers": servers}}))


def apply(items: list[Item], s: state.SetupState, run: Runner, *, dry_run: bool = False) -> bool:
    """Write the selected changes in one validated patch and record them for teardown."""
    chosen = [i for i in items if i.action in ACTIONABLE and i.name not in s.openclaw.mcp_skipped]
    mirrored = s.openclaw.mcp_mirrored
    servers: dict[str, Any] = {}
    for item in chosen:
        if item.action in ("add", "update"):
            servers[item.name] = item.entry
        elif item.action == "remove":
            servers[item.name] = mirrored[item.name].get("previous")
    if servers:
        rc, out = _patch(run, servers, dry_run=dry_run)
        if rc != 0:
            ui.error(f"OpenClaw rejected the MCP servers patch: {out[-400:]}")
            return False
    if dry_run:
        return True
    for item in chosen:
        if item.action == "add":
            mirrored[item.name] = {"previous": None, "applied": item.entry}
        elif item.action == "adopt":
            mirrored[item.name] = {"previous": item.entry, "applied": item.entry}
        elif item.action == "update":
            mirrored[item.name] = {**mirrored[item.name], "applied": item.entry}
        elif item.action == "remove":
            mirrored.pop(item.name, None)
    return True


def teardown(s: state.SetupState, servers: dict, run: Runner) -> bool:
    """Put back what each mirrored server replaced. A server edited by hand since is left alone."""
    patch: dict[str, Any] = {}
    for name, record in s.openclaw.mcp_mirrored.items():
        current = servers.get(name)
        if current != record.get("applied"):
            ui.warn(f"OpenClaw MCP {name}: changed by hand since the kit wrote it; left as it is.")
        elif current != record.get("previous"):
            patch[name] = record.get("previous")
    if patch:
        rc, out = _patch(run, patch)
        if rc != 0:
            ui.error(f"OpenClaw MCP servers teardown failed: {out[-400:]}")
            return False
    s.openclaw.mcp_mirrored = {}
    return True


# --- gateway environment and report ---------------------------------------------------------

def state_dir() -> Path:
    return Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw").expanduser()


def _dotenv_names(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    names = set()
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line and not line.startswith("#"):
            names.add(line.split("=", 1)[0].strip())
    return names


def gateway_process_env_names() -> set[str] | None:
    """Variable names in the running gateway service's environment; None when it cannot be read.

    Linux only (macOS does not expose another process's environment)."""
    if not sys.platform.startswith("linux"):
        return None
    pid = _gateway_pid()
    if not pid:
        return None
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    return {chunk.split(b"=", 1)[0].decode(errors="replace") for chunk in raw.split(b"\0") if chunk}


def _gateway_pid() -> int:
    """The systemd user service's main PID, else a process running `openclaw … gateway`."""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                            "openclaw-gateway.service"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and int(r.stdout.strip() or 0) > 0:
            return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"gateway" in argv and any(b"openclaw" in a for a in argv[:3]):
            return int(cmdline.parent.name)
    return 0


def gateway_env_names(doc: dict) -> tuple[set[str], bool]:
    """Names the gateway can resolve, and whether its live process environment was checked."""
    names = _dotenv_names(state_dir() / ".env")
    names |= _dotenv_names(Path.home() / ".config/openclaw/gateway.env")
    env_block = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    names |= {k for k, v in env_block.items() if isinstance(v, str)}
    names |= set((env_block.get("vars") or {}) if isinstance(env_block.get("vars"), dict) else {})
    process = gateway_process_env_names()
    return names | (process or set()), process is not None


def report_env(entries: list[dict], doc: dict) -> list[str]:
    """Warn about `${VAR}` references the gateway cannot resolve. Returns the missing names."""
    refs = env_refs(entries)
    if not refs:
        return []
    available, checked = gateway_env_names(doc)
    missing = [name for name in refs if name not in available]
    if not missing:
        return []
    who = "The OpenClaw gateway does not have" if checked else "Make sure the OpenClaw gateway has"
    ui.warn(f"{who}: {', '.join(missing)}. MCP servers that use them will not start.")
    ui.detail(f"Add each as NAME=value to {state_dir() / '.env'} (OpenClaw's global .env, "
              "read by the gateway service), then run: openclaw gateway restart")
    return missing


def login_commands(items: list[Item]) -> list[str]:
    return [f"openclaw mcp login {i.name}" for i in items
            if i.action in ("add", "update", "adopt") and (i.entry or {}).get("auth") == "oauth"]


def table(items: list[Item]) -> None:
    ui.info("Claude Code MCP servers and what mirroring does with each:")
    for item in items:
        label = ACTION_LABELS.get(item.action) or f"{item.action}: {item.reason}"
        ui.detail(f"{item.name:<24} {item.kind or '-':<10} {label}")


# --- setup entry points ------------------------------------------------------------------

ANSWERS = {
    "mirror": "Yes — mirror Claude Code's MCP servers into OpenClaw",
    "keep": "Keep OpenClaw's MCP servers as they are",
}


def default_answer(s: state.SetupState) -> str:
    if s.openclaw.mcp in ANSWERS:
        return s.openclaw.mcp
    # Never change a live bot's tools from an unattended first run.
    return "keep" if ui.is_non_interactive() else "mirror"


def prompt(s: state.SetupState, servers: dict, home: Path | None = None) -> None:
    items = plan(collect(home), servers, s.openclaw.mcp_mirrored)
    if not items:
        return
    table(items)
    s.openclaw.mcp = ui.select("Give the bot the same MCP servers as Claude Code?",
                               [ui.Choice(label, value=a) for a, label in ANSWERS.items()],
                               default=default_answer(s))
    if s.openclaw.mcp != "mirror":
        return
    actionable = [i for i in items if i.action in ACTIONABLE]
    if not actionable:
        return
    names = [i.name for i in actionable]
    selected = ui.checkbox(
        "MCP servers to mirror:",
        [ui.Choice(f"{i.name} — {ACTION_LABELS[i.action]}", value=i.name) for i in actionable],
        default=[n for n in names if n not in s.openclaw.mcp_skipped],
    ) or []
    kept = [n for n in s.openclaw.mcp_skipped if n not in names]
    s.openclaw.mcp_skipped = kept + [n for n in names if n not in selected]


def configure(s: state.SetupState, doc: dict, run: Runner, *, home: Path | None = None,
              dry_run: bool = False) -> bool:
    """Apply the saved answer. True when openclaw.json changed."""
    servers = (doc.get("mcp") or {}).get("servers") or {}
    items = plan(collect(home), servers, s.openclaw.mcp_mirrored)
    if not apply(items, s, run, dry_run=dry_run) or dry_run:
        return False

    chosen = [i for i in items if i.action in ACTIONABLE and i.name not in s.openclaw.mcp_skipped]
    done = {a: [i.name for i in chosen if i.action == a] for a in ACTIONABLE}
    summary = ", ".join(f"{a} {', '.join(n)}" for a, n in done.items() if n)
    ui.ok(f"OpenClaw MCP servers → {summary}" if summary else "OpenClaw MCP servers: up to date")
    for item in items:
        if item.action == "attention":
            ui.warn(f"MCP {item.name} not mirrored: {item.reason}")
        elif item.action == "skip":
            ui.detail(f"MCP {item.name} skipped: {item.reason}")

    live = [i.entry for i in items if i.entry and i.name in s.openclaw.mcp_mirrored]
    report_env(live, doc)
    logins = login_commands([i for i in chosen if i.action != "remove"])
    if logins:
        ui.info("Authorise the OAuth servers once (each opens a browser; skip any already done):")
        for cmd in logins:
            ui.detail(cmd)
    return any(i.action in WRITES for i in chosen)
