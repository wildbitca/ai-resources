# ADR-0005: Cross-provider role dispatch (`backend` step field)

**Status**: accepted for the fields, the validator and the procedure; `sessions_spawn` behaviour verified on an isolated gateway on 2026-10-14 for claude-cli children and, later the same day, for an `agy-cli` child (see "Verification evidence" and "agy-cli evidence"); not yet on the live gateway
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

An OpenClaw agent on `agy-cli` that accepts role work through `sessions_spawn` with an explicit `cwd` and signals completion was shown on an isolated gateway on 2026-10-14 (see "agy-cli evidence"); it has not been shown on the live gateway. That, together with the agent being on `subagents.allowAgents`, is the external prerequisite. The mechanism is backend-agnostic; it is meant to be exercised with two ordinary OpenClaw agents first.

**Composition (ADR-0006).** Scored team composition emits no `backend` or `agentId`: it writes models into `by_role` through the profile path only.

## Consequences

- Existing workflows are unchanged: no `backend` means `claude-code`, and the only generated difference is the Backend column.
- A workflow cannot pin a model per step.
- The worked example `workflows/feature-cross-provider.workflow.yaml` uses placeholder agent ids (`agy-flash`, `agy-pro`) that must exist on the host.
- A stalled child costs a whole attempt budget, up to about 1 hour for 3 attempts of 20 minutes.

## Verification evidence

Measured on 2026-10-14 (UTC) on OpenClaw 2026.10.3 against an **isolated** second gateway (`kite2e`, loopback
19789, own state dir, own cgroup), never against the live gateway. The live gateway was only read (P0-1).
The isolated run used three claude-cli agents: orchestrator `e2e-orch` (allowAgents `e2e-a`, `e2e-b`),
`e2e-a` (Sonnet 5) and `e2e-b` (Haiku 4.5). Proof of an untouched live host: 86 L1 sha256 entries identical,
live MainPID and NRestarts unchanged, no `message` call, no non-isolated cwd in any of our 14 sessions.

### Spike table (P0-1, P0-2)

| Spike | Command | Observed result | Timestamp | Verdict |
|-------|---------|-----------------|-----------|---------|
| S4 agent backends (live, read-only) | `openclaw agents list --json` and `openclaw config get agents.entries` | all 7 live agents (`ai`, `claude`, `devops`, `elinvo`, `main`, `security`, `snoutzone`) are claude-cli; `defaults.subagents.allowAgents` and every orchestrator admit only `claude` | 2026-10-13T23:48Z | VERIFIED. P6 is BLOCKED on the live host (fewer than two claude-cli targets admitted) |
| S1 `sessions_spawn` accepts `agentId` + `cwd`; return payload | orchestrator `e2e-orch` asked to spawn `e2e-a` with `cwd` and `task` | accepted `agentId`, `cwd`, `task`; returned `{status: accepted, childSessionKey, runId, mode: run, expectsCompletionMessage: true, context: isolated}` | 2026-10-14T00:40Z | VERIFIED (isolated gateway) |
| S2 child `pwd` and toplevel equal the worktree; report written | child ran `pwd` and `git rev-parse --show-toplevel`; script compared | pwd and toplevel equal the worktree, nonce matches, report and sentinel present, git state unchanged | 2026-10-14T00:39Z | VERIFIED (isolated gateway) |
| S3 status tool / completion announcement / readable final message | `sessions --all-agents --json` before/after plus the orchestrator reply | child session present in the store; sentinel written; final message readable. A terminal state is not guaranteed: treat the sentinel as the signal | 2026-10-14T00:39Z | RECORDED with the stated limitation |
| S5 a spawned child can itself call `sessions_spawn` | orchestrator asked a child to spawn | refused by the gateway: `{"status":"forbidden","error":"agentId is not allowed for sessions_spawn (allowed: none)"}` | 2026-10-14T00:40Z | VERIFIED NEGATIVE |

### Dispatch evidence (P6)

| Probe | backend | agentId | handle | cwd vs pwd vs toplevel | nonce | attempt | checks 1-4 | verdict |
|-------|---------|---------|--------|------------------------|-------|---------|------------|---------|
| sequential A: explore on e2e-a | openclaw-agent | e2e-a | orchestrator session `seq-a` | equal | match | 1 | ok | PASS |
| sequential B: code-reviewer on e2e-b (Haiku 4.5) | openclaw-agent | e2e-b | orchestrator session `seq-b` | sentinel carried no pwd/toplevel | missing | 1 | 1-4 ok, report with front matter `step=p6-review status=success` | **FAIL** (sentinel fields missing; the dispatch itself worked) |
| parallel A: explore on e2e-a | openclaw-agent | e2e-a | orchestrator session `par` | equal | match | 1 | ok | PASS |
| parallel B: code-reviewer on e2e-b | openclaw-agent | e2e-b | orchestrator session `par` | equal | match | 1 | ok, handoff hash unchanged | PASS |
| negative probe (no `sessions_spawn`) | claude-code | e2e-a (never spawned) | n/a | n/a | n/a | 1 | exact Blocked text | PASS |

