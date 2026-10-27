"""Gateway reconcile: the one drained restart that clears what a restart clears (R5/R7).

Three things leave the running gateway different from what setup wants, and one stop/start cycle fixes all of
them:

* it runs older kit plugin code than the kit on disk (`stale_reason`: path, version or time);
* its cgroup holds more MCP server children than `MCP_CHILD_WARN` (per-session engram / supabase stacks that
  nothing evicted: 216 against a threshold of 24 on the 2026-10-27 host);
* a restart-required plugin version is recorded as pending;
* the caller passes `extra_reasons` for a change only a restart applies (a key rewritten in gateway.systemd.env by
  `key_check`).

`reconcile` is used by `ai-resources setup`, by `ai-resources openclaw reconcile` and, read-only, by
`verify --live`. It never restarts on its own initiative: it asks `openclaw_host.drained_window` (restart lock,
`watchdog.off` held on every exit path, signal-safe, busy probe, T29 cgroup refusal) and reports the outcome.
Everything takes an injectable runner, so the tests drive it against a fake.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, NamedTuple

from . import openclaw_host as host

Runner = Callable[..., "tuple[int, str]"]

MCP_CHILD_WARN = 24
_MCP_SERVERS = ("supabase", "engram")
_MCP_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(" + "|".join(_MCP_SERVERS) + r")(?![A-Za-z0-9])")

# kit-host.env: the agents allowed to keep stdio MCP servers; every other agent id matching a deny glob loses them.
MCP_DENY_AGENTS_KEY = "OPENCLAW_MCP_DENY_AGENTS"
MCP_DENY_AGENTS_DEFAULT = "kit-role-*"

RECONCILE_COMMAND = "ai-resources openclaw reconcile"


class Reason(NamedTuple):
    kind: str          # "plugin-stale" | "mcp-overflow" | "env-changed"
    text: str


class Outcome(NamedTuple):
    code: int                  # 0 done or nothing to do; else an openclaw_host EXIT_* code of the window
    reasons: list              # list[Reason]
    deferred: bool             # the window was refused (marker, lock, busy, inside the cgroup): nothing was stopped
    restarted: bool
    lines: list                # what was said, in order


def count_mcp_children(control_group: str, *, cgroup_root: Path | None = None,
                       proc_root: Path | None = None) -> dict[str, int]:
    """Processes in the unit's cgroup tree whose command line names a known MCP server, per server."""
    cgroup_root = cgroup_root or host.CGROUP_ROOT
    proc_root = proc_root or host.PROC_ROOT
    unit_dir = cgroup_root / control_group.lstrip("/")
    counts: dict[str, int] = {}
    for pid in host._unit_pids(unit_dir, None):
        try:
            raw = (proc_root / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        if not cmd.strip().split(" ")[0] or "openclaw-gateway" in cmd.split(" ")[0]:
            continue
        m = _MCP_RE.search(cmd)
        if m:
            counts[m.group(1).lower()] = counts.get(m.group(1).lower(), 0) + 1
    return dict(sorted(counts.items()))


def gateway_control_group(runner: Runner) -> str:
    rc, out = runner(["systemctl", "--user", "show", host.GATEWAY_UNIT, "-p", "ControlGroup"], timeout=10)
    for line in (out or "").splitlines():
        k, _, v = line.partition("=")
        if rc == 0 and k.strip() == "ControlGroup":
            return v.strip()
    return ""


def gateway_mcp_children(runner: Runner, *, cgroup_root: Path | None = None,
                         proc_root: Path | None = None) -> "dict[str, int] | None":
    """MCP children under the gateway unit; None when it cannot be told (unit down, no systemd, unreadable)."""
    try:
        cg = gateway_control_group(runner)
        if not cg:
            return None
        return count_mcp_children(cg, cgroup_root=cgroup_root, proc_root=proc_root)
    except Exception:  # noqa: BLE001 - a read-only probe never fails its caller
        return None


def mcp_overflow(counts: "dict[str, int] | None", threshold: int = MCP_CHILD_WARN) -> bool:
    return bool(counts) and sum(counts.values()) > threshold


def restart_reasons(*, plugin_dir: str, runner: "Runner | None" = None, stale_reason: Callable[..., "str | None"],
                    mcp_counts: "dict[str, int] | None", threshold: int = MCP_CHILD_WARN) -> list[Reason]:
    """Why the gateway should be restarted now; [] when it should not. Pure given its inputs."""
    out: list[Reason] = []
    why = stale_reason(plugin_dir, runner) if runner else stale_reason(plugin_dir)
    if why:
        out.append(Reason("plugin-stale", "the gateway runs older kit plugin code than the kit on disk"
                          + (f" ({why} rule)" if why in ("path", "version", "time") else "")))
    if mcp_overflow(mcp_counts, threshold):
        per = ", ".join(f"{k} {v}" for k, v in (mcp_counts or {}).items())
        out.append(Reason("mcp-overflow", f"{sum((mcp_counts or {}).values())} MCP server child processes under "
                          f"{host.GATEWAY_UNIT} ({per}); threshold {threshold}"))
    return out


def deny_agent_globs(env: "dict[str, str] | None" = None) -> list[str]:
    """Agent-id globs that lose stdio MCP servers (kit-host.env `OPENCLAW_MCP_DENY_AGENTS`, comma separated;
    default `kit-role-*`, the short-lived team roles that multiply per workflow)."""
    raw = (host.read_host_env() if env is None else env).get(MCP_DENY_AGENTS_KEY, MCP_DENY_AGENTS_DEFAULT)
    return [g.strip() for g in raw.split(",") if g.strip()]


def reconcile(runner: Runner, *, plugin_dir: str, stale_reason: Callable[..., "str | None"],
              dry_run: bool = False, drained: "Callable[..., int] | None" = None,
              cgroup_root: Path | None = None, proc_root: Path | None = None,
              self_cgroup_path: "Path | str | None" = None, sleep: Callable[[float], None] = time.sleep,
              out: "Callable[[str], None] | None" = None, extra_reasons: "list[Reason] | None" = None,
              check_env: bool = False, **window: object) -> Outcome:
    """Decide, and when something is owed do ONE drained restart. Idempotent: a clean gateway is a no-op.

    The window refuses (never forces) when a maintenance window is open, another restart holds the lock, agent
    turns are in flight or the busy probe cannot tell, or this process is a child of the gateway (T29); the
    outcome is then `deferred` and nothing was stopped. After a restart the result is read back: health, the
    plugin version the gateway reports against the disk, the MCP child count."""
    lines: list[str] = []

    def say(msg: str) -> None:
        lines.append(msg)
        if out:
            out(msg)

    reasons = restart_reasons(plugin_dir=plugin_dir, stale_reason=stale_reason,
                              mcp_counts=gateway_mcp_children(runner, cgroup_root=cgroup_root, proc_root=proc_root))
    if check_env:
        from . import key_check
        why = key_check.env_newer_than_gateway(host.gateway_started_at())
        if why:
            reasons.append(Reason("env-changed", why))
    reasons += list(extra_reasons or [])      # e.g. "env-changed": a rewritten gateway.systemd.env is read at start
    if not reasons:
        return Outcome(0, [], False, False, lines)
    for r in reasons:
        say(f"restart owed: {r.text}")
    if dry_run:
        say(f"dry run: a drained restart would clear {len(reasons)} item(s): stop {host.GATEWAY_UNIT}, wait until "
            "its cgroup empties, start it, wait for `openclaw health`; refused (nothing stopped) while turns are in "
            "flight")
        return Outcome(0, reasons, False, False, lines)
    window_lines: list[str] = []
    run = drained or host.drained_window
    code = run(lambda: True, runner=runner, require_idle=True, sleep=sleep, out=window_lines.append,
               self_cgroup_path=self_cgroup_path, **window)
    for line in window_lines:
        say(line)
    if code in (host.EXIT_REFUSED, host.EXIT_REFUSED_BUSY, host.EXIT_REFUSED_SELF):
        say(f"restart deferred (nothing was stopped); run `{RECONCILE_COMMAND}` from a terminal when the gateway is idle")
        return Outcome(code, reasons, True, False, lines)
    if code:
        say(f"the drained restart did not complete (exit {code}); the gateway was started again if it could be, "
            f"`{RECONCILE_COMMAND}` resumes")
        return Outcome(code, reasons, False, False, lines)
    return Outcome(0, reasons, False, True, lines)


# --- what is left after it: the live checks `verify --live` and setup print -----------------------------------

_HOOK_SCRIPT = re.compile(r"/hooks/([\w.-]+\.py)")


def _scripts(command: object) -> set[str]:
    return set(_HOOK_SCRIPT.findall(str(command)))


def hooks_missing(ak_path: str, *, team: bool, guard: bool, settings_path: "Path | None" = None) -> list[str]:
    """`<event>:<script>` for every hook the kit should have registered in ~/.claude/settings.json and has not
    (compared by event and script name: the kit root may be spelled through opt/ or the Cellar)."""
    import json
    from .setup.cockpits import claude as claude_cockpit
    path = settings_path or claude_cockpit.SETTINGS_PATH
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError, AttributeError):
        hooks = {}
    if not isinstance(hooks, dict):
        hooks = {}
    wanted = {e: list(v) for e, v in claude_cockpit._kit_hooks(ak_path).items()}
    for event, entries in claude_cockpit._openclaw_hooks(ak_path, team=team, guard=guard).items():
        wanted.setdefault(event, []).extend(entries)
    missing: list[str] = []
    for event, entries in wanted.items():
        have: set[str] = set()
        for e in hooks.get(event) or []:
            for h in (e.get("hooks") or []) if isinstance(e, dict) else []:
                have |= _scripts(h.get("command") if isinstance(h, dict) else "")
        for e in entries:
            for h in e["hooks"]:
                for script in sorted(_scripts(h["command"]) - have):
                    missing.append(f"{event}:{script}")
    return missing
