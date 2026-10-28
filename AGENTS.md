<!-- BEGIN ai-resources: managed by `ai-resources setup`; edits inside this block are overwritten -->
# ai-resources (OpenClaw agent)

Kit root: `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec`. After `brew upgrade ai-resources`, run `ai-resources setup`.

## Kit workflow

- Before any multi-step work, load the `kit-orchestration` skill: it explains how to run a workflow (steps, handoff, parallel groups, return format).
- Workflows are the `workflow-*` skills (feature, bugfix, refactor, incident response, ...). Pick one by reading each skill's description, which is its trigger. Definitions live in `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/workflows/`.
- Work as a team with the kit roles rather than in one head: `planner`, `software-architect`, `implementer`, `tester`, `code-reviewer`, `security-auditor`, `verifier`, `doc-writer`. `/kit-plan <goal>` then `/kit-implement` drive that loop.
- Never sign off your own work: only the `verifier` confirms acceptance criteria.
- Size first: the orchestrator scores the task (`kit-orchestration`, "Scorecard") into LITE, STANDARD or FULL and prints it; no mode to pick (override: `--inline`, `--light`, `--full`); one concrete change: `/kit-plan --inline`.
- Hand work between roles through the handoff file: `handoff.md` at the repo root (or `.agent-output/handoff.md`); the shape is `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/handoff.md.template`.

## Long-running commands never block a tool call

- A tool call that is still open holds a gateway stop until the stop timeout; then systemd SIGKILLs the gateway and every child it started (T39).
- Work that may take more than about 2 minutes runs in the background, writes to a file, and is polled with short reads.
- When `run_in_background` is not available (OpenClaw claude-cli children cannot background a Bash call), wrap the command in `timeout <N>` with N well under the tool-call limit, or detach it (`setsid nohup cmd >file 2>&1 &`) and poll the file with short reads.
- Never put a long `sleep` or a wait loop inside one call.
- Never call the native AskUserQuestion in a headless or subagent session: it hangs until the watchdog fires. Use `ask_user` with a `timeoutSeconds` of 120-300, then end the turn; never leave it open waiting. A subagent returns its question to the parent.
- A cron job reports at each phase, so a timeout names the phase that hung.

## Waiting without polling

- Every poll turn re-reads the whole context (poll turns were 21 % of the spend): wait with ONE blocking call per job, or poll no more often than every 60-120 s.
- One wait per background job: `exec` or `agents_wait` with `awaitResults: true` blocks until it finishes; otherwise end the turn and let the completion event wake you.
- No `process(poll)`, `sleep` or `sessions_yield` loops at short intervals; `process(poll)` with `timeout` is the one allowed wait, at most once per phase, then one short read of the output file.
- Where hooks run, `kit_sleep_guard` refuses a `sleep` above 30 s, sleeps that add up above 30 s and any `while`/`until` wait loop.

## Scratch space

- Never write directly under `~/.cache` or `/tmp`. Every clone, venv build, log, `mktemp` and pytest basetemp goes under `$AI_RESOURCES_SCRATCH` (`tmp/ pytest/ logs/ out/ clone/`), one directory per run: `~/.cache/ai-resources/runs/<role>-<slug>-<UTCstamp>-<pid>-<rand>/` (ADR-0020).
- Dispatch exports it for you. Without it: `eval "$(ai-resources scratch new --role <role> --slug <task> --owner-pid $$ --shell)"`, and finish with `ai-resources scratch done "$AI_RESOURCES_SCRATCH" --status $?`. In a shell script, source `scripts/scratch.sh` and call `scratch_begin <role> <slug>` (it traps EXIT INT TERM).
- Success removes the run. Failure keeps only `logs/`, for 3 days. A daily timer and every `scratch new` prune by `.scratch-meta.json` and owner-pid liveness, never by mtime.
- Virtualenvs are shared, not per run: `ai-resources scratch venv --lock <requirements file>` (keyed by python version and lockfile hash, pruned after 30 idle days). Package and build caches (Go, npm, pnpm, pip, pub, gradle) are shared under `~/.cache/ai-resources/shared/<tool>`: `eval "$(ai-resources scratch shared-env)"`; never create a cache dir of your own.
- Scans read the existing checkout under `~/Development` (`ai-resources scratch scan-checkout <repo>`), never a clone or build under `~/.openclaw`. A finished workflow removes its worktree with `ai-resources scratch worktree-done <path>` (refuses with unpushed commits). Kit backups go to `ai-resources scratch backup-dir <name>` (14-day expiry).

## Worktrees

