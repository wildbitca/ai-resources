# Scratch space (ADR-0020)

Every step of a workflow, and every subagent you spawn, writes scratch under one directory: `$AI_RESOURCES_SCRATCH`
(`~/.cache/ai-resources/runs/<role>-<slug>-<UTCstamp>-<pid>-<rand>/` with `tmp/ pytest/ logs/ out/ clone/`).
Never write directly under `~/.cache` or `/tmp`: not a clone, a venv build, a log, `mktemp`, `--basetemp` or `tempfile`.

- **Create:** `eval "$(ai-resources scratch new --role <role> --slug <task> --owner-pid $$ --shell)"` exports `AI_RESOURCES_SCRATCH`
  and `TMPDIR=$AI_RESOURCES_SCRATCH/tmp`. In a shell script: `. "$AGENT_KIT/scripts/scratch.sh"; scratch_begin <role> <slug>` (traps EXIT INT TERM).
  In Python: `with scratch.run(role, slug):`.
- **Finish:** `ai-resources scratch done "$AI_RESOURCES_SCRATCH" --status <exit code>`. Success removes the whole directory;
  failure keeps only `logs/`, for 3 days.
- **Dispatch:** put `AI_RESOURCES_SCRATCH=<dir>` and the rule above in every child prompt. The OpenClaw orchestrator does it for you and
  finishes the directory when it judges the step. A child that inherits a variable reuses that directory and leaves its cleanup to the owner.
- **Virtualenvs** are shared, never per run: `ai-resources scratch venv --lock requirements.txt` prints the path (keyed by python version
  and lockfile hash; pruned after 30 idle days).
- **Prune:** `ai-resources scratch prune [--older-than 7d] [--dry-run]`, also on every `scratch new` and from a daily user timer. It decides by
  `.scratch-meta.json` and owner-pid liveness, never by mtime, skips live runs and caps the root at 5 GiB.
