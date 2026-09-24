# OpenClaw AI setup on bithome

How the Telegram-facing AI agents on `bithome` are built, why each piece is the way it is,
and how to stand the whole thing up again. Written on 2026-09-18 from a single session that
took the setup from "haiku inventing that it had created Telegram topics" to a runtime on
Homebrew Node with off-box backups, alerting, a published Control UI and a narrated team.

Everything here was verified against the live system on that date. Where something could
not be verified it says so; treat those lines as leads, not facts.

## Read in this order

| # | Document | What it answers |
|---|---|---|
| 1 | [inventory.md](inventory.md) | What exists today: the six agents with their model and workspace, all 48 configuration changes with the command and the reason, what was installed, what was written, what was cleaned up, the decisions taken and how each one was verified |
| 2 | [pitfalls.md](pitfalls.md) | 32 traps with the literal symptom, the verified cause, the fix and how to spot it next time; what is still open; and the ten kit customizations, each with what was implemented and where it lives |
| 3 | [runbook.md](runbook.md) | How `ai-resources setup` asks, standing it up from a clean machine in six phases, recovering it from a backup, the daily cheat sheet, a 34-box final checklist, and the pending operator steps. Eight steps need a human and say so |
| 4 | [../runbooks/openclaw-host-dr.md](../runbooks/openclaw-host-dr.md) | Disaster recovery from a backup tarball with the kit, `kit-host.env` first. **Unrehearsed**, and it says so |

If you only read one page, read `pitfalls.md`. The configuration in `inventory.md` can be
re-derived from the live system in an afternoon; the traps cost hours each and several of
them fail silently.

## The five things most likely to bite you

1. **`openclaw doctor --fix` stops the gateway and can leave it dead.** It re-inspects the
   stopped unit and aborts if the cgroup has not drained — and `Restart=always` does not
   cover an explicit stop. Drain first, always. (`pitfalls.md` T01)
2. **A repository's `CLAUDE.md` is never loaded in a topic session.** OpenClaw forces
   `--setting-sources user` and throws if you try otherwise, so project rules live in each
   workspace's `AGENTS.md`. `/claude` and `/equipo` are the exception. (T02)
3. **OpenClaw receives every Claude Code subagent record and discards it on purpose**, so
   team activity cannot be surfaced by configuration; it has to be instrumented on the
   Claude Code side, which the kit's narration hook does (off unless you turn it on). (T31)
4. **A DNS record without its `recordId` is a time bomb** in this Crossplane composition:
   the managed resource is named by list position. (T25)
5. **The orchestrator's model is not a detail.** Every confabulated topic, every invented
   completion and every "I don't have permission" in this setup's history came from the
   same cause: haiku orchestrating work it could not do. (`inventory.md` §6)

## Keeping this honest

These documents describe a machine that keeps changing. When you change the setup, change
the page that lies. `inventory.md` §7 lists the command behind every claim, so re-verifying
a section is minutes, not archaeology.

## What the kit now implements

The ten customizations in `pitfalls.md` section C (C01-C10) are implemented as of ai-resources
1.9.0, so the setup is **reproducible from the kit**, not only documented. The way in is
`ai-resources setup`: its OpenClaw section asks, per capability, whether to enable it and configures
it (see "How setup asks" in `runbook.md`). The `ai-resources openclaw <verb>` commands are thin
wrappers for headless runs and recovery.

| What | Where it lives |
|---|---|
| Team narration hook (T31), off unless opted in | `hooks/openclaw_team_progress.py` |
| Gateway guard hook (T01) | `hooks/openclaw_gateway_guard.py` |
| Host scripts (backup, maintenance, watchdog, verify, team-watch) | `scripts/openclaw/` |
| The ten systemd units | `templates/systemd/` |
| Bootstrap, drained doctor, status, agent-new | `scripts/ai_resources/openclaw_host.py` |
| The wizard section | `scripts/ai_resources/setup/cockpits/_openclaw_host.py` |
| Canonical config block | `profiles/openclaw-host.json5` |
| AGENTS.md templates | `templates/AGENTS.*.template.md` |
| Operating skill | `skills/openclaw-operations/` |
| Off-box backup manifests | `templates/gitops/openclaw-backups/` |
| DR runbook and onboarding workflow | `docs/runbooks/openclaw-host-dr.md`, `workflows/openclaw-host-setup.workflow.yaml` |

Still open: the restore has **never been rehearsed** (P5), and the live rehearsal of the kit on the
reference host is a pending operator step (`runbook.md`, "Pending operator steps"). Secrets and the
host-specific values live in `~/.openclaw/kit-host.env` and the secret store, never in the kit.