Reading of the one FAIL: the same role and agent passed in the parallel run, so the sequential-B failure is the
model not writing the sentinel fields, not a dispatch failure. It supports the existing design rule that the
orchestrator verifies the report and the post-step checks itself and never trusts a child's own claim.

### Limits of this evidence

- The isolated gateway is not the live gateway: the live host still admits only `claude`, so a live P6 needs the
  operator to add claude-cli agents to `allowAgents` (not done; operator-owned).
- The table above exercised only claude-cli children. The `agy-cli` child is covered in the next section; a Codex child was not exercised, by design.
- Five probes, one run each: this is existence evidence, not a reliability rate.

## agy-cli evidence

Measured on 2026-10-14 (UTC) against an isolated gateway (`kite2e`, loopback 19789, own cgroup), seal
`df652532b8f59a00402d9f9751acadd3601245adeeed9acc8f5cfc47239582b8`, run `20261014T191727Z`. Orchestrator `e2e-orch`
is claude-cli; targets `e2e-agy` and `e2e-agy-direct` use the model ref `agy-cli/gemini-3.8-flash-low`
(Antigravity CLI 1.3.5). Each probe used a fresh sandbox clone pinned to commit `73fdb0d`. Proof of an untouched
live host (`proof-after`, Overall PASS): 93 L1 sha256 entries identical, live MainPID 962 and NRestarts 0 unchanged,
no `message` call (2 shim calls, both `--profile kite2e --version`), 0 live message deliveries in any probe window,
live `oom_kill` +0 and `memory.events high` +0.

| Probe | What it does | Result | Verdict |
|-------|--------------|--------|---------|
| direct-agy | one gateway turn to `e2e-agy-direct`, no `sessions_spawn` | `executionTrace.winnerProvider` `agy-cli`, runner `cli`; sentinel valid, nonce, cwd = toplevel = workspace, check 4 PASS | UNVERIFIED (fdscan sampled no agy pid in the window; the functional evidence is complete) |
| seq-agy | orchestrator `sessions_spawn` of `e2e-agy`, explore step, explicit `cwd` | checks 1-4 ok, `step=agy-explore status=success`, role line ok, nonce match | PASS |
| neg-agy | spawn of `e2e-agy-denied` (not on `allowAgents`) | refused: spawned=false, no sentinel, no session | PASS |

Findings that change how an agy-backed agent must be configured:

1. **Model catalog.** `sessions_spawn` resolves the target agent's model through the model catalog and does not
   consult the CLI-backend check. A ref `agy-cli/<model>` therefore needs a `models.providers["agy-cli"].models[]`
   entry (the schema also demands `baseUrl`, `api` and `apiKey`; a dead loopback `baseUrl` and a placeholder key
   fail closed because agy turns take the CLI runner). Without it the spawn is refused with `Unknown model`
   although config validation passes.
2. **Process environment.** Under `sessions_spawn` the agy process does not necessarily have the guard shim first
   in its own `PATH` while its shell tool does. A check that requires the shim first in the agy process `PATH`
   gives a false HIT (run `20261014T190726Z`); the first executable `openclaw` on the walked `PATH` is the right test.
3. **Workspace files.** OpenClaw writes bootstrap files (`IDENTITY.md`, `SOUL.md`, `USER.md`) into the agent
   workspace before an agy turn. A clean-worktree check must tolerate untracked bootstrap files and nothing else.
4. **Cost.** One agy step is about 50k tokens of Antigravity quota, mostly system context.

### Limits of the agy-cli evidence

- Isolated gateway only. The live host admits only `claude`, so using an agy-backed agent there needs the
  operator to add the agent, its catalog entry and the `allowAgents` membership (not done; operator-owned).
- Three probes, one run each: existence evidence, not a reliability rate. A first run on the same day was refused
  at the catalog (finding 1) and a second was aborted by the over-strict PATH check (finding 2); both are
  recorded by the run directories of the harness.
- `agy` runs with `--dangerously-skip-permissions` as the host user. The harness detects but does not prevent a
  write outside the sandbox, or a call to the live `openclaw` binary by absolute path.
- Antigravity's FAQ says third-party tools that access it violate its terms of service. The operator ordered
  this test knowingly and accepted the account risk.
- Not signed off: only a Verifier confirms acceptance.
