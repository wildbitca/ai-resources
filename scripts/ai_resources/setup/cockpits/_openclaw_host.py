"""OpenClaw host section of `ai-resources setup`: this machine runs the gateway.

Everything the kit can do for an OpenClaw host is reachable from here, as opt-in questions in the
same shape as `_openclaw_voice`: a question in `prompt()`, an apply in `configure()`, a reversal
in `teardown()` and a line in `status_line()`. The logic itself lives in `openclaw_host` (which the
`ai-resources openclaw <verb>` commands wrap for headless runs and disaster recovery): one
implementation, two entry points.

    team narration   the Claude Code hook that reports a team's work into its Telegram topic
    guard            the hook that denies an undrained gateway stop (T01)
    units            the openclaw-* systemd units (backup, maintenance, watchdog, verify, health-restart)
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
* Setup never makes the gateway restart while it is live (ADR-0003). Config keys that OpenClaw
  restarts for are applied only after an explicit confirm, while no agent run is in flight, inside
  a drained window (watchdog.off, stop, drain, patch, start, health). Unattended runs, busy
  gateways and declined confirms record them as pending (`ai-resources openclaw apply-pending`).
* The host profile only FILLS keys the operator has not set; `kit-host-overrides.json5` opts a key
  back in. openclaw.json is only ever written through `openclaw config patch`.
* Secrets are never asked. The wizard points at `openclaw configure`.
* configure() is idempotent (a second run changes nothing) and teardown() removes exactly what
  configure() recorded, restoring whatever it replaced.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import state, ui
from ... import openclaw_host as host
from ... import openclaw_reload_rules as rr
from . import claude as claude_cockpit

RESTART_NOTE = ("Some keys only take effect after a gateway restart. Setup never makes the gateway restart while it "
                "is live: restart-required keys are applied only after an explicit confirm, while idle, inside a "
                "drained window (`ai-resources openclaw apply-pending`; `ai-resources openclaw doctor` drains too).")
NARRATION_CHOICES = (
    ("off", "Off — do not narrate the team into Telegram"),
    ("milestones", "Milestones — the request, the team start, each hand-off and the close (recommended)"),
    ("every-step", "Every step — also each edit, a periodic pulse and a live message per member"),
)


def applied_any(o: state.OpenClawState) -> bool:
    return bool(o.host_hooks_applied or o.host_units_applied or o.host_config_changes
                or o.host_workboard_applied == "enabled-by-kit" or o.host_agents_md_written
                or o.host_env_previous or o.host_env_created or o.bindings_applied)


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
            "BACKUP_DIR": o.host_backup_dir, "TAILNET": "1" if host.tailnet_available() else "0"}


# --- the questions ----------------------------------------------------------------------------------------

def prompt(s: state.SetupState, doc: dict | None = None) -> None:
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
    _ask_group_ids(o, doc or {})
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
        "Write an AGENTS.md into every agent workspace that has none (an existing file is never replaced; "
        "only the kit's marked block is refreshed in it)?",
        default=o.host_agents_md))
    o.host_check = bool(ui.confirm(
        "Check this host against the documented setup (node, openclaw install, unit, linger, backup dirs, "
        "MemoryHigh) and offer to fix what differs?", default=o.host_check))
    ui.detail("Secrets are not asked here: set them with `openclaw configure`.")


def _routed_agents(doc: dict) -> list[str]:
    """The agents that get their own Telegram group: not main (the catch-all) and not the worker."""
    entries = ((doc.get("agents") or {}).get("entries")) or {}
    return [a for a in entries if a != "main" and a not in host.ENGINE_OWNED_ENTRIES]


def _ask_group_ids(o: state.OpenClawState, doc: dict) -> None:
    """One question per routed agent: its Telegram group chat id.

    Pre-filled with the id already bound in the live config, so a re-run is Enter, Enter; an empty
    answer leaves that agent's binding alone. Nothing is asked without a terminal.
    """
    if ui.is_non_interactive():
        return
    live = host.group_binding_ids(doc)
    for aid in _routed_agents(doc):
        default = o.host_group_ids.get(aid) or (live.get(aid) or [""])[0]
        answer = (ui.text(f"Telegram group chat id for agent `{aid}` (a basic group, -5xxxxxxxxx; "
                          "empty: leave its binding alone):", default=default,
                          validate=_group_id_check) or "").strip()
        if not answer:
            continue
        if host.is_supergroup_id(answer):
            ui.warn(f"{answer} starts with -100: that is a supergroup or channel id, and a BASIC group "
                    "never has one. Converting a group to a supergroup changes its id and kills the "
                    "binding with no error (T35). Keeping the value you typed.")
        o.host_group_ids[aid] = answer


def _group_id_check(value: str):
    value = (value or "").strip()
    if not value:
        return True
    return True if host.validate_group_id(value) is None else host.validate_group_id(value)


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
              config_path: Path, run_changes: list[dict] | None = None) -> bool:
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
    changed = _configure_config(o, doc, config_path, written, dry_run=dry_run, apply_patch=apply_patch, oc=oc,
                                run_changes=run_changes)
    changed = _configure_workboard(o, doc, dry_run=dry_run, oc=oc) or changed
    changed = _configure_bindings(o, doc, config_path, written, dry_run=dry_run, apply_patch=apply_patch) or changed
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
    hand = host.hand_copy_files(dest)
    if not changed and not disabled and not hand:
        return
    summary = (f"{len(changed)} unit file(s) to write in {dest}"
               + (f", {len(disabled)} timer(s) to enable" if disabled else ""))
    if hand:
        summary += (f"; adopt the hand-installed health-restart timer (its files move to "
                    f"{Path.home() / '.openclaw' / 'backup' / 'hand-units'}/<timestamp>, nothing is deleted)")
    if dry_run:
        ui.detail("Would install the openclaw-* units: " + summary)
        return
    if hand and ui.is_non_interactive():
        ui.info("A hand-installed openclaw-health-restart timer is present: run setup interactively to adopt it "
                "(the kit's version never forces a restart over live runs).")
    if not _gate("Install the openclaw-* systemd units and enable their timers?",
                 detail=summary + ": " + ", ".join(changed)):
        return
    for name in changed:
        o.host_units_previous.setdefault(name, _unit_text(name, dest))
    result = host.install_units(dest, enable=True, runner=_runner(), adopt_hand_copy=True)
    o.host_units_applied = True
    for moved in result["adopted"]:
        # `adopted` lists the backup paths; the original is the hand copy path with the same file name.
        name = Path(moved).name
        original = (Path.home() / ".local" / "bin" / name) if name.endswith(".sh") else dest / name
        o.host_hand_moved[str(original)] = moved
    # Only the timers that were NOT enabled before are the kit's to disable again.
    for timer in result["enabled"]:
        if timer in disabled and timer not in o.host_timers_enabled:
            o.host_timers_enabled.append(timer)
    if result["adopted"]:
        ui.ok(f"Hand-installed health-restart copy moved to {Path(result['adopted'][0]).parent}")
    ui.ok(f"openclaw-* units installed ({len(result['changed'])} written, {len(result['enabled'])} timers enabled); "
          "systemd reloaded, the gateway was not restarted")


def _window_kwargs() -> dict:
    """Extra `drained_window` arguments (a seam for tests: a fake clock instead of real sleeps)."""
    return {}


def _oc_runner(oc: Callable[..., tuple[int, str]]) -> Callable[..., tuple[int, str]]:
    return lambda argv, **kw: oc(list(argv[1:]), timeout=kw.get("timeout", 30))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _in_flight() -> tuple[int | None, str]:
    """(runs in flight or None, text for the operator). None is an unreadable probe: treated as busy."""
    n = host.gateway_busy_strict(_runner())
    return n, ("unknown (treated as busy)" if n is None else str(n))


def _record_pending(o: state.OpenClawState, entries: list[dict]) -> None:
    known = {(e.get("op"), e.get("path"), e.get("source")) for e in o.host_restart_pending}
    for e in entries:
        if (e["op"], e["path"], e["source"]) not in known:
            o.host_restart_pending.append(e)


def _drained_apply(patch: dict, replace_paths: list[str],
                   apply_patch: Callable[..., tuple[bool, str]]) -> tuple[bool, bool]:
    """(written, window_ok). The confirmed path: dry run, then the patch inside a drained window."""
    code, written = host.apply_drained(patch, replace_paths, apply_patch=apply_patch, runner=_runner(),
                                       out=ui.detail, busy_probe=lambda: _in_flight()[0], **_window_kwargs())
    if code == host.EXIT_REFUSED_BUSY:
        ui.warn("not applied: agent runs started in flight before the window opened; deferred.")
    elif code == host.EXIT_APPLY_REJECTED:
        ui.error("OpenClaw rejected the restart-required patch in a dry run; nothing was stopped.")
    elif code == host.EXIT_REFUSED:
        ui.warn("not applied: another maintenance window is open (watchdog.off exists); deferred.")
    elif code:
        ui.error(f"the drained window ended with code {code} ({'config written; ' if written else ''}"
                 "check `ai-resources openclaw status`)")
    return written, code == host.EXIT_OK


def _handle_restart_part(o: state.OpenClawState, patch: dict, replace_paths: list[str], changes: list[dict], *,
                         source: str, apply_patch: Callable[..., tuple[bool, str]]) -> bool:
    """The restart-required half of a patch. True when it was written (inside a drained window).

    Never applied unattended, never while runs are in flight or the probe cannot tell, and only
    after a separate explicit confirm. Everything else is recorded as pending.
    """
    paths = rr.leaf_paths(patch)
    n, shown = _in_flight()
    ui.warn("needs a gateway restart: " + ", ".join(paths))
    ui.info(f"agent runs in flight: {shown}")
    pend = [{"op": "profile", "path": ".".join(c["path"]), "action": c.get("action", "filled"),
             "source": source, "at": _now()} for c in changes] if source == "setup" else \
           [{"op": "restore", "path": ".".join(c["path"]), "change": c, "source": source, "at": _now()}
            for c in changes if not (c.get("secret") and c.get("had"))]
    if ui.is_non_interactive():
        ui.warn("not applied: restart-required keys are never applied unattended; recorded as pending "
                "(`ai-resources openclaw apply-pending` when idle)")
        _record_pending(o, pend)
        return False
    if n is None or n > 0:
        ui.warn(f"not applied: {shown if n is None else n} agent run(s) in flight, and a restart would cut them. "
                "Recorded as pending; run `ai-resources openclaw apply-pending` when idle.")
        _record_pending(o, pend)
        return False
    if not ui.confirm("Apply in a drained window? The gateway will stop and start "
                      f"(N={n} runs in flight).", default=False):
        ui.info("Recorded as pending: `ai-resources openclaw apply-pending` applies it later.")
        _record_pending(o, pend)
        return False
    written, window_ok = _drained_apply(patch, replace_paths, apply_patch)
    if not written:
        _record_pending(o, pend)
    return written


def _configure_config(o: state.OpenClawState, doc: dict, path: Path, written: list[Path], *, dry_run: bool,
                      apply_patch: Callable[..., tuple[bool, str]],
                      oc: Callable[..., tuple[int, str]] | None = None,
                      run_changes: list[dict] | None = None) -> bool:
    if not o.host_config:
        return False
    try:
        overrides = host.load_host_overrides()
    except ValueError as e:
        # Never crash setup on a hand-edited file, and never fall back to "no overrides": that could
        # overwrite a value the operator meant to protect. Skip the step and say why.
        ui.error(f"OpenClaw config skipped: {e}")
        return False
    built = host.build_host_patch(host.load_host_profile(), doc, _values(o), overrides=overrides)
    if built["kept"]:
        ui.info("kept your value: " + ", ".join(built["kept"])
                + f" (opt back in with `force` in {host.host_overrides_path()})")
    for note in built["skipped"]:
        ui.detail(f"Not sent: {note}")
    for name in host.mcp_latest_findings(doc):
        ui.warn(f"mcp.servers.{name} runs an unpinned package (@latest). The kit will not rewrite it: pin the version by hand.")
    version = rr.installed_openclaw_version(_oc_runner(oc), refresh=True) if oc else None
    mode = rr.reload_mode_of(doc)
    reverted = _offer_legacy_revert(o, doc, version, mode, dry_run=dry_run, apply_patch=apply_patch)
    if not built["patch"]:
        return reverted
    if built["filled"]:
        ui.info("filled: " + ", ".join(built["filled"]))
    if built["forced"]:
        ui.info("replaced (forced by the profile or your overrides): " + ", ".join(built["forced"]))
    ui.info("Canonical config patch (sent with `openclaw config patch --stdin`):")
    ui.detail(json.dumps(built["patch"], indent=2))
    ok, out = apply_patch(built["patch"], dry_run=True, replace_paths=built["replace_paths"])
    if not ok:
        # Fail closed: a rejected dry run stops here. There is no per-key fallback.
        ui.error(f"OpenClaw rejected the config patch in a dry run; nothing was applied: {out[-400:]}")
        return reverted
    restart = rr.restart_paths(built["patch"], openclaw_version=version, reload_mode=mode)
    hot_patch, restart_patch = rr.split_patch(built["patch"], restart)
    hot_rp, restart_rp = rr.split_replace_paths(built["replace_paths"], restart)
    hot_changes, restart_changes = rr.split_changes(built["changes"], restart)
    if dry_run:
        ui.detail("Dry run accepted by OpenClaw; nothing applied.")
        if restart_patch:
            ui.detail("These keys need a gateway restart and would be pending: " + ", ".join(restart))
        return reverted

    applied = False
    if hot_patch and _gate("Apply this patch to the running gateway's config?",
                           detail=f"{len(hot_changes)} key(s) change; none of them restarts the gateway. "
                                  "OpenClaw validated it with --dry-run."):
        ok, out = apply_patch(hot_patch, replace_paths=hot_rp)
        if not ok:
            ui.error(f"OpenClaw rejected the config patch: {out[-400:]}")
        else:
            _record_changes(o, hot_changes, run_changes)
            applied = True
            ui.ok(f"OpenClaw config: {len(hot_changes)} key(s) applied")
    if restart_patch and _handle_restart_part(o, restart_patch, restart_rp, restart_changes, source="setup",
                                              apply_patch=apply_patch):
        _record_changes(o, restart_changes, run_changes)
        applied = True
        ui.ok(f"OpenClaw config: {len(restart_changes)} restart-required key(s) applied in a drained window")
    if applied:
        o.config_path = str(path)
        if path not in written:
            written.append(path)
        ui.warn(RESTART_NOTE)
    return applied or reverted


def _record_changes(o: state.OpenClawState, changes: list[dict], run_changes: list[dict] | None = None) -> None:
    known = {tuple(c["path"]) for c in o.host_config_changes}
    o.host_config_changes.extend(c for c in changes if tuple(c["path"]) not in known)
    if run_changes is not None:                      # what the post-setup watch may revert
        run_changes.extend(copy.deepcopy(c) for c in changes)


def _offer_legacy_revert(o: state.OpenClawState, doc: dict, version, mode, *, dry_run: bool,
                         apply_patch: Callable[..., tuple[bool, str]]) -> bool:
    """Offer to put back a restart-required value that setup 2.0.0 overwrote (default No, interactive only).

    A change recorded before the fill-only rule has no `action`. If the live value is still the
    one the profile writes, the operator never chose it: offer the recorded previous value back,
    through a drained window. Returns True when it was reverted.
    """
    if dry_run or ui.is_non_interactive():
        return False
    for ch in list(o.host_config_changes):
        if "action" in ch or not ch.get("had") or ch.get("secret"):
            continue
        dotted = ".".join(ch["path"])
        if not rr.restart_paths(_nest(ch["path"], 0), openclaw_version=version, reload_mode=mode):
            continue                      # a hot key left behind is harmless: nothing to offer
        live, had = host._get_path(doc, ch["path"])
        if not had or live == ch["previous"] or live != _profile_value(ch["path"], o):
            continue                      # gone, already back, or changed by the operator since
        n, shown = _in_flight()
        ui.warn(f"{dotted} is `{live}`, written by an earlier setup that replaced your `{ch['previous']}`. "
                "Setup no longer overwrites your values, but it cannot tell whether you want this one.")
        ui.info(f"agent runs in flight: {shown}")
        if n is None or n > 0:
            ui.info("Not offered now (a revert restarts the gateway in a drained window, and runs are in flight).")
            continue
        if not ui.confirm(f"Restore {dotted} to `{ch['previous']}` in a drained window? The gateway will stop and "
                          f"start (N={n} runs in flight).", default=False):
            continue
        patch, rp = host.restore_patch([ch])
        wrote, _ok = _drained_apply(patch, rp, apply_patch)
        if wrote:
            o.host_config_changes = [c for c in o.host_config_changes if c is not ch]
            return True
    return False


def _nest(path: list[str], value) -> dict:
    node: object = value
    for k in reversed(path):
        node = {k: node}
    return node  # type: ignore[return-value]


def _profile_value(path: list[str], o: state.OpenClawState):
    """The value the profile writes at `path` for this host (None when it writes none)."""
    built = host.build_host_patch(host.load_host_profile(), {}, _values(o), overrides=host.FORCE_ALL)
    cur: object = built["patch"]
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def apply_pending(s: state.SetupState, *, assume_yes: bool = False) -> int:
    """`ai-resources openclaw apply-pending`: apply the recorded restart-required keys in a drained window.

    Exit 0 done (or nothing pending), 1 refused or failed. Refuses while runs are in flight or the
    probe cannot tell; the keys stay pending. Values are re-derived from the profile and the
    overrides (or taken from the recorded inverse), so no secret was ever stored.
    """
    from . import openclaw as cockpit

    o = s.openclaw
    if not o.host_restart_pending:
        ui.ok("No restart-required config is pending.")
        return 0
    pending = list(o.host_restart_pending)
    ui.info("Pending restart-required keys: " + ", ".join(sorted({e["path"] for e in pending})))
    n, shown = _in_flight()
    ui.info(f"agent runs in flight: {shown}")
    if n is None or n > 0:
        ui.warn("Refusing: a restart would cut the runs in flight (or the probe cannot tell). Try again when idle.")
        return 1
    doc = cockpit.read_config(cockpit.config_path())
    patch: dict = {}
    rp: list[str] = []
    changes: list[dict] = []
    wanted = {e["path"] for e in pending if e.get("op") == "profile"}
    if wanted:
        try:
            ov = host.load_host_overrides()
        except ValueError as e:
            # A hand-edited overrides file: say which one and why (no contents), apply nothing.
            ui.error(f"apply-pending: {e}. Nothing was applied; the keys stay pending.")
            return 1
        forced = frozenset(e["path"] for e in pending if e.get("op") == "profile" and e.get("action") == "forced")
        built = host.build_host_patch(host.load_host_profile(), doc, _values(o),
                                      overrides={"keep": ov["keep"], "force": ov["force"] | forced})
        have = [p for p in rr.leaf_paths(built["patch"]) if p in wanted]
        _hot, only = rr.split_patch(built["patch"], have)
        patch = rr.deep_merge(patch, only)
        rp += rr.split_replace_paths(built["replace_paths"], have)[1]
        changes += [c for c in built["changes"] if ".".join(c["path"]) in wanted]
    restores = [e["change"] for e in pending if e.get("op") == "restore"]
    if restores:
        rpatch, rrp = host.restore_patch(restores)
        patch = rr.deep_merge(patch, rpatch)
        rp += rrp
    if not patch:
        ui.ok("Nothing left to apply: the pending keys already match.")
        o.host_restart_pending = []
        return 0
    ui.info("Will apply (keys only): " + ", ".join(rr.leaf_paths(patch)))
    if not assume_yes:
        if ui.is_non_interactive():
            ui.warn("Needs an interactive confirm, or pass --yes.")
            return 1
        if not ui.confirm("Apply in a drained window? The gateway will stop and start "
                          f"(N={n} runs in flight).", default=False):
            return 1
    written, window_ok = _drained_apply(patch, rp, cockpit.apply_patch)
    if not written:
        return 1
    _record_changes(o, changes)
    o.host_restart_pending = []
    ui.ok("Restart-required config applied in a drained window.")
    return 0 if window_ok else 1


# --- the post-setup watch (R3) ------------------------------------------------------------------------------

def stop_config_watch(*, forget: bool = False, o: state.OpenClawState | None = None) -> None:
    """Stop any `openclaw-config-watch-*` unit (a new run supersedes it; teardown cancels it).

    A watch that is inside its drained window (`watchdog.off` present) is never stopped: killing it
    would leave the gateway down. It stands down by itself at its next check, because the run id it
    watches is no longer the recorded one.
    """
    if host.Marker().exists():
        ui.info("a maintenance window is open (watchdog.off); leaving the post-setup watch to finish it")
        if forget and o is not None:
            o.config_watch = None
        return
    _runner()(["systemctl", "--user", "stop", host.WATCH_UNIT_PREFIX + "*"], env=host.systemd_env())
    if forget and o is not None:
        o.config_watch = None


def start_config_watch(s: state.SetupState, run_changes: list[dict]) -> bool:
    """Record this run's changes and start the bounded health watch as a transient user unit.

    Never a foreground loop (T39: an open tool call holds a gateway stop). When `systemd-run` or
    the user bus is missing, say so and carry on: setup is not blocked by its safety net.
    """
    import shutil
    import uuid

    o = s.openclaw
    if not run_changes:
        return False
    run_id = uuid.uuid4().hex[:8]
    unit = host.WATCH_UNIT_PREFIX + run_id
    cli = shutil.which("ai-resources")
    if not cli:
        ui.warn("post-setup watch not started: `ai-resources` is not on PATH; run `ai-resources openclaw status` "
                "in 10 minutes")
        return False
    o.config_watch = {"run_id": run_id, "unit": unit, "started_at": _now(), "changes": run_changes,
                      "done": False, "result": None}
    # The watch reads the state file from its very first tick: it must be on disk before the unit starts.
    state.save(s)
    rc, out = _runner()(["systemd-run", "--user", f"--unit={unit}", "--collect", "-p", "RuntimeMaxSec=1800",
                         "-p", "TimeoutStopSec=600",
                         cli, "openclaw", "config-watch", "--run-id", run_id], env=host.systemd_env(), timeout=30)
    if rc != 0:
        o.config_watch = None
        state.save(s)
        ui.warn(f"post-setup watch not started: {out.strip()[-160:] or 'systemd-run failed'}; run "
                "`ai-resources openclaw status` in 10 minutes")
        return False
    ui.info(f"Post-setup watch started ({unit}): if `openclaw health` fails 3 times within 10 minutes, this run's "
            f"changes are reverted.\n  follow:  journalctl --user -u {unit}\n  cancel:  systemctl --user stop {unit}")
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


def _digest_bindings(bindings) -> str:
    return _sha(json.dumps(bindings, sort_keys=True))


def _configure_bindings(o: state.OpenClawState, doc: dict, path: Path, written: list[Path], *,
                        dry_run: bool, apply_patch: Callable[..., tuple[bool, str]]) -> bool:
    """Bind each answered agent to its Telegram group in the root `bindings` array. True when it changed.

    Writes ONLY `bindings` (`channels.telegram.*` is the operator's: `assert_channels_safe` is
    unchanged). The array is rebuilt from the live one and sent whole with `--replace-path bindings`.
    """
    wanted = {a: c for a, c in o.host_group_ids.items() if a in _routed_agents(doc) and c}
    if not wanted:
        return False
    current = doc.get("bindings")
    new, notes = host.build_bindings(current, wanted)
    _report_allowlist_gaps(doc, wanted)
    if not notes:
        return False
    ui.info("Telegram group bindings (the root `bindings` array is replaced whole, main's catch-all stays last):")
    for note in notes:
        ui.detail(note)
    patch = {"bindings": new}
    # A passing --dry-run only proves the schema accepts the array. It is NOT evidence that Telegram
    # will route the group: an unlisted group is dropped with no log line (T35), and `topics["*"]`
    # validates and is then ignored. The evidence is a message arriving.
    ok, out = apply_patch(patch, dry_run=True, replace_paths=["bindings"])
    if not ok:
        ui.error(f"OpenClaw rejected the bindings patch in a dry run; nothing was applied: {out[-400:]}")
        return False
    if dry_run:
        ui.detail("Dry run accepted by OpenClaw; nothing applied.")
        return False
    if not _gate("Apply the group bindings to the running gateway's config?",
                 detail=f"{len(notes)} binding(s) change. OpenClaw validated it with --dry-run."):
        return False
    try:
        ok, out = apply_patch(patch, replace_paths=["bindings"])
    except rr.RestartRequired as e:
        ui.warn(f"bindings not applied: {e}. They need a gateway restart on this OpenClaw version; "
                "apply them by hand in a drained window.")
        return False
    if not ok:
        ui.error(f"OpenClaw rejected the bindings patch: {out[-400:]}")
        return False
    if not o.bindings_applied:
        o.bindings_previous = copy.deepcopy(current)
        o.bindings_applied = True
    o.bindings_written = _digest_bindings(new)
    o.config_path = str(path)
    if path not in written:
        written.append(path)
    ui.ok(f"OpenClaw bindings: {len(notes)} group(s) bound")
    return True


def _report_allowlist_gaps(doc: dict, wanted: dict[str, str]) -> None:
    """Say loudly which routed groups Telegram would drop without a log line. Reported, never patched."""
    for chat in host.allowlist_gaps(doc, sorted(set(wanted.values()))):
        agents = ", ".join(a for a, c in wanted.items() if c == chat)
        ui.warn(f"Telegram group {chat} ({agents}) is routed but is NOT in channels.telegram.groups, and "
                "groupPolicy is \"allowlist\": Telegram will DROP every message from it with no log line "
                "(T35). The kit never writes channels; run this yourself:\n    "
                + host.allowlist_fix_command(chat))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _configure_agents_md(o: state.OpenClawState, doc: dict, *, dry_run: bool) -> None:
    """A template AGENTS.md for every workspace that has none (an existing file is never replaced).

    The kit block itself is not written here: it is refreshed in every workspace, existing file or
    not, by `openclaw._configure_workspace_blocks`, whatever this question was answered.
    """
    if not o.host_agents_md:
        return
    # One file per resolved workspace, even when two agents (`claude`, `security`) share it. The
    # engine-owned worker never picks the template: it only gets the kit block, from the other writer.
    for ws, aids in _by_workspace(doc).items():
        template_aids = [a for a in aids if a not in host.ENGINE_OWNED_ENTRIES]
        if not template_aids or not ws.is_dir() or (ws / "AGENTS.md").exists():
            continue
        aid = "main" if "main" in aids else template_aids[0]
        kind = "orchestrator" if aid == "main" else host.detect_workspace_kind(ws, _runner())
        if dry_run:
            ui.detail(f"Would write {ws / 'AGENTS.md'} ({kind} template)")
            continue
        host.write_agents_md(ws, kind, aid)
        host.add_to_git_exclude(ws)
        o.host_agents_md_written[str(ws / "AGENTS.md")] = _sha((ws / "AGENTS.md").read_text(encoding="utf-8"))
        ui.ok(f"{ws / 'AGENTS.md'} written ({kind} template)")


def _by_workspace(doc: dict) -> dict[Path, list[str]]:
    grouped: dict[Path, list[str]] = {}
    for aid, paths in host.agent_workspaces(doc).items():
        for path in paths:
            grouped.setdefault(path, []).append(aid)
    return grouped


def teardown_workspace_blocks(o: state.OpenClawState) -> list[str]:
    """Undo the kit block in every workspace it was refreshed in. Returns the paths changed.

    A file the kit created holding only the block is deleted, unless somebody edited it since (a
    digest that no longer matches): then only the block goes. A file that existed before loses the
    block and keeps every other byte.
    """
    from . import _shared
    done: list[str] = []
    for path_str, digest in list(o.agent_kit_blocks.items()):
        p = Path(path_str)
        try:
            if digest and p.is_file() and _sha(p.read_text(encoding="utf-8")) == digest:
                p.unlink()
                done.append(path_str)
            elif _shared.remove_managed_block(p):
                done.append(path_str)
        except OSError:
            pass
        o.agent_kit_blocks.pop(path_str, None)
    return done


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
             oc: Callable[..., tuple[int, str]], doc: dict | None = None) -> bool:
    """Remove exactly what configure() recorded and restore what it replaced. True when finished."""
    o = s.openclaw
    ok = True
    if o.config_watch:
        stop_config_watch(forget=True, o=o)     # before restoring: a revert must not race the restore

    for path_str, digest in list(o.host_agents_md_written.items()):
        p = Path(path_str)
        try:
            if p.is_file() and _sha(p.read_text(encoding="utf-8")) == digest:
                p.unlink()
        except OSError:
            pass
        o.host_agents_md_written.pop(path_str, None)   # an edited file is the user's now

    if o.bindings_applied:
        ok = _restore_bindings(o, doc or {}, apply_patch) and ok

    if o.host_config_changes:
        for ch in o.host_config_changes:
            if ch.get("secret") and ch.get("had"):
                ui.warn(f"{'.'.join(ch['path'])} held a credential before the kit replaced it with a reference. "
                        "The old value was never stored, so it is not restored: set it again with `openclaw configure`.")
        ok = _teardown_config(o, apply_patch, oc, doc or {}) and ok

    if o.host_workboard_applied == "enabled-by-kit":
        rc, out = oc(["plugins", "disable", "workboard"])
        if rc == 0:
            o.host_workboard_applied = ""
        else:
            ui.error(f"Could not disable the workboard plugin: {out[-200:]}")
            ok = False
    else:
        o.host_workboard_applied = ""

    if o.host_units_applied or o.host_units_previous or o.host_timers_enabled or o.host_hand_moved:
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


def _restore_path(ch: dict) -> list[str]:
    return ch["path"] if ch["had"] else ch.get("delete", ch["path"])


def _teardown_config(o: state.OpenClawState, apply_patch: Callable[..., tuple[bool, str]],
                     oc: Callable[..., tuple[int, str]], doc: dict) -> bool:
    """Put the config leaves back. The hot half is applied as before; the restart-required half
    (the inverse of a `gateway.bind` change, say) goes through the same gate as a forward change:
    a drained window after a confirm while idle, otherwise pending. True when nothing failed."""
    patch, replace_paths = host.restore_patch(o.host_config_changes)
    if not patch:
        o.host_config_changes = []
        return True
    version = rr.installed_openclaw_version(_oc_runner(oc), refresh=True)
    restart = rr.restart_paths(patch, openclaw_version=version, reload_mode=rr.reload_mode_of(doc))
    hot, res = rr.split_patch(patch, restart)
    hot_rp, res_rp = rr.split_replace_paths(replace_paths, restart)
    if hot:
        done, out = apply_patch(hot, replace_paths=hot_rp)
        if not done:
            ui.error(f"OpenClaw host config teardown failed: {out[-300:]}")
            return False
    if res:
        res_changes = [c for c in o.host_config_changes
                       if ".".join(_restore_path(c)) in restart and not (c.get("secret") and c.get("had"))]
        _handle_restart_part(o, res, res_rp, res_changes, source="teardown", apply_patch=apply_patch)
    o.host_config_changes = []
    return True


def _restore_bindings(o: state.OpenClawState, doc: dict,
                      apply_patch: Callable[..., tuple[bool, str]]) -> bool:
    """Put the `bindings` array back as it was before the kit's first write (absent: removed).

    Only when the live array is still what the kit wrote: one the operator changed since is theirs,
    and is left alone with a warning rather than replaced by a stale snapshot. True when finished.
    """
    live = doc.get("bindings")
    if o.bindings_written and live is not None and _digest_bindings(live) != o.bindings_written:
        ui.warn("The root `bindings` array changed since the kit wrote it, so it is left as it is now. "
                "Remove the groups you no longer want with `openclaw config set bindings ...`.")
    else:
        previous = o.bindings_previous
        try:
            done, out = apply_patch({"bindings": previous},
                                    replace_paths=["bindings"] if isinstance(previous, list) else None)
        except rr.RestartRequired as e:
            ui.warn(f"bindings not restored: {e}. The array is left as it is now; restore it by hand.")
            return False
        if not done:
            ui.error(f"OpenClaw bindings teardown failed: {out[-300:]}")
            return False
    o.bindings_applied, o.bindings_previous, o.bindings_written = False, None, ""
    o.host_group_ids = {}
    return True


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
    for original, moved in list(o.host_hand_moved.items()):
        src, dst = Path(moved), Path(original)
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            host.shutil.move(str(src), str(dst))
        o.host_hand_moved.pop(original, None)
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


# --- verify (read-only) ---------------------------------------------------------------------------------------

_UNIT_STATES = ("enabled", "enabled-runtime", "disabled", "static", "linked", "linked-runtime", "masked",
                "masked-runtime", "indirect", "generated", "alias")
OFFBOX_REMEDY = ("add OPENCLAW_OFFBOX_LIST_CMD=<your listing command> to ~/.openclaw/kit-host.env "
                 "\u2014 the kit will not choose a destination for you (another host, S3, rclone are all yours to pick)")


class _Watch:
    """Wraps a runner and remembers which probes timed out (rc 124): "could not measure", not "broken"."""

    def __init__(self, runner: Callable[..., tuple[int, str]]):
        self.runner, self.timed_out = runner, []

    def __call__(self, argv, **kw):
        rc, out = self.runner(argv, **kw)
        if rc == 124:
            self.timed_out.append(list(argv))
            return 1, ""
        return rc, out

    def timed(self, *needles: str) -> bool:
        return any(all(n in argv for n in needles) for argv in self.timed_out)


def verify(ctx: dict, runner: Callable[..., tuple[int, str]] | None = None) -> list:
    """The host findings: unit, health, timers, backups, listeners and the off-box gap.

    Every call is a read (systemctl is-*/show, ss, loginctl, `openclaw health`, a directory listing);
    it never runs doctor and never stops or restarts the gateway. The one collector is
    `openclaw_host.collect_status(run_doctor=False)`, so status and verify cannot disagree.
    """
    from ...verify import Finding

    s = ctx["state"]
    watch = _Watch(runner or ctx.get("runner") or _runner())
    selection = s.get_selection() if hasattr(s, "get_selection") else None
    report = host.collect_status(watch, run_doctor=False, probe_offbox=False, selection=selection)
    who = "openclaw"
    unit = report["unit"]
    present = bool(unit["enabled"].split()) and unit["enabled"].split()[0] in _UNIT_STATES
    if not present and not s.openclaw.host and not watch.timed(host.GATEWAY_UNIT):
        return [Finding("ok", who, "no gateway unit on this machine; the host checks were skipped")]
    out: list[Finding] = []
    unit_timeout = watch.timed(host.GATEWAY_UNIT)
    gateway_up = unit["active"] == "active"
    if unit_timeout:
        out.append(Finding("warn", who, f"could not measure {host.GATEWAY_UNIT} (systemctl timed out)",
                           "re-run `ai-resources verify`"))
    elif not gateway_up or not unit["enabled"].startswith("enabled"):
        out.append(Finding("error", who, f"{host.GATEWAY_UNIT} is {unit['active']}, {unit['enabled']}",
                           "systemctl --user enable --now openclaw-gateway.service, or "
                           "`ai-resources openclaw doctor` to repair it safely"))
    else:
        out.append(Finding("ok", who, f"{host.GATEWAY_UNIT} is active and enabled"))
    # A down unit is one error, not a cascade: health answers nothing when nothing is running.
    if gateway_up:
        if watch.timed("health"):
            out.append(Finding("warn", who, "could not measure gateway health (`openclaw health` timed out)",
                               "re-run `ai-resources verify`"))
        elif not report["health"]["ok"]:
            out.append(Finding("error", who, "the gateway is running but `openclaw health` does not answer",
                               "`ai-resources openclaw doctor` (drain, fix, start, health)"))
        else:
            out.append(Finding("ok", who, "the gateway answers `openclaw health`"))
    for t in report["timers"]:
        if not t["enabled"]:
            out.append(Finding("warn", who, f"timer {t['name']} is not enabled",
                               f"systemctl --user enable --now {t['name']}"))
    daily = report["backups"]["daily"]
    if not daily["present"]:
        out.append(Finding("warn", who, "no daily backup found", "check openclaw-backup.timer and OPENCLAW_BACKUP_DIR"))
    elif daily["stale"]:
        out.append(Finding("warn", who, f"the newest daily backup is {daily['age_hours']:.0f}h old",
                           "run `ai-resources openclaw status` and check openclaw-backup.timer"))
    if report["listeners"]["wildcard"]:
        out.append(Finding("warn", who, f"the gateway port {report['listeners']['port']} is bound to all interfaces (T04)",
                           "set gateway.bind to loopback or a tailnet address"))
    if not report["off-box"]["configured"]:
        out.append(Finding("warn", who, "no off-box backup listing is configured", OFFBOX_REMEDY))
    out += _stability_findings(report.get("stability") or {})
    out += _pending_findings(s.openclaw)
    out += _watchdog_unit_findings()
    out += _hand_copy_findings()
    from ... import model_pins, models
    accounts = None
    if selection is not None and selection.enabled_providers() != ["anthropic"]:
        try:
            from ... import model_accounts
            accounts = model_accounts.detect(model_accounts.default_deps())
        except Exception:  # noqa: BLE001 - advisory only; a failing status call must not break verify
            accounts = None
    out += [Finding(level, who, message, remedy)
            for level, message, remedy in models.model_findings(report, model_pins.load_overlay(),
                                                               selection=selection, accounts=accounts)]
    return out


STABILITY_RECENT_DAYS = 7
_STABILITY_REMEDY = ("see T39 and the openclaw-operations skill; long work runs in the background "
                     "(kit block rule)")


def _stability_findings(s: dict) -> list:
    """T39: a recent stop that ran into its timeout while tool calls were still open. A warn at
    most: the stop is over, and the gateway being up is what verify's errors are about."""
    from ...verify import Finding

    if not s.get("present"):
        return []
    if s.get("error"):
        return [Finding("warn", "openclaw", f"the newest gateway stability bundle could not be read ({s['error']})",
                        "open it by hand under ~/.openclaw/logs/stability/; see T39")]
    age = s.get("age_hours")
    reason = s.get("reason", "")
    stalled = s.get("stalled", {})
    held = {k: v for k, v in stalled.items() if k in ("blocked_tool_call", "active_work_without_progress")}
    if age is None or age > STABILITY_RECENT_DAYS * 24 or not held \
            or not reason.endswith(("shutdown_timeout", "close_failed")):
        return []
    import datetime as dt
    day = dt.datetime.fromtimestamp(s["generated_at"], dt.timezone.utc).strftime("%Y-%m-%d")
    blocked = held.get("blocked_tool_call", 0)
    what = (f">={blocked} blocked tool calls ({', '.join(sorted(s.get('tools', {}))) or 'unnamed tools'})"
            if blocked else f">={sum(held.values())} stalled sessions")
    return [Finding("warn", "openclaw", f"gateway stop on {day} was held by {what}", _STABILITY_REMEDY)]


