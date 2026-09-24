---
name: openclaw-operations
description: "Use when touching ~/.openclaw, openclaw-*.service or .timer units, the OpenClaw gateway, its watchdog, control ui or Telegram bot, or when `openclaw doctor` warns. Covers stopping or repairing the gateway safely, telling doctor noise from signal, verifying backups, team narration into Telegram, and wiring host units and config through `ai-resources setup`. (triggers: openclaw, gateway, watchdog, watchdog.off, control ui, telegram bot, doctor --fix, kit-host.env, team narration)"
globs: ["**/.openclaw/**", "**/openclaw-*.service", "**/openclaw-*.timer", "**/kit-host.env"]
---

# OpenClaw operations

A host runs the OpenClaw gateway as the user systemd unit `openclaw-gateway.service`. The kit
reproduces the surrounding setup; this skill is the operating manual. Background and evidence:
`$AGENT_KIT/docs/openclaw/pitfalls.md` (T-numbers below) and `$AGENT_KIT/docs/openclaw/runbook.md`.

## Where the setup lives: `ai-resources setup` asks

`ai-resources setup` is the primary surface. In the OpenClaw section it asks, one opt-in question
each, and every answer defaults to no on a first run (the workboard defaults to yes):

- team narration (`off`, `milestones`, `every-step`), the gateway guard hook, the ten `openclaw-*`
  units, the canonical config block, the workboard plugin, an `AGENTS.md` for workspaces that
  have none, and a host check;
- host values (domain, operator Telegram id, ingress CIDR, backup dir), written to
  `~/.openclaw/kit-host.env`. Secrets are never asked: use `openclaw configure`.

Anything that touches the running gateway (config patch, units, workboard) is its own confirm,
prints the exact change, is skipped under `--non-interactive`, and never restarts the gateway; it
says "restart needed" and stops. A second run changes nothing; teardown removes only what the kit
recorded and restores what it replaced.

The subcommands are thin wrappers over the same functions, for headless runs and recovery:

| Command | Does |
|---|---|
| `ai-resources openclaw status` | one screen, never repairs |
| `ai-resources openclaw doctor` | the drained `doctor --fix` (below) |
| `ai-resources openclaw bootstrap [--dry-run] [--only STEP]` | eight idempotent host checks, fixes each after a confirm |
| `ai-resources openclaw install-units [--dry-run] [--enable]` | render/install the ten units |
| `ai-resources openclaw agent-new ID WORKSPACE` | workspace plus the right `AGENTS.md` |

Prefer `status` and `bootstrap --dry-run` first: they report and change nothing.

## Rules that prevent outages

1. **Drain before `doctor --fix` (T01).** `openclaw doctor --fix` stops the gateway and aborts if a
   child (claude, engram, npx) is still alive, leaving it DOWN, and `Restart=always` does not
   cover an explicit stop. Run `ai-resources openclaw doctor`: it pauses the watchdog, stops the
   unit, polls until the cgroup drains (`TasksCurrent` is `[not set]`; the stop itself can take
   ~5 min, `TimeoutStopSec=330`), runs `doctor --fix` only if drained, removes the marker, starts
   the unit and polls `openclaw health`. Exit codes: 2 marker already present (use `--force` only
   if you know the window is yours), 3 not drained, 4 doctor failed, 5 gateway did not answer.
   Never run bare `openclaw doctor --fix` as the normal path.
2. **`watchdog.off` is a maintenance window, not a habit.** `~/.openclaw/watchdog.off` pauses
   `openclaw-watchdog.service`. Whoever creates it removes it, on every exit path. Leaving it
   means the gateway is silently unguarded. If you find one you did not create, check for a live
   window (`ai-resources openclaw status`) before deleting it. The guard hook steps aside while it
   exists.
3. **Never restart or stop the gateway from a tool.** A Claude Code session started by the gateway
   is a child of the unit: restarting it kills your own session and any message in flight (T29).
   `openclaw gateway restart` and `systemctl --user stop|restart openclaw-gateway.service` are for
   the operator in a maintenance window. Report "restart needed" instead. The kit's
   `openclaw_gateway_guard.py` hook denies exactly `openclaw doctor --fix` and a plain gateway stop
   while the gateway is live and the watchdog is not paused; a deny aborts the WHOLE Bash call
   (T24), so if a file "is missing" after a deny it was never written: repeat the corrected call.
