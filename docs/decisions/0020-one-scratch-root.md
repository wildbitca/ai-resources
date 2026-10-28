# ADR-0020: One scratch root, runs that clean up after themselves, and pruning

**Status**: Accepted (2026-10-28). Implemented in 3.2.5.
**Date**: 2026-10-28
**Decision makers**: operator (problem and requirements), implementer (record only). Release gate: the full suite and the independent Verifier.
**Constraint, not amended**: `/tmp` stays banned for anything that must survive a reboot or is large (tmpfs, held in RAM; T26).

## Context

On 2026-10-28 `~/.cache` held 336 stray top-level entries (about 10 GB of 22 GB): `ai-resources-pytest-*` (400-560 MB each),
`pt-*`, `pytest-models` (2,229 directories), `verifier-*`, `verif*`, `tmp-*`, `tmpXXXXXXXX`, `cr-*`, `aisec-*`, `sec-audit-*`,
`ci-*.out`, `full*.log`, per-run virtualenvs, loose `*_fixed.py` and `*.sh`. Verifier, implementer, reviewer and security-auditor
runs made them. `/tmp` is banned and nothing offered a sanctioned alternative, so every run invented its own name and none
cleaned up. `tests/basetemp_default.py` pruned only `run-*` under one directory by mtime, and any explicit `--basetemp` bypassed it.
`~/.openclaw/security-scan-results` grew to 2 GB with no retention.

## Decision

### 1. One root, one directory per run
`~/.cache/ai-resources/runs/<role>-<slug>-<UTCstamp>-<pid>-<rand>/` with `tmp/ pytest/ logs/ out/ clone/` and a
`.scratch-meta.json` (`owner_pid`, `owner_start`, `created_at`, `role`, `keep_on_fail`, `status`). The helper is
`scripts/ai_resources/scratch.py` (Python context manager `scratch.run()`), `scripts/scratch.sh` (`scratch_begin`, trap on
EXIT INT TERM) and the CLI `ai-resources scratch new|done|rm|ls|prune|prune-scans|venv`. It exports `AI_RESOURCES_SCRATCH` and
`TMPDIR=$AI_RESOURCES_SCRATCH/tmp`. Library code that needs a temp directory uses `scratch.tempdir()` / `scratch.new_tmpdir()`
(a run owned by the process when none is exported, removed at exit); write-then-rename temp files beside their destination use
`scratch.atomic_tmp()`. These helpers are the only place `tempfile` and `mktemp` appear.

### 2. Success removes it, failure keeps logs for 3 days
Success deletes the whole directory. Failure (non-zero exit, exception, SIGTERM/SIGINT) deletes everything except `logs/` and the
meta, and marks it `failed`; it expires 3 days later. An owner that vanished without finishing (SIGKILL, power loss) is converted
to the same logs-only state by the next prune, after a 2-minute grace.

### 3. Pruning decides by meta and pid, never by mtime
Fixtures with future-dated mtimes (2027, 2286) already broke mtime guards. A run's age is `created_at` from its meta (else the
UTC stamp in its name); liveness is `owner_pid` plus its start ticks from `/proc` (a recycled pid is not alive). Live runs are never removed.
Order: dead and older than 7 days, failed past its TTL, then a global 5 GiB cap, oldest first. `flock` on `.prune.lock` serialises pruners.
A directory with neither meta nor a stamp is left alone. Pruning runs opportunistically on every `scratch new`, and daily from the
`openclaw-scratch-prune.timer` user unit (`openclaw-scratch-prune.service` runs `scripts/openclaw/openclaw-scratch-prune.sh`),
which `ai-resources setup` installs with the other host units (merge, never replace).

### 4. Shared virtualenvs
`~/.cache/ai-resources/venvs/<python version>-<lockfile hash>/`, built under `flock` on first use
(`ai-resources scratch venv --lock <file>`; falls back to `uv venv` when the distro python has no ensurepip), pruned after 30 days
without use (a `last_used` stamp in the venv, not its mtime). `tests/requirements.txt` is the suite's lockfile.

### 5. Every entry point uses it
- The OpenClaw orchestrator creates a run per dispatched step (or reuses `$AI_RESOURCES_SCRATCH` when its caller owns one), puts the
  variable and the rule "never write directly under ~/.cache or /tmp" in every child prompt, passes the env to the plugin, and
  finishes the run when the step is judged (ok removes, anything else keeps logs).
- Role prompts (implementer, tester, verifier), `kit-implement.js`, `WORKFLOW_CONTRACT.md`, the generated AGENTS block ("Scratch space")
  and the `kit-orchestration` skill name `$AI_RESOURCES_SCRATCH`. `validate_kit.py` rejects `/tmp`, `mktemp`, `--basetemp` or a bare
  `~/.cache` path in a role, persona or workflow prompt.
- `tests/conftest.py` + `tests/basetemp_default.py`: a bare pytest creates `pytest-suite-...`, uses its `pytest/` as basetemp, removes
  it on green. It still refuses a cache root under `/tmp`, honours an explicit `TMPDIR` outside `/tmp` and `PYTEST_DEBUG_TEMPROOT`,
  and reuses an inherited `$AI_RESOURCES_SCRATCH/pytest` without owning its cleanup. An explicit `--basetemp` on the command line is
  still honoured (pytest offers no hook to refuse it); no script or doc in the kit passes one.
- The host shell scripts (`openclaw-maintenance.sh`, `openclaw-models-update.sh`) take their temp files from `scratch_tmpfile`;
  the backup staging directory and the uploader state file use `scratch_tmp_beside`, beside their destination.

### 6. Doctor and legacy cleanup
`ai-resources doctor` warns about legacy top-level `~/.cache` names that match the patterns above. `--fix` removes one only when
nothing inside it changed for 24 h (future mtimes count as "just written") and no process has its cwd or an open file in it.
An allowlist of real tool caches (go-build, gomod, uv, pnpm, pip, claude, openclaw, p10k-*, gitstatus, Unity, Homebrew, ...) is never touched.

### 7. security-scan-results retention
The writer, `~/.openclaw/workspace/scripts/security/security-scan-daily-basic.py`, lives outside this repo and is not edited by the kit.
`scratch.prune_scan_results()` keeps the newest 7 runs ordered by the timestamp in the directory name (`<date>-basic`, `<date>-weekly`
and their `.out` sidecars count as one run). The daily timer calls it, and the writer must call it at the end of every run:
`ai-resources scratch prune-scans` (or `python3 -m ai_resources.scratch prune-scans` with `PYTHONPATH=<kit>/scripts`;
`--root DIR`, `--keep N`, `--dry-run`). Doctor warns above 500 MB.

## Consequences

- A run that leaves nothing behind needs nothing from the operator; a crashed one is reaped in at most 7 days (3 once it is marked failed).
- The legacy pile is removed once, by `doctor --fix`; nothing recreates it because there is no other sanctioned place to write.
- The cost is one `scratch new` (a few file writes and a bounded prune) per dispatched step.