def _watchdog_unit_findings() -> list:
    """The installed watchdog unit must run the kit's script: an older host kept a copy under
    ~/.local/bin that never gets the kit's fixes. A file read; nothing is run."""
    from ...verify import Finding

    path = host.user_unit_dir() / "openclaw-watchdog.service"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    exec_line = next((ln for ln in text.splitlines() if ln.startswith("ExecStart=")), "")
    want = f"{host.kit_root()}/scripts/openclaw/openclaw-watchdog.sh"
    if exec_line.split()[-1:] == [want]:
        return []
    return [Finding("warn", "openclaw", f"the watchdog unit does not run the kit script ({exec_line or 'no ExecStart'})",
                    "`ai-resources openclaw install-units --dry-run`, then `install-units`")]


def _hand_copy_findings() -> list:
    """A hand-installed health-restart copy next to (or instead of) the kit's: it can force a restart
    over live runs (ADR-0003). Reads files only."""
    from ...verify import Finding

    try:
        hand = host.hand_copy_files()
    except (OSError, KeyError, ValueError):
        return []
    if not hand:
        return []
    return [Finding("warn", "openclaw",
                    "a hand-installed openclaw-health-restart copy is present (" + ", ".join(p.name for p in hand) + "); "
                    "it can restart the gateway over live runs and may run beside the kit's timer",
                    "run `ai-resources setup` interactively to adopt the kit's version (the hand files are moved to "
                    "~/.openclaw/backup/hand-units/, never deleted)")]


def _pending_findings(o: state.OpenClawState) -> list:
    """Restart-required config that setup (or a teardown, or the post-setup watch) did not apply, and
    the last watch's revert. Paths only, from setup-state; nothing is run."""
    from ...verify import Finding

    summary = host.pending_summary(o)
    out = []
    if summary.get("paths"):
        out.append(Finding("warn", "openclaw", host.pending_lines({"paths": summary["paths"]})[0].split("; run")[0],
                           f"run `{host.PENDING_COMMAND}` when no agent run is in flight"))
    w = summary.get("watch") or {}
    if w.get("reverted"):
        out.append(Finding("warn", "openclaw", "the post-setup watch reverted: " + ", ".join(w["reverted"]),
                           "the gateway stopped answering after the last `ai-resources setup`; check "
                           "`ai-resources openclaw status` before running setup again"))
    if w.get("problems"):
        out.append(Finding("error", "openclaw", "the post-setup watch could not finish its revert: " + "; ".join(w["problems"]),
                           f"`{host.PENDING_COMMAND}` retries the restart-required part when idle"))
    return out
