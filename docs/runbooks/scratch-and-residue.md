# Runbook: scratch, shared caches and residue (ADR-0020, ADR-0021)

## Inspect
- `ai-resources doctor` prints the sizes (`~/.cache/ai-resources`, `~/.openclaw/{scratch,tmp,scan,gocache,security-scan,backups}`) and flags legacy entries.
- `ai-resources scratch ls` lists runs (status, live/dead, age, size). `~/.cache/ai-resources/prune.log` lists every removal.
- `du -sh ~/.cache/ai-resources/{runs,shared,venvs,backups}` for the four buckets.
- Timers: `systemctl --user list-timers | grep -E 'scratch-prune|worktrees-gc'`.

## Prune
- `ai-resources scratch prune --dry-run` then `ai-resources scratch prune` (7 days, 2 GiB cap; `--older-than 3d`, `--cap-gib 1`).
- `ai-resources scratch prune --shared [--dry-run]` trims the shared caches to 6 GiB.
- `ai-resources scratch prune-scans [--dry-run]` keeps 7 basic + 2 weekly in `~/.openclaw/security-scan-results`.
- Legacy dirs: `ai-resources doctor --fix` (dry run, lists), then `ai-resources doctor --fix --apply` after the owner agrees.

## Recover
- A run kept with `--keep-on-fail` has `logs/` and `.scratch-meta.json` for 3 days: read them before the prune does.
- Nothing is ever deleted outside `~/.cache/ai-resources/{runs,venvs,backups,shared}` by the timer. A removed backup is gone: backups expire 14 days after creation (`.expires.json`).
- A worktree removed by `worktree-done` is snapshotted by OpenClaw: `openclaw worktrees list`, then `openclaw worktrees restore <id>`.
- `worktree-done` refused? It names the reason (dirty files or N commits on no remote). Push or merge, then run it again.

## Host change for the security scan writer (outside this repo; applied by the owner, not by the kit)
`~/.openclaw/workspace/scripts/security/security-scan-daily-basic.py` should, at its start and end:
1. `eval "$(ai-resources scratch scan-env --name security-scan)"` (exports `AI_RESOURCES_SCRATCH`, `TMPDIR` and the shared `GOCACHE`/`GOMODCACHE`), and stop setting its own `GOCACHE`/`GOMODCACHE`/work dir under `~/.openclaw`.
2. Read each repo in place: `repo=$(ai-resources scratch scan-checkout <repo-name>)`; no clone or build into `~/.openclaw/security-scan`. Write only the results into `~/.openclaw/security-scan-results/<date>-basic|weekly`.
3. Last step of every run: `ai-resources scratch prune-scans` and `ai-resources scratch done "$AI_RESOURCES_SCRATCH" --status $?`.
(From Python: `subprocess.run(["ai-resources", "scratch", "scan-env"])` and parse the `export` lines, or import `ai_resources.scratch` / `ai_resources.residue` with `PYTHONPATH=<kit>/scripts`.)
