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
| 2 | [pitfalls.md](pitfalls.md) | 31 traps with the literal symptom, the verified cause, the fix and how to spot it next time; what is still open; and ten concrete customizations for this kit |
| 3 | [runbook.md](runbook.md) | Standing it up from a clean machine in six phases, recovering it from a backup, the daily cheat sheet, and a 34-box final checklist. Eight steps need a human and say so |

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
   Claude Code side. (T31)
4. **A DNS record without its `recordId` is a time bomb** in this Crossplane composition:
   the managed resource is named by list position. (T25)
5. **The orchestrator's model is not a detail.** Every confabulated topic, every invented
   completion and every "I don't have permission" in this setup's history came from the
   same cause: haiku orchestrating work it could not do. (`inventory.md` §6)

## Keeping this honest

These documents describe a machine that keeps changing. When you change the setup, change
the page that lies. `inventory.md` §7 lists the command behind every claim, so re-verifying
a section is minutes, not archaeology.

The proposals in `pitfalls.md` section C are not implemented. They turn this setup from
"documented" into "reproducible from the kit" — the three host scripts as versioned
artifacts, a wrapper that drains before calling doctor, and the canonical configuration
block. Until they land, the scripts live on one disk and are only recoverable from the
backup tarball.
