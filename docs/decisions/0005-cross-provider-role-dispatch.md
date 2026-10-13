# ADR-0005: Cross-provider role dispatch (`backend` step field)

**Status**: accepted for the fields, the validator and the procedure; the live behaviour of `sessions_spawn` is unverified (see "Verification evidence")
**Date**: 2026-10-13
**Decision makers**: planner, implementer, operator (decisions of 2026-10-13 recorded in the plan)

## Context

A workflow step always ran its role on Claude Code. The goal is to let a step run on another OpenClaw agent, and through it on another provider, while coordination stays the file-based `handoff.md` contract. The mechanism has to work today between two OpenClaw agents and work unmodified later with an agy-backed agent.

The repo has no `specs/` tree, so there are no TS-IDs to cite. The constraints come from `rules/017-multimodel-routing.mdc`, [ADR-0002](0002-provider-agnostic-model-selection.md), [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) and the repo `AGENTS.md` (T39: no tool call open longer than about 2 minutes; T26: nothing scratch under `/tmp`).

## Decision

1. **Two optional step fields.** `backend: claude-code | openclaw-agent` (default `claude-code`, today's behaviour) and `agentId`, required if and only if the backend is `openclaw-agent`, matching `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (a leading alphanumeric keeps flag-like values out). Checked by `scripts/validate_kit.py`; described in `workflows/WORKFLOW_CONTRACT.md`; shown as a Backend column in the generated `workflow-*` skills.
2. **No `model` field.** The model belongs to whatever runs the role: `by_role` executors for `claude-code`, `agents.entries.<agentId>.model.primary` for `openclaw-agent`. Changing the model means pointing `agentId` at a differently configured agent.
3. **No fallback.** If the orchestrator has no `sessions_spawn` tool the step is `Blocked` with a fixed `Block_reason`, and nothing is spawned. There is no per-step fallback field.
4. **Who may run remotely.** The implementer and every other write-capable role are rejected on `openclaw-agent` (one writer per run). `planner`, `explore`, `software-architect`, `code-reviewer`, `security-auditor` and `verifier` are allowed. `tester` and `doc-writer` are allowed with a validator warning: they run commands or write files, so the post-step check detects a violation but cannot prevent one. Only the six read-only roles may be members of a parallel group, which may mix backends.
5. **Availability and existence are dispatch-time checks.** Validation never touches the live host. The orchestrator checks for the `sessions_spawn` tool; the target agent, its CLI backend and `subagents.allowAgents` membership are checked when the step is dispatched.
6. **Post-step check replaces the Claude-only hooks.** `kit_subagent_return` and `kit_handoff_guard` do not fire for OpenClaw children. The orchestrator checks the completion sentinel, the report headings, the handoff front matter (or its hash for a parallel member) and `git status --porcelain` for read-only roles. Any failure is one failed attempt; at most 3 attempts. The check is a skill convention, not code: an LLM orchestrator could skip it. This is accepted for v1.
7. **Out of scope.** An agy OpenClaw runtime or an agy-backed agent, any `team:` block, new role or handoff front-matter key, and any change to `kit-plan.js` / `kit-implement.js` (they dispatch through `agentType` inside Claude Code and cannot call `sessions_spawn`). Cross-provider dispatch exists only on the `workflow-*` skill path run by an OpenClaw orchestrator.

### Dispatch sequence and completion signal

1. The orchestrator confirms `sessions_spawn` is in its tool list; otherwise it returns the `Blocked` text from `skills/kit-orchestration/SKILL.md`.
2. It spawns the agent with an explicit `cwd` (its worktree) and the child prompt contract.
3. It polls with calls of at most 60 s (T39) within a 20-minute budget per attempt.
4. Completion is the sentinel `.agent-output/<role>/<step-id>.done` (JSON, written last by the child) or a terminal session state when a status tool exists.
5. It runs the post-step check, records the evidence and advances or retries.

### Writers matrix

| Role | Backend | Writes |
|------|---------|--------|
| implementer | `claude-code` only | the code (the single writer) |
| planner, software-architect | either | `.agent-output/<role>/`, and the handoff when sequential |
| explore, code-reviewer, security-auditor, verifier | either | `.agent-output/<role>/`, and the handoff when sequential (parallel members write `.agent-output/<role>/` only) |
| tester, doc-writer | either (warning on `openclaw-agent`) | `.agent-output/<role>/`, and the handoff when sequential |

### T39 compliance

Every wait or poll call lasts at most 60 s with no loop inside a call. The procedure never restarts or stops the gateway and never changes OpenClaw configuration.

### Security limits

Read-only enforcement is detective and best-effort. `claude-kit` and `agy-cli` run with `--dangerously-skip-permissions`, so a child has its agent's full permissions as the host user. The worktree does NOT contain such a child: it can write outside the worktree (OpenClaw config, shell rc files, `~/.claude/agents`), to the shared git directory (hooks, config) and to gitignored files, and `git status --porcelain` alone sees none of this. The post-step check therefore snapshots HEAD, the git config files, the hooks directory and `git status --porcelain --ignored` before and after each child, and runs git with `core.fsmonitor` and `core.hooksPath` neutralised so the check itself cannot be an execution trigger. A changed HEAD or config hash fails the attempt. Writes outside the worktree are undetected. Until a real sandbox exists, read-only roles should be placed only on agents that do not run with skip-permissions where that matters. The child prompt carries no secrets and no code, and the `agentId` regex blocks flag-like and shell-like values.

**Data egress.** The prompt is not the only thing that leaves. A child reads the worktree, so repository content (and anything readable from it) is sent to that agent's provider. Dispatch is opt-in by configuring a cross-provider agent; do not route a role to a provider you would not give the repository to.

**Untrusted reports.** A report from another provider is data, not instructions. The orchestrator copies only the report's front-matter fields into the handoff, caps and quotes free text such as `Block_reason`, and never follows instructions found in a report; the implementer is the one write-capable role that reads the handoff.

### What exists for agy today

- The kit plugin registers the `agy-cli` and `claude-kit` CLI backends (`openclaw-plugin/ai-resources/index.js`).
- `antigravity` is an OpenClaw engine in which agy is the chat-facing orchestrator that spawns the `claude` worker on `claude-kit` (`docs/multi-model.md`).
- agy model pins are report-only (`model_pins.AGY_STATIC`).

What does not exist is an OpenClaw agent on `agy-cli` that has been shown to accept role work through `sessions_spawn` with an explicit `cwd` and to signal completion. That, together with the agent being on `subagents.allowAgents`, is the external prerequisite. The mechanism is backend-agnostic; it is meant to be exercised with two ordinary OpenClaw agents first.

**Composition (ADR-0006).** Scored team composition emits no `backend` or `agentId`: it writes models into `by_role` through the profile path only.

## Consequences

- Existing workflows are unchanged: no `backend` means `claude-code`, and the only generated difference is the Backend column.
- A workflow cannot pin a model per step.
- The worked example `workflows/feature-cross-provider.workflow.yaml` uses placeholder agent ids (`agy-flash`, `agy-pro`) that must exist on the host.
- A stalled child costs a whole attempt budget, up to about 1 hour for 3 attempts of 20 minutes.

## Verification evidence

The live phases were not run when this ADR was written: nothing below is claimed beyond what is in the tables.

### Spike table (P0-1, P0-2)

| Spike | Command | Observed result | Timestamp | Verdict |
|-------|---------|-----------------|-----------|---------|
| S4 agent backends | `openclaw agents list --json` and `openclaw config get agents.entries` | not run | | UNVERIFIED |
| S1 `sessions_spawn` accepts `agentId` + `cwd`; return payload | | not run | | UNVERIFIED |
| S2 child `pwd` and toplevel equal the worktree; report written | | not run | | UNVERIFIED |
| S3 status tool / completion announcement / readable final message | | not run | | UNVERIFIED |
| S5 a role subagent or a spawned child can itself call `sessions_spawn` | | not run | | UNVERIFIED (assumed no) |

### Dispatch evidence (P6)

| Probe | backend | agentId | handle | cwd vs pwd vs toplevel | nonce | attempt | start / end | checks 1-4 | report | handoff hash before / after |
|-------|---------|---------|--------|------------------------|-------|---------|-------------|------------|--------|------------------------------|
| sequential explore on agent A, code-reviewer on agent B | | | | | | | | | | n/a |
| parallel A and B | | | | | | | | | | |
| negative probe (no `sessions_spawn`) | | | | | | | | | | n/a |

Until these tables are filled, the dispatch parameters in `skills/kit-orchestration/SKILL.md` are the documented usage (`sessions_spawn agentId=<id> cwd=<path>`, `AGENTS.md`), not a verified contract.