4. **Never write `~/.openclaw/openclaw.json` by hand.** Use `openclaw config set|patch`
   (`config patch --stdin --dry-run` first), or `ai-resources setup`, which sends the canonical
   block as one validated patch. It never touches `channels.*` except `channels.telegram.streaming`.
5. **Secrets stay out of files the kit writes.** The config uses a SecretRef for the GitHub token
   (`GH_TOKEN`); `setup-state.yaml` records no credential value; set credentials with
   `openclaw configure`.
6. **MCP servers stay pinned.** Never `@latest` in `mcp.servers`; setup reports it and does not
   rewrite it.

## Team narration into Telegram (T31)

OpenClaw drops Claude Code subagent events on purpose, so the kit ships a Claude Code hook,
`hooks/openclaw_team_progress.py`, that publishes them with `openclaw message send`.

- **Off is the default and is real.** It speaks only when `OPENCLAW_CLI=1` (a session started by
  the gateway, the normal state on a host) AND `~/.openclaw/kit-host.env` has
  `OPENCLAW_NARRATION=milestones` or `every-step`. A missing key, a missing file or an unknown
  value means it exits 0 without publishing or logging.
- To turn it off: answer `off` in `ai-resources setup` (removes the key). Do not just delete the
  hook registration by hand; a hand-installed `openclaw-team-progress.py` from before the kit is
  replaced by the kit's hook on setup and restored on teardown.
- It never publishes prompts, raw tool input or tool output. Edits are capped per member and other
  non-milestone messages by a sliding window per session; milestones are never dropped (T32). A member
  without a live message: `pgrep -af 'openclaw-team-watch.py --agent-id'` against the `watch-<id>` markers
  in `/run/user/$UID/openclaw-team-hook/` (the marker holds the watcher PID).

## Diagnosis, in the order that has worked

1. `ai-resources openclaw status`: unit, linger, listeners, health, six timers, newest backup per
   tier (daily older than 36 h is flagged), off-box copy, model per agent, doctor warnings. It sets
   `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` itself; do the same by hand for `systemctl --user`
   from SSH or a timer.
2. `openclaw doctor` warnings: the known-noise catalogue is `DOCTOR_NOISE` in
   `scripts/ai_resources/openclaw_host.py` (T30: V8 heap not measured, shell PATH artifacts, "run
   gateway install --force" hint, host desktop, legacy session bindings, whisper-cli, Telegram privacy
   mode, `/dashboard` conflict). `status` filters them; anything else is signal, read it verbatim.
   `doctor` evaluates the CLI's environment, not the gateway's (T14).
3. A 403 `proxy_attribution_required` from the ingress is a missing `X-Forwarded-For` and a
   `trustedProxies` mismatch (T05): check `gateway.trustedProxies` and `gateway.allowRealIpFallback`.
4. A dashboard or pairing link with the wrong host: `gateway.publicOrigin` and the device-pair
   `publicUrl` (T28). `bind: tailnet` also listens on loopback, which is why the CLI still works (T04).
5. Live sessions keep the old context: after changing an `AGENTS.md`, send `/new` in the topic
   (T19). Project `CLAUDE.md` is not loaded in topic sessions (T02).
6. Useful greps: `grep "outbound send ok" ~/.openclaw/logs/gateway.log` is the only proof the
   narration hook published; `ss -lntp | grep 18789` after any restart; `journalctl --user -u
   openclaw-gateway.service -n 100`.

## Backups and units

Ten units, from `templates/systemd/`: `openclaw-backup@.service` with daily, weekly and monthly
timers, `openclaw-maintenance`, `openclaw-watchdog` and `openclaw-verify`, each with a timer (six
timers in all). Scripts are in `scripts/openclaw/` and run as `/bin/bash <script>`, so the exec bit
is not load-bearing. Host values come from `~/.openclaw/kit-host.env`, which is also the only
record of the installed openclaw version when nothing is pinned
(`OPENCLAW_INSTALLED_VERSION`, `OPENCLAW_PREVIOUS_VERSION` is the one-command rollback target):
back it up and restore it first. Verify a backup by listing the newest tarball and its `.sha256`,
never by trusting the timer; the backup omits regenerable skills, transcripts and git checkouts on
purpose. The off-box restore has never been rehearsed end to end (P5): treat it as unproven.

## Related

`$AGENT_KIT/docs/runbooks/openclaw-host-dr.md` (disaster recovery),
`$AGENT_KIT/workflows/openclaw-host-setup.workflow.yaml` (host onboarding),
`$AGENT_KIT/docs/openclaw/runbook.md` ("how setup asks").
