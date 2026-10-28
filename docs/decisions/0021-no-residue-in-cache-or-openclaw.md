# ADR-0021: Agent work leaves no unbounded residue in ~/.cache or ~/.openclaw

**Status**: Accepted (2026-10-28, owner update to the scratch-root task). Implemented in 3.2.6. Extends and, where they differ, supersedes [ADR-0020](0020-one-scratch-root.md).
**Date**: 2026-10-28
**Decision makers**: owner (requirements), implementer (record only). Release gate: the full suite and the independent Verifier.
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md); nothing here touches the gateway.

## Context

After the 2026-10-28 `~/.cache` cleanup (22.4 -> 4.5 GB), `~/.openclaw` held 10.8 GB, about 5 GB of it agent leftovers: Go caches
(`security-scan/gocache` 1.2 GB, `gocache` 274 MB, `scratch/gocache` 94 MB), `scratch` 830 MB (135 entries), `scan` 104 MB, `tmp`
205 MB, `security-scan` 1.3 GB (scans cloned and compiled repos), `backups` 429 MB and `cleanup-backup-*` 111 MB (repo snapshots, no
expiry), `state/.pre-startup-migration-*.bak` 242 MB, `media/inbound` 680 MB (chat attachments, no TTL), `gateway.{1..5}.log` 33 MB each.
Target: a normal workflow run leaves nothing unbounded. Steady state: `~/.openclaw` = OpenClaw state + live worktrees;
`~/.cache/ai-resources` bounded and self-pruning.

## Decision

1. **Scratch root** stays `~/.cache/ai-resources/runs/<workflow-or-role>-<slug>-<UTC stamp>-<pid>-<rand>/` through the one helper
   (`ai-resources scratch new`, `scratch dir` prints the root). Meta + owner-pid/lease liveness + flock are unchanged.
   Nothing goes to `/tmp`, `~/.openclaw/{scratch,tmp,scan}` or an ad-hoc `~/.cache/<name>`.
2. **Cleanup on EXIT/INT/TERM; keep-on-failure only with an explicit flag** (`--keep-on-fail`, `keep_on_fail=True`). The default failure
   removes the run. Kept runs hold `logs/` for 3 days and count toward the cap. The orchestrator passes the flag explicitly for dispatched
   steps (the retry needs the evidence); pytest and scripts do not. *Supersedes ADR-0020 "failure keeps logs".*
3. **Prune** (daily user timer `openclaw-scratch-prune.timer` through `ai-resources setup`, merge-never-replace, plus opportunistic on
   `scratch new`): dead runs older than 7 days, then the root capped at **2 GiB** by default (`AI_RESOURCES_SCRATCH_CAP_GB` or
   `--cap-gib`), oldest first; live runs never. Every removal is one line in `~/.cache/ai-resources/prune.log` (path, reason, bytes).
   *Supersedes ADR-0020's 5 GiB.*
4. **Shared caches**: `GOCACHE`, `GOMODCACHE`, npm, pnpm store, pip, pub and gradle all live in `~/.cache/ai-resources/shared/<tool>` (`go-build
   go-mod npm pnpm-store pip pub gradle`). `scratch new --shell`, `scratch scan-env`, `scratch shared-env` and the orchestrator's dispatch env
   export them; no task or scan gets a cache of its own. Cap 6 GiB (`AI_RESOURCES_SHARED_CAP_GB`), trimmed oldest-files-first by
   `ai-resources scratch prune --shared` (mtime is the right signal here: a cache file's age is its last use, and nothing in it has an owner).
   Installs inside a worktree use the shared store through the same variables.
5. **Scans read, never clone or build under `~/.openclaw`**: `scratch scan-checkout <repo>` prints the existing checkout under `~/Development`
   (scans read it in place, or use an ephemeral worktree removed afterwards); `scratch scan-env` opens a scratch run with the shared caches. The writer
   scripts live in `~/.openclaw/workspace/scripts/security/` (outside this repo, not edited here); the exact host change is in the runbook.
6. **security-scan-results retention**: newest **7 `basic` + 2 `weekly`** runs by the timestamp in the name (a run's `.out` sidecar goes with it); the
   timer and the writer's last step call `ai-resources scratch prune-scans`. Doctor warns above 500 MB. *Supersedes "last 7 runs".*
7. **Backups**: any kit-created backup is `ai-resources scratch backup-dir <name>` = `~/.cache/ai-resources/backups/<name>-<date>` with a 14-day
   `.expires.json` marker, pruned by the same timer. The kit has no writer into `~/.openclaw/backups` or `cleanup-backup-*` (they were
   host scripts; the runbook lists the call that replaces them).
8. **Worktrees** stay under `~/.openclaw/worktrees` (OpenClaw-managed). `ai-resources scratch worktree-done <path>` removes one after its branch is
   pushed or merged: it refuses (exit 3) with dirty files or commits no remote has, then calls `openclaw worktrees remove <id> --if-lossless`
   (OpenClaw's own guard, second line). A weekly `openclaw-worktrees-gc.timer` runs `openclaw worktrees gc` (installed by setup, same merge install).
9. **Doctor** reports sizes of `~/.cache/ai-resources` and `~/.openclaw/{scratch,tmp,scan,gocache,security-scan,backups,security-scan-results,workspace/scratch}`,
   flags anything inside the legacy ones and the legacy top-level `~/.cache` names. `doctor --fix` is a **dry run** that lists; `--fix --apply` deletes:
   `~/.cache` entries only if >24 h old with no process cwd/open file inside, `~/.openclaw` legacy children only if >7 days old, not in use, not
   credential-shaped. Real tool caches stay allowlisted. `workspace/scratch` (whisper-models, maybe a duplicate of `~/.local/share/whisper-models`)
   is reported, never deleted. A loose `*.sh` in `~/.cache` is reported, never deleted.
10. **Never touched**: `~/.openclaw/{agents,state,browser}`, workspace configs, live worktrees, `~/.ssh`, any name matching credential/secret/token/`.key`/`.pem`/`.env`.
    No secrets in logs (paths, reasons and sizes only).

## Upstream proposals (not patched here)

- OpenClaw: TTL for `media/inbound` (and `workspace/media/inbound`), 568 MB + 112 MB of chat attachments with no expiry.
- OpenClaw: rotation of `gateway.{1..5}.log` (33 MB each, kept indefinitely).
- OpenClaw: `state/.pre-startup-migration-*.bak` (242 MB) retention.

## Consequences

- One `scratch new` plus a bounded prune per dispatched step; a few seconds of work a day for the timers.
- The host's scan writer needs the three-line change in the runbook; until then its old paths keep filling, and doctor says so.
- A failed unattended run leaves nothing unless someone opted in, so debugging a failure means re-running with `--keep-on-fail`.