- Do a task's work on its own branch in a managed worktree, not in the shared checkout: `openclaw worktrees create <repoRoot> --name <agent>-<task> [--base-ref <ref>]` prints the path and the branch (`openclaw/<name>`); work only there. The kit does not create, snapshot or clean worktrees: it delegates to `openclaw worktrees`.
- `openclaw worktrees list` shows the active and the restorable ones; `remove <id>` snapshots then removes; `restore <id>` brings one back; `gc` reclaims the old ones.
- Managed worktrees live under `~/.openclaw/worktrees/<repo-fingerprint>/<name>` (measured on openclaw 2026.9.6, with `worktreeRoot` unset). Never create a worktree, a virtualenv or any scratch you need again under `/tmp`: it is tmpfs, held in RAM and emptied on every reboot, which is how the kit's test virtualenv and the old scratch worktrees died (T26).
- The `using-git-worktrees` skill has the whole flow and the raw `git worktree` fallback for sessions outside OpenClaw.

## Delegation

- Hand code and team work to the `claude` worker agent: `sessions_spawn agentId=claude cwd=<project> thread=true`, so the reply lands back in the same Telegram thread.
- Do not do a project's coding in this workspace; delegate it, then report.
<!-- END ai-resources -->

# Agent skills & orchestration policy

> [!IMPORTANT]
> **Prefer retrieval-led reasoning over pre-training-led reasoning.**
> When a skill matches the task, read its `SKILL.md` before acting — do not rely on memory alone.

## Rule Zero: Zero-Trust engineering

- **Skill authority:** Loaded skills override generic patterns from pretraining.
- **Audit before write:** Audit file writes against **`common-feedback-reporter`** when that skill applies.

## How the kit is delivered

- **`ai-resources setup`** links every skill into the agent's native skills directory (`~/.claude/skills/<id>`, `~/.agents/skills/<id>`), generates the role subagents, installs the kit hooks, and writes a short managed block into each tool's instruction file. Content outside that block is never touched.
- **`ai-resources generate`** imports vendor skills from **`resources.json`**, turns each `workflows/*.workflow.yaml` into a `workflow-<name>` skill, and rebuilds **`skills-index.json`** (for tools without native skill discovery).
- After updating the install (`brew upgrade ai-resources` or `git pull`), run `ai-resources generate` and `ai-resources setup`.

## Orchestration

| Need | Where |
|------|-------|
| Pick a workflow | The `workflow-*` skills — each description is the workflow's trigger |
| Run a workflow (steps, handoff, parallel groups, return format) | Skill **`kit-orchestration`** |
| Workflow YAML shape | **`workflows/WORKFLOW_CONTRACT.md`** |
| Domain commands | **`workflows/_domain-commands.yaml`** |
| Roles and personas | **`agents/roles/`**, **`agents/personas/`** |
| Commands | Skills `delegate`, `setup-project`, `self-update`, `knowledge-audit` |

## Project repos: specs vs knowledge (SDD)

- **Product source of truth:** `<repo>/specs/` — features, cross-cutting, architecture, **`specs/PROJECT.md`**.
- **Working artifacts:** `<repo>/specs/knowledge/{research,decisions,searchable}/` — not canonical long-term. When promoted to specs, run **`knowledge-audit`** in the same workflow run.
- **Workflows** that touch `specs/knowledge/` include SDD closure via the **`knowledge-audit`** skill.
- **Bootstrap:** skill **`setup-project`** and **`rules/000-project-bootstrap.mdc`**.

## Skills

- **Canonical content:** each **`skills/<id>/SKILL.md`** — the only source of procedures. Ids are flat and globally unique.
- **Index:** `skills-index.json` is generated; do not hand-maintain a skill list anywhere else.
- **Imported skills (`gpm-*`):** re-import from **`resources.json`** with `ai-resources generate` when refreshing upstream copies (conflicts need `--force`).

## Polling guard (Claude Code, ADR-0014)

- `ai-resources setup` registers `hooks/kit_sleep_guard.py` as a PreToolUse hook on Bash: a `sleep` longer than 30 seconds, sleeps that add up above 30 seconds and any `while`/`until` wait loop are refused with exit 2. Wait with `run_in_background` and one read, or the Monitor tool.
- The hook does not fire inside OpenClaw `claude-cli` children (ADR-0005, ADR-0007 section 9); the "Waiting without polling" rules above are the guard there.
- `ai-resources audit` and `ai-resources doctor` alert when poll turns exceed 5% of the estimated cost (`AI_RESOURCES_POLL_SHARE_ALERT` overrides the threshold).

## Further reading

- **Kit map:** `rules/016-kit-architecture.mdc`
- **Token cost:** `docs/decisions/0013-token-cost-lean-defaults.md`, `docs/decisions/0014-token-cost-round-2.md`
- **Engram / persistence:** `rules/014-engram-mcp-protocol.mdc`, `rules/300-engram-memory.mdc`, `skills/_shared/persistence-contract.md`
- **Multi-model routing:** `rules/017-multimodel-routing.mdc`, `docs/multi-model.md`
