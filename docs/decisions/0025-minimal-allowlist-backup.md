# ADR-0025 — The host backup is an allowlist of non-reproducible state, and a held lock is never a silent success

Status: accepted (unreleased)

## Context

`openclaw-backup.sh` archived all of `~/.openclaw` with `openclaw backup create`, which has no exclude option. On the reference host
that meant tmp, gocache, npm, venvs, worktrees, security-scan, browser, media, logs and `*.bak*` duplicates: 3.7-11 GB and 6-30 min per
run, fourteen copies on disk. The OpenClaw docs forbid copying a live `.sqlite`, `-wal` or `-shm`, and the full archive did exactly that.

The weekly backup also went stale for 17 days. The daily (03:30) and weekly (03:45) timers share one `flock`; the loser logged
"another run in progress -- skipping" and exited 0, so neither systemd nor the guard saw a failure.

## Decision

1. **The backup is the minimal allowlist.** The archive holds:
   - SQLite snapshots from `openclaw backup sqlite create`: `--global`, and one `--agent <id>` per agent from
     `openclaw agents list --json` that has `agents/<id>/agent/openclaw-agent.sqlite` (three at a time). The stage is a `0700` directory
     directly under the backup dir, because the command refuses a repository under a group-writable parent. engram is not OpenClaw-owned
     and keeps `sqlite3 ... VACUUM INTO`.
   - `files.tar`, built from the allowlist in `scripts/openclaw/_backup_lib.sh` with the excludes `*.bak`, `*.bak-*`, `*.bak.*`,
     `*.reconcile-*`, `*.lock`, `*-wal`, `*-shm`.
   - `INVENTORY.txt`.
   Not captured: anything `ai-resources setup` regenerates (kit roles and lanes and their workspaces, skills, subagents), `~/.local/bin`
   except `openclaw-*`, caches, venvs, worktrees, logs, media, browser state, transcripts.
2. **There is one backup layout.** The legacy whole-`~/.openclaw` archive (`full` mode) was removed rather than kept as a rollback: the
   lock, the assertions, the naming, the `.sha256`-last rule and rotation are unchanged, so the uploader and the guard are unchanged.
3. **The archive is built as `<name>.tar.gz.part` and published only after the assertions pass**: size between `OPENCLAW_BACKUP_MIN_BYTES`
   (5 MB, the guard's floor) and `OPENCLAW_BACKUP_MAX_BYTES` (4 GiB), listable, every expected snapshot with database and
   manifest, `engram/engram.db`, `.openclaw/openclaw.json` inside `files.tar`. The checksum is written last and checked. Any failure removes
   the partial file and notifies once.
4. **A held lock is waited for, then fails.** `flock -n`, then `flock -w ${OPENCLAW_BACKUP_LOCK_WAIT:-3600}`; on timeout the run exits 75
   and notifies. No path exits 0 without a backup.
5. **Schedule.** Daily 03:30 (unchanged), weekly Sun 05:00, monthly on the 1st at 05:30, each with `RandomizedDelaySec=5min`: clear of the
   daily run (minutes long now) and of each other on a Sunday the 1st. The operator's drop-in
   `openclaw-backup-weekly.timer.d/override.conf` already said Sun 05:00; the template now agrees, the drop-in is redundant, and
   `install-units` never touches `*.timer.d`.
6. **Retention** is configurable per tier (`OPENCLAW_BACKUP_KEEP_{DAILY,WEEKLY,MONTHLY,MANUAL}`), defaults 7 / 4 / 3 / 3. At about 0.3 GB
   an archive, fourteen copies are about 4.5 GB. Weekly is optional: disable the timer and add `weekly` to `OPENCLAW_BACKUP_SOFT_TIERS` so
   the guard reports it without failing. The guard limits (daily 36 h, weekly 216 h, monthly 780 h) already fit these tiers.

## Consequences

- Measured on the reference host: 311 MB and 78 s, against 3.7 GB and about 10 min (and up to 11 GB / 30 min).
- **The global snapshot sanitizes the delivery queue**: queued outbound deliveries are not in it, so a restore never replays messages that
  were pending at backup time. The old full archive carried them.
- Agent databases of the kit's own role and lane agents are still snapshotted (small); their workspaces are not.
- A restore is two steps (verify and restore each snapshot, then unpack `files.tar`), documented in `docs/runbooks/openclaw-host-dr.md`.
  The restore test in `tests/test_openclaw_backup_minimal.py` runs `openclaw backup sqlite verify` on every snapshot directory.
- A backup that cannot start because another is running now fails after an hour instead of reporting success.
