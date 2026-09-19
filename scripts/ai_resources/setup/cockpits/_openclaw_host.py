"""OpenClaw host section of `ai-resources setup`: this machine runs the gateway.

Everything the kit can do for an OpenClaw host is reachable from here, as opt-in questions in the
same shape as `_openclaw_voice`: a question in `prompt()`, an apply in `configure()`, a reversal
in `teardown()` and a line in `status_line()`. The logic itself lives in `openclaw_host` (which the
`ai-resources openclaw <verb>` commands wrap for headless runs and disaster recovery): one
implementation, two entry points.

    team narration   the Claude Code hook that reports a team's work into its Telegram topic
    guard            the hook that denies an undrained gateway stop (T01)
    units            the ten openclaw-* systemd units (backup, maintenance, watchdog, verify)
    config           the canonical openclaw.json keys (profiles/openclaw-host.json5)
    workboard        `openclaw plugins enable workboard`
    AGENTS.md        a template for every agent workspace that has none
    checks           what `openclaw bootstrap` finds, and an offer to fix it

Rules this module keeps:

* Every answer defaults to no on a first run (narration off; only the workboard defaults to yes), and an unattended run (`--non-interactive`) never
  changes how a live host behaves: it re-applies only the LOCAL pieces (kit-host.env, hooks,
  AGENTS.md) that were already agreed to.
* Anything that mutates the running gateway (a config patch, the units, the workboard plugin,
  a bootstrap fix) is a separate confirm that prints the exact change, defaults to no, and is
  skipped without a terminal. The config patch is always validated with `--dry-run` first, and a
  rejected dry run stops there: there is no per-key fallback.
* Nothing here restarts the gateway or writes openclaw.json by hand. It reports "restart needed"
  and stops.
* Secrets are never asked. The wizard points at `openclaw configure`.
* configure() is idempotent (a second run changes nothing) and teardown() removes exactly what
  configure() recorded, restoring whatever it replaced.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .. import state, ui
from ... import openclaw_host as host
from . import claude as claude_cockpit

RESTART_NOTE = ("Restart needed for some keys. The kit never restarts the gateway: do it yourself in a "
                "maintenance window (`ai-resources openclaw doctor` drains it safely).")
NARRATION_CHOICES = (
    ("off", "Off — do not narrate the team into Telegram"),
    ("milestones", "Milestones — the request, the team start, each hand-off and the close (recommended)"),
    ("every-step", "Every step — also each edit, a periodic pulse and a live message per member"),
)


def applied_any(o: state.OpenClawState) -> bool:
    return bool(o.host_hooks_applied or o.host_units_applied or o.host_config_changes
                or o.host_workboard_applied == "enabled-by-kit" or o.host_agents_md_written
                or o.host_env_previous or o.host_env_created)


def _runner() -> Callable[..., tuple[int, str]]:
    """The subprocess runner for systemctl, git and the bootstrap probes (a seam for tests)."""
    return host.default_runner


def _gate(message: str, detail: str = "") -> bool:
    """Consent for a change to the running gateway: interactive, explicit, default no."""
    if ui.is_non_interactive():
        ui.info(f"Skipped (needs an interactive confirm): {message}")
        return False
    if detail:
        ui.detail(detail)
    return bool(ui.confirm(message, default=False))


def _values(o: state.OpenClawState) -> dict[str, str]:
    return {"DOMAIN": o.host_domain, "POD_CIDR": o.host_pod_cidr, "OWNER_TELEGRAM_ID": o.host_operator_id,
            "BACKUP_DIR": o.host_backup_dir}


# --- the questions ----------------------------------------------------------------------------------------

def prompt(s: state.SetupState) -> None:
    """Ask what to set up on this OpenClaw host. Called from step 7."""
    o = s.openclaw
    env = host.read_host_env()
    o.host = bool(ui.confirm(
        "Configure this machine as an OpenClaw host (team narration, gateway guard, systemd units, "
        "canonical config, workboard)? Nothing that touches the running gateway is applied without "
        "its own confirm.", default=o.host))
    if not o.host:
        return

    choice = ui.select("Narrate the Claude Code team's work into its Telegram topic?",
                       [ui.Choice(label, value=value) for value, label in NARRATION_CHOICES],
                       default=o.host_narration or env.get("OPENCLAW_NARRATION") or "off")
    o.host_narration = "" if choice == "off" else choice
    o.host_guard = bool(ui.confirm(
        "Install the gateway guard hook (denies `openclaw doctor --fix` and a plain gateway stop while "
        "the gateway is live; `ai-resources openclaw doctor` is the safe way)?",
        default=o.host_guard))
    o.host_units = bool(ui.confirm(
        "Install the openclaw-* systemd units (daily/weekly/monthly backup, weekly maintenance, "
        "watchdog, daily verify)?", default=o.host_units))
    o.host_config = bool(ui.confirm(
        "Apply the canonical OpenClaw config block (heartbeats, tools, logging, gateway origin, Telegram "
        "streaming)? You will see the patch and a dry-run result first.", default=o.host_config))

    if o.host_units or o.host_config:
        _ask_host_values(o, env)
    o.host_workboard = bool(ui.confirm(
        "Enable the workboard plugin (a shared task board for the agents)?", default=o.host_workboard))
    o.host_agents_md = bool(ui.confirm(
        "Write an AGENTS.md into every agent workspace that has none (existing files are never touched)?",
        default=o.host_agents_md))
    o.host_check = bool(ui.confirm(
        "Check this host against the documented setup (node, openclaw install, unit, linger, backup dirs, "
        "MemoryHigh) and offer to fix what differs?", default=o.host_check))
    ui.detail("Secrets are not asked here: set them with `openclaw configure`.")


def _ask_host_values(o: state.OpenClawState, env: dict[str, str]) -> None:
    def valid(key: str) -> Callable[[str], Any]:
        def check(value: str):
            problems = host.validate_host_values({key: value.strip()})
            return True if not problems else problems[0]
        return check

    o.host_operator_id = (ui.text("Your Telegram user id, for failure notices (empty: no notices):",
                                  default=o.host_operator_id or env.get("OPENCLAW_OWNER_TELEGRAM_ID", ""),
                                  validate=valid("OWNER_TELEGRAM_ID")) or "").strip()
    o.host_backup_dir = (ui.text("Where the backup tiers are written:",
                                 default=o.host_backup_dir or env.get("OPENCLAW_BACKUP_DIR", "/srv/openclaw-backups"),
                                 validate=valid("BACKUP_DIR")) or "").strip()
    if o.host_config:
        o.host_domain = (ui.text("Public host name of the control UI, without scheme (empty: skip those keys):",
                                 default=o.host_domain or env.get("OPENCLAW_PUBLIC_DOMAIN", ""),
                                 validate=valid("DOMAIN")) or "").strip()
        o.host_pod_cidr = (ui.text("CIDR of the ingress that reaches the gateway, e.g. 10.42.0.0/24 (empty: skip):",
                                   default=o.host_pod_cidr or env.get("OPENCLAW_POD_CIDR", ""),
                                   validate=valid("POD_CIDR")) or "").strip()


# --- apply ---------------------------------------------------------------------------------------------------

def configure(s: state.SetupState, doc: dict, ak_path: str, written: list[Path], *, dry_run: bool,
              apply_patch: Callable[..., tuple[bool, str]], oc: Callable[..., tuple[int, str]],
              config_path: Path) -> bool:
    """Apply what the user chose. True when openclaw.json changed."""
    o = s.openclaw
    if not o.host:
        if applied_any(o) and not dry_run and ui.confirm(
                "The kit configured this OpenClaw host earlier. Remove what it added and restore what it "
                "replaced?", default=False):
            teardown(s, apply_patch=apply_patch, oc=oc)
        # A teardown never counts as "openclaw.json changed": the caller only re-renders its own
        # AGENTS.md and reports on that flag, and the restore already ran its own patch.
        return False
    problems = host.validate_host_values(_values(o))
    if problems:
        ui.error("OpenClaw host: " + "; ".join(problems) + " — nothing applied.")
        return False

    _configure_env(o, dry_run=dry_run)
    _configure_hooks(s, ak_path, dry_run=dry_run)
    _configure_units(o, dry_run=dry_run)
    changed = _configure_config(o, doc, config_path, written, dry_run=dry_run, apply_patch=apply_patch)
    changed = _configure_workboard(o, doc, dry_run=dry_run, oc=oc) or changed
    _configure_agents_md(o, doc, dry_run=dry_run)
    _configure_check(o, dry_run=dry_run)
    return changed


def _configure_env(o: state.OpenClawState, *, dry_run: bool) -> None:
    """~/.openclaw/kit-host.env: the host values the scripts and the narration hook read."""
    wanted: dict[str, str | None] = {
        "OPENCLAW_OWNER_TELEGRAM_ID": o.host_operator_id or None,
        "OPENCLAW_BACKUP_DIR": o.host_backup_dir or None,
        "OPENCLAW_PUBLIC_DOMAIN": o.host_domain or None,
        "OPENCLAW_POD_CIDR": o.host_pod_cidr or None,
        "OPENCLAW_NARRATION": o.host_narration or None,
        "OPENCLAW_GUARD": "1" if o.host_guard else None,
    }
    current = host.read_host_env()
    todo = {k: v for k, v in wanted.items() if current.get(k) != v}
    if not todo:
        return
    if dry_run:
        ui.detail(f"Would update {host.HOST_ENV_PATH}: " + ", ".join(sorted(todo)))
        return
    existed = host.HOST_ENV_PATH.exists()
    for key in todo:
        o.host_env_previous.setdefault(key, current.get(key))
    if not existed:
        o.host_env_created = True
    host.write_host_env(todo)
    ui.ok(f"{host.HOST_ENV_PATH} updated ({', '.join(sorted(todo))})")


def _configure_hooks(s: state.SetupState, ak_path: str, *, dry_run: bool) -> None:
    o = s.openclaw
    team, guard = bool(o.host_narration), o.host_guard
    if not (team or guard) and not o.host_hooks_applied:
        return
    if not (s.cockpits.get("claude") or state.CockpitState()).installed:
        if team or guard:
            ui.warn("OpenClaw host: Claude Code is not installed, so the team hooks were not registered.")
        return
    if dry_run:
        ui.detail(f"Would register the OpenClaw hooks in {claude_cockpit.SETTINGS_PATH} "
                  f"(narration: {o.host_narration or 'off'}, guard: {'on' if guard else 'off'})")
        return
    # The hand-installed hyphenated registration is replaced when the team hook is wanted (two
    # copies would narrate every event twice). Record it first so teardown can put it back.
    legacy = claude_cockpit.legacy_openclaw_entries() if team else []
    if claude_cockpit.install_openclaw_hooks(ak_path, team=team, guard=guard):
        ui.ok(f"OpenClaw hooks registered in {claude_cockpit.SETTINGS_PATH}")
    if legacy and not claude_cockpit.legacy_openclaw_entries():
        known = [json.dumps(x, sort_keys=True) for x in o.host_legacy_hooks]
        o.host_legacy_hooks.extend(x for x in legacy if json.dumps(x, sort_keys=True) not in known)
        ui.info("Replaced your hand-installed openclaw-team-progress.py hook with the kit's; "
                "teardown puts it back.")
    o.host_hooks_applied = team or guard


def _unit_text(name: str, dest: Path) -> str | None:
    try:
        return (dest / name).read_text(encoding="utf-8")
    except OSError:
        return None


def _configure_units(o: state.OpenClawState, *, dry_run: bool) -> None:
    if not o.host_units:
        return
    dest = host.user_unit_dir()
    changed, disabled = host.units_state(dest, _runner())
    if not changed and not disabled:
        return
    summary = (f"{len(changed)} unit file(s) to write in {dest}"
               + (f", {len(disabled)} timer(s) to enable" if disabled else ""))
    if dry_run:
        ui.detail("Would install the openclaw-* units: " + summary)
        return
    if not _gate("Install the openclaw-* systemd units and enable their timers?",
                 detail=summary + ": " + ", ".join(changed)):
        return
    for name in changed:
        o.host_units_previous.setdefault(name, _unit_text(name, dest))
    result = host.install_units(dest, enable=True, runner=_runner())
    o.host_units_applied = True
    # Only the timers that were NOT enabled before are the kit's to disable again.
    for timer in result["enabled"]:
        if timer in disabled and timer not in o.host_timers_enabled:
            o.host_timers_enabled.append(timer)
    ui.ok(f"openclaw-* units installed ({len(result['changed'])} written, {len(result['enabled'])} timers enabled); "
          "systemd reloaded, the gateway was not restarted")


def _configure_config(o: state.OpenClawState, doc: dict, path: Path, written: list[Path], *, dry_run: bool,
                      apply_patch: Callable[..., tuple[bool, str]]) -> bool:
    if not o.host_config:
        return False
    built = host.build_host_patch(host.load_host_profile(), doc, _values(o))
    for note in built["skipped"]:
        ui.detail(f"Not sent: {note}")
    for name in host.mcp_latest_findings(doc):
        ui.warn(f"mcp.servers.{name} runs an unpinned package (@latest). The kit will not rewrite it: pin the version by hand.")
    if not built["patch"]:
        return False
    ui.info("Canonical config patch (sent with `openclaw config patch --stdin`):")
    ui.detail(json.dumps(built["patch"], indent=2))
    ok, out = apply_patch(built["patch"], dry_run=True, replace_paths=built["replace_paths"])
    if not ok:
        # Fail closed: a rejected dry run stops here. There is no per-key fallback.
        ui.error(f"OpenClaw rejected the config patch in a dry run; nothing was applied: {out[-400:]}")
        return False
    if dry_run:
        ui.detail("Dry run accepted by OpenClaw; nothing applied.")
        return False
    if not _gate("Apply this patch to the running gateway's config?",
                 detail=f"{len(built['changes'])} key(s) change. OpenClaw validated it with --dry-run."):
        return False
    ok, out = apply_patch(built["patch"], replace_paths=built["replace_paths"])
    if not ok:
        ui.error(f"OpenClaw rejected the config patch: {out[-400:]}")
        return False
    known = {tuple(c["path"]) for c in o.host_config_changes}
    o.host_config_changes.extend(c for c in built["changes"] if tuple(c["path"]) not in known)
    o.config_path = str(path)
    if path not in written:
        written.append(path)
    ui.ok(f"OpenClaw config: {len(built['changes'])} key(s) applied")
    ui.warn(RESTART_NOTE)
    return True


def _configure_workboard(o: state.OpenClawState, doc: dict, *, dry_run: bool,
                         oc: Callable[..., tuple[int, str]]) -> bool:
    if not o.host_workboard:
        return False
    enabled = (((doc.get("plugins") or {}).get("entries") or {}).get("workboard") or {}).get("enabled")
    if enabled is True:
        o.host_workboard_applied = o.host_workboard_applied or "already-enabled"
        return False
    if dry_run:
        ui.detail("Would run `openclaw plugins enable workboard`.")
        return False
    if not _gate("Enable the workboard plugin (`openclaw plugins enable workboard`)?"):
        return False
    rc, out = oc(["plugins", "enable", "workboard"])
    if rc != 0:
        ui.error(f"Could not enable the workboard plugin: {out[-300:]}")
        return False
    o.host_workboard_applied = "enabled-by-kit"
    ui.ok("workboard plugin enabled")
    ui.warn(RESTART_NOTE)
    return True


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _configure_agents_md(o: state.OpenClawState, doc: dict, *, dry_run: bool) -> None:
    if not o.host_agents_md:
        return
    for aid, entry in (((doc.get("agents") or {}).get("entries")) or {}).items():
        if aid in host.ENGINE_OWNED_ENTRIES or not (entry or {}).get("workspace"):
            continue
        ws = Path(entry["workspace"]).expanduser()
        if not ws.is_dir() or (ws / "AGENTS.md").exists():
            continue
        kind = "orchestrator" if aid == "main" else host.detect_workspace_kind(ws, _runner())
        if dry_run:
            ui.detail(f"Would write {ws / 'AGENTS.md'} ({kind} template)")
            continue
        host.write_agents_md(ws, kind, aid)
        host.add_to_git_exclude(ws)
        o.host_agents_md_written[str(ws / "AGENTS.md")] = _sha((ws / "AGENTS.md").read_text(encoding="utf-8"))
        ui.ok(f"{ws / 'AGENTS.md'} written ({kind} template)")


def _configure_check(o: state.OpenClawState, *, dry_run: bool) -> None:
    if not o.host_check:
        return
    skip = ("units",) if o.host_units else ()
    results, _ctx = host.bootstrap(_runner(), dry_run=True, backup_dir=o.host_backup_dir or None, skip=skip, out=ui.detail)
    pending = [r for r in results if r.status in ("would-change", "refused", "failed")]
    fixable = [r for r in results if r.status == "would-change"]
    if pending:
        ui.warn("OpenClaw host check: " + ", ".join(f"{r.step} ({r.status})" for r in pending))
    else:
        ui.ok("OpenClaw host check: everything matches the documented setup")
    if dry_run or not fixable:
        return
    if ui.is_non_interactive() or not ui.confirm(
            f"Apply the {len(fixable)} change(s) the check found? Each one asks again.", default=False):
        return
    host.bootstrap(_runner(), dry_run=False, backup_dir=o.host_backup_dir or None, skip=skip, out=ui.detail,
                   confirm=lambda step, what: bool(ui.confirm(f"{what}?", default=False)))


# --- teardown ------------------------------------------------------------------------------------------------

def teardown(s: state.SetupState, *, apply_patch: Callable[..., tuple[bool, str]],
             oc: Callable[..., tuple[int, str]]) -> bool:
    """Remove exactly what configure() recorded and restore what it replaced. True when finished."""
    o = s.openclaw
    ok = True

    for path_str, digest in list(o.host_agents_md_written.items()):
        p = Path(path_str)
        try:
            if p.is_file() and _sha(p.read_text(encoding="utf-8")) == digest:
                p.unlink()
        except OSError:
            pass
        o.host_agents_md_written.pop(path_str, None)   # an edited file is the user's now

    if o.host_config_changes:
        for ch in o.host_config_changes:
            if ch.get("secret") and ch.get("had"):
                ui.warn(f"{'.'.join(ch['path'])} held a credential before the kit replaced it with a reference. "
                        "The old value was never stored, so it is not restored: set it again with `openclaw configure`.")
        patch, replace_paths = host.restore_patch(o.host_config_changes)
        done, out = apply_patch(patch, replace_paths=replace_paths) if patch else (True, "")
        if done:
            o.host_config_changes = []
        else:
            ui.error(f"OpenClaw host config teardown failed: {out[-300:]}")
            ok = False

    if o.host_workboard_applied == "enabled-by-kit":
        rc, out = oc(["plugins", "disable", "workboard"])
        if rc == 0:
            o.host_workboard_applied = ""
        else:
            ui.error(f"Could not disable the workboard plugin: {out[-200:]}")
            ok = False
    else:
        o.host_workboard_applied = ""

    if o.host_units_applied or o.host_units_previous or o.host_timers_enabled:
        _remove_units(o)

    if o.host_hooks_applied or o.host_legacy_hooks:
        claude_cockpit.remove_openclaw_hooks()
        o.host_hooks_applied = False
        if o.host_legacy_hooks:
            claude_cockpit.restore_legacy_openclaw_hooks(o.host_legacy_hooks)
            o.host_legacy_hooks = []

    if o.host_env_previous or o.host_env_created:
        host.write_host_env(dict(o.host_env_previous))
        if o.host_env_created and not host.read_host_env():
            host.HOST_ENV_PATH.unlink(missing_ok=True)
        o.host_env_previous, o.host_env_created = {}, False
    return ok


def _remove_units(o: state.OpenClawState) -> None:
    """Undo the units: disable exactly the timers the kit enabled (a timer that was already
    enabled stays enabled), then put every unit file back as it was: its old text, or gone if
    the kit created it."""
    dest = host.user_unit_dir()
    runner = _runner()
    env = host.systemd_env()
    for timer in list(o.host_timers_enabled):
        runner(["systemctl", "--user", "disable", "--now", timer], env=env)
        o.host_timers_enabled.remove(timer)
    for name, previous in list(o.host_units_previous.items()):
        if previous is None:
            (dest / name).unlink(missing_ok=True)
        else:
            (dest / name).write_text(previous, encoding="utf-8")
        o.host_units_previous.pop(name, None)
    runner(["systemctl", "--user", "daemon-reload"], env=env)
    o.host_units_applied = False


# --- status --------------------------------------------------------------------------------------------------------

def status_line(s: state.SetupState) -> str:
    """One line for the end of step 9; "" when the kit configured nothing on this host."""
    o = s.openclaw
    if not o.host or not applied_any(o):
        return ""
    parts = []
    if o.host_hooks_applied:
        parts.append("narration " + (o.host_narration or "off") + (", guard" if o.host_guard else ""))
    if o.host_units_applied:
        parts.append("units")
    if o.host_config_changes:
        parts.append(f"config ({len(o.host_config_changes)} keys)")
    if o.host_workboard_applied:
        parts.append("workboard")
    if o.host_agents_md_written:
        parts.append(f"{len(o.host_agents_md_written)} AGENTS.md")
    return "OpenClaw host: " + (", ".join(parts) or "env only")
