# Backends

On-demand reference for `skills/kit-orchestration/SKILL.md`. Read it before dispatching any step.

## Dispatch: the matrix decides

Every step resolves its role first: `ai-resources route <role> --run $RUN --record --step <id>` (add `--refresh` to re-read live pool usage). The result is the only thing that picks the runtime; `claude-code` is one result, not a default. A step that pins `backend: claude-code | openclaw-agent | agy` skips the matrix for the runtime only, never for the record.

| Route result | Dispatch |
|---|---|
| Claude cell (`claude-cli` / `claude:*`) from Claude Code | Task tool with the kit role subagent |
| Any cell from an OpenClaw-run orchestrator (`main` and every channel agent may spawn every role) | `sessions_spawn agentId=kit-role-<role> cwd=<checkout>`; the agent's model and `model.fallbacks` are the matrix cell and chain |
| agy cell from Claude Code (no `sessions_spawn`) | `ai-resources agy run --role <role> --run $RUN --step <id> --prompt-file F [--scope PATH...]`; it returns the report contract and writes `.agent-output/<run>/<role>/report.md` |
| `Blocked` (checker has no independent family, tool missing, `openclaw-agent` step without `sessions_spawn`) | the step result is `Blocked: <cause>`, shape below |

`sessions_spawn` must be in the orchestrator's own tool list; an `openclaw` binary on PATH does not count. A blocked step is exactly:

```text
Blocked: true
Block_reason: <role> step <id>: <cause from route / agy run>
Return_to_step: <id>
```

**Checkers (`verifier`, `security-auditor`).** Independence is dynamic: the route takes `--run`, reads the writer families recorded in the dispatch ledger for that run and picks the cheapest adequate model of a family that did not write (Sonnet checks Gemini- or Haiku-written work; Opus only as the ceiling). No independent family: `Blocked` with cause `no-independent-checker`, never a same-family check.

**Fallback.** One primary, one chain per role. The chain fires only when the pool is exhausted (quota/credit text, `pool_usage` at `spill_at`) or the model does not respond (timeout, 5xx, empty). OpenClaw applies it natively through `model.fallbacks`; `agy run` exits 75 with `{cause, fallback}` and the caller dispatches the next cell. Every fallback is recorded with its cause (`ai-resources dispatch record --run $RUN --step <id> --role <role> --outcome ok --cell <backend:model> --fallback-from <primary cell> --cause <cause>`). Any other failure is a failed attempt, not a fallback.

**Dispatch.** Whether an agent exists and is admitted by `subagents.allowAgents` is checked now, never by the validator. The `cwd` is mandatory and explicit. Parameters and completion signal of `sessions_spawn` are in ADR 0005 (`docs/decisions/0005-cross-provider-role-dispatch.md`); the child's startup check (below) is the only proof of its working directory.

**Child prompt.** The child has no kit subagent definitions and no native skills, so the prompt carries everything:

- the absolute workspace path, which must be its working directory, and the absolute handoff path;
- `$AGENT_KIT/agents/roles/<role>.md` and, if it exists, `$AGENT_KIT/agents/personas/<role>-<domain>.md`, to be read first;
- each skill as `$AGENT_KIT/skills/<id>/SKILL.md`;
- the goal, the constraints and the domain commands;
- the write scope: `.agent-output/<run>/<role>/`, plus the paths of the run's write-scope claim for a writer, plus the run's handoff for a sequential step only;
- the attempt number, the return format above verbatim (some role files have no return block of their own), and the sentinel spec below;
- a startup self-check: run `git rev-parse --show-toplevel`, and end as `Blocked` if it differs from the given worktree.

The prompt carries no secrets, no environment dumps and no code.

**Waiting (T39).** Every call that waits or polls lasts at most 60 s; never put a wait loop inside one call, and never restart or stop the gateway. The budget per attempt is 20 minutes. Completion is the sentinel `.agent-output/<run>/<role>/<step-id>.done` (JSON `{step, attempt, status, finished_at}`, written by the child last), or a terminal session state when a status tool exists. A timed-out child is ended with a session tool if there is one; otherwise the attempt is recorded as lost.

**Post-step check.** The Claude Code hooks (`kit_subagent_return`, `kit_handoff_guard`) do not fire for these children, so the orchestrator runs the check itself. The step is complete only if all of these hold:

1. The sentinel (or a terminal state) is present.
2. `.agent-output/<run>/<role>/<step-id>.md` exists and contains `## Result` and `## Routing`.
3. For a sequential step, the handoff front matter has `step: <id>`, a valid `status` and the keys of `handoff.md.template`. For a parallel member, the handoff sha256 is the same before and after.
4. For a read-only role, compared with a snapshot taken before the child was spawned: HEAD is unchanged; the sha256 of `$(git rev-parse --git-common-dir)/config`, the worktree config and the hooks directory listing is unchanged; and `git -C <worktree> status --porcelain --ignored` shows no new path outside `.agent-output/`. Always run these git commands with `-c core.fsmonitor=false -c core.hooksPath=/dev/null` before `-C`, so a planted config cannot execute code during the check.

Any failure is one failed attempt. On a failure of check 4, restore and clean only the offending paths (for a writer, paths outside its claimed scope are logged on the ledger row, never trusted away); a changed HEAD or config hash is reported to the operator and not auto-repaired. The check is best-effort: it detects a violation and cannot prevent one, because the child runs with its own agent's full permissions. It does not cover writes outside the worktree (home-directory config, shell rc files, `~/.claude/agents`), so the worktree is not a sandbox.

**Retries.** At most 3 attempts per step, each a new spawn with the same `cwd`. Before a retry, rename the previous report to `<step-id>.attempt-<n>.md`. After the third failure, stop and report the last `Block_reason`.

**Evidence.** Per dispatch the ledger row (`dispatches.jsonl`) holds role, pool, backend, model, tokens, duration, outcome and fallback cause; in the report and the handoff notes (never in new front-matter keys) record: handle or sessionKey, cwd, attempt, the result of checks 1-4, the report path and, for a parallel member, the handoff hash before and after.

Teams share the checkout: a run that writes holds a write-scope claim (`scope_guard`, `--param write_scope=src/,docs/`; default the whole checkout), a second team on an overlapping scope waits queued, and a finished step's out-of-scope paths are logged, never trusted away. Every run has its own handoff file and `.agent-output/` reports.
