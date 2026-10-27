# Backends

On-demand reference for `skills/kit-orchestration/SKILL.md`. Read it only when a step has `backend: openclaw-agent`. Steps on `claude-code` never need it.

## Choosing a backend

A step runs on `claude-code` unless its YAML says `backend: openclaw-agent` with an `agentId`. Use `openclaw-agent` when the role should run on another OpenClaw agent, and through it on another provider. Coordination stays the handoff file. The model is a property of whatever runs the role (the role's `by_role` executor, or the model configured on that agent); a step cannot pin one.

**Availability.** An `openclaw-agent` step can be dispatched only by an orchestrator whose own tool list contains `sessions_spawn`, that is, an OpenClaw-run agent turn. An `openclaw` binary on PATH does not count. A Claude Code session, a kit role subagent, a plain shell and the `/kit-plan` / `/kit-implement` scripts do not have it. When it is missing, do not fall back to `claude-code` and do not spawn anything; the step result is exactly:

```text
Blocked: true
Block_reason: openclaw-agent step <id> (agentId=<x>) needs sessions_spawn in an OpenClaw-run orchestrator; missing: sessions_spawn tool in this session
Return_to_step: <id>
```

**Dispatch.** Whether the agent exists, runs the CLI backend you expect and is admitted by `subagents.allowAgents` is checked now, never by the validator. Spawn with `sessions_spawn agentId=<agentId> cwd=<the orchestrator's worktree>`. The `cwd` is mandatory and explicit. The parameters, the return value and the completion signal of `sessions_spawn` are recorded in ADR 0005 (`docs/decisions/0005-cross-provider-role-dispatch.md`); where that record is empty they are unverified, and the child's own startup check (below) is the only proof of the working directory.

**Child prompt.** The child has no kit subagent definitions and no native skills, so the prompt carries everything:

- the absolute workspace path, which must be its working directory, and the absolute handoff path;
- `$AGENT_KIT/agents/roles/<role>.md` and, if it exists, `$AGENT_KIT/agents/personas/<role>-<domain>.md`, to be read first;
- each skill as `$AGENT_KIT/skills/<id>/SKILL.md`;
- the goal, the constraints and the domain commands;
- the write scope: only `.agent-output/<run>/<role>/`, plus the handoff for a sequential step only;
- the attempt number, the return format above verbatim (some role files have no return block of their own), and the sentinel spec below;
- a startup self-check: run `git rev-parse --show-toplevel`, and end as `Blocked` if it differs from the given worktree.

The prompt carries no secrets, no environment dumps and no code.

**Waiting (T39).** Every call that waits or polls lasts at most 60 s; never put a wait loop inside one call, and never restart or stop the gateway. The budget per attempt is 20 minutes. Completion is the sentinel `.agent-output/<run>/<role>/<step-id>.done` (JSON `{step, attempt, status, finished_at}`, written by the child last), or a terminal session state when a status tool exists. A timed-out child is ended with a session tool if there is one; otherwise the attempt is recorded as lost.

**Post-step check.** The Claude Code hooks (`kit_subagent_return`, `kit_handoff_guard`) do not fire for these children, so the orchestrator runs the check itself. The step is complete only if all of these hold:

1. The sentinel (or a terminal state) is present.
2. `.agent-output/<run>/<role>/<step-id>.md` exists and contains `## Result` and `## Routing`.
3. For a sequential step, the handoff front matter has `step: <id>`, a valid `status` and the keys of `handoff.md.template`. For a parallel member, the handoff sha256 is the same before and after.
4. For a read-only role, compared with a snapshot taken before the child was spawned: HEAD is unchanged; the sha256 of `$(git rev-parse --git-common-dir)/config`, the worktree config and the hooks directory listing is unchanged; and `git -C <worktree> status --porcelain --ignored` shows no new path outside `.agent-output/`. Always run these git commands with `-c core.fsmonitor=false -c core.hooksPath=/dev/null` before `-C`, so a planted config cannot execute code during the check.

Any failure is one failed attempt. On a failure of check 4, restore and clean only the offending paths; a changed HEAD or config hash is reported to the operator and not auto-repaired. The check is best-effort: it detects a violation and cannot prevent one, because the child runs with its own agent's full permissions. It does not cover writes outside the worktree (home-directory config, shell rc files, `~/.claude/agents`), so the worktree is not a sandbox.

**Retries.** At most 3 attempts per step, each a new spawn with the same `cwd`. Before a retry, rename the previous report to `<step-id>.attempt-<n>.md`. After the third failure, stop and report the last `Block_reason`.

**Evidence.** Per dispatch, record in the report and the handoff notes (never in new front-matter keys): backend, agentId, handle or sessionKey, cwd, attempt, start and end times, the result of checks 1-4, the report path and, for a parallel member, the handoff hash before and after.

**Example team.** Models are properties of the agent or role and informational here; `agy-flash` and `agy-pro` are placeholder ids for agents that must exist and be on `subagents.allowAgents`.

| Role | Backend | agentId | Model configured on that agent or role |
|------|---------|---------|-----------------------------------------|
| planner | openclaw-agent | agy-flash | gemini-flash (agy label) |
| explore (the "researcher") | openclaw-agent | agy-pro | gemini-pro (agy label) |
| software-architect | claude-code | | opus via `by_role` |
| implementer | claude-code | | sonnet via `by_role` |
| code-reviewer | openclaw-agent | agy-flash | gemini-flash (agy label) |

The example team is illustrative. `rules/017-multimodel-routing.mdc` recommends a strong model for review, so a real team would keep `code-reviewer` on a strong model. An agent backed by agy is an external prerequisite that this kit does not provide (see ADR 0005).

Teams share the checkout: a run that writes holds a write-scope claim (`scope_guard`, `--param write_scope=src/,docs/`; default the whole checkout), a second team on an overlapping scope waits queued, and a finished step's out-of-scope paths are logged, never trusted away. Every run has its own handoff file and `.agent-output/` reports.
