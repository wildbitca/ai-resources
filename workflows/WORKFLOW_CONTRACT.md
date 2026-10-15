# Workflow YAML contract

Workflow files live under **`workflows/*.workflow.yaml`**. `ai-resources generate` turns each one into a `workflow-<name>` skill (its `trigger` becomes the skill description), and the **`kit-orchestration`** skill defines how an agent runs the steps.

## Top-level fields (typical)

| Field           | Required    | Notes                                            |
|-----------------|-------------|--------------------------------------------------|
| `name`          | yes         | Stable id for the workflow                       |
| `description`   | yes         | Human summary                                    |
| `domain`        | often       | Placeholder e.g. `{{domain}}` or fixed domain id |
| `trigger`       | recommended | When the orchestrator should select this file    |
| `prerequisites` | optional    | List                                             |
| `steps`         | yes         | Ordered list of step objects                     |
| `integrating_writer` | optional | Role that integrates lane work, default `implementer` (see "Lane steps")  |

## Step object (typical)

| Field                  | Required | Notes                                                                                      |
|------------------------|----------|--------------------------------------------------------------------------------------------|
| `id`                   | yes      | Step id (e.g. `research`, `implement`)                                                     |
| `subagent_type`        | yes      | e.g. `planner`, `implementer`, `generalPurpose`                                            |
| `skills`               | yes      | List of flat skill ids — `$AGENT_KIT/skills/<id>/SKILL.md` (see skill **`kit-orchestration`**) |
| `entry_criteria`       | optional |                                                                                            |
| `exit_criteria`        | optional |                                                                                            |
| `handoff_to`           | optional | Next step id                                                                               |
| `on_concern_return_to` | optional |                                                                                            |
| `prompt_template`      | optional | Multi-line template for Task prompts                                                       |
| `execution_hints`      | optional | Runtime hints for parallelism and coordination (see below)                                 |
| `backend`              | optional | `claude-code` (default), `openclaw-agent` or `lane` — where the role runs (see "Step backend") |
| `agentId`              | with `openclaw-agent` | OpenClaw agent id, `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`; an error on any other backend |
| `write_scope`          | `lane` scope writers | Repository-relative path prefixes the step may write (see "Lane steps")        |
| `integrates`           | `lane` integrating step | Scope step ids the kit merges for this step (see "Lane steps")            |
| `verifies`             | `verifier` steps of a lane chain | Step ids the verifier checks (see "Lane steps")                   |

## Step backend (optional)

`backend` decides which runtime runs the step's role. Omitting it means `claude-code`, which is the behaviour every workflow had before the field existed. There is no `model` field: the model belongs to whatever runs the role (the role's `by_role` executor for `claude-code`, the agent's configured model for `openclaw-agent`).

| Backend          | Runs the role on                                                         | Dispatched by                                              |
|------------------|--------------------------------------------------------------------------|------------------------------------------------------------|
| `claude-code`    | The kit role subagent (today's behaviour)                                | Any runtime that runs kit role subagents                   |
| `openclaw-agent` | The OpenClaw agent named by `agentId`, through `sessions_spawn`          | Only an OpenClaw-run orchestrator that has `sessions_spawn` |
| `lane`           | Whatever the setup lanes table resolves for the role (claude-code or a lane agent), under the kit's `ai-resources lanes` commands | The orchestrator, at dispatch (ADR-0007) |

| Runtime                          | `openclaw-agent` behavior                                              |
|----------------------------------|------------------------------------------------------------------------|
| OpenClaw-run orchestrator        | Spawns the agent with an explicit `cwd`, polls, runs the post-step check |
| Claude Code, Cursor, others      | The step is `Blocked`; there is no fallback to `claude-code`           |

Validation rules (`scripts/validate_kit.py`):

- `backend` must be `claude-code`, `openclaw-agent` or `lane`; `agentId` is required for `openclaw-agent` and invalid otherwise (so it is forbidden on `lane`).
- One writer per run: the implementer and every other write-capable role are rejected on `openclaw-agent`. `planner`, `explore`, `software-architect`, `code-reviewer`, `security-auditor` and `verifier` are allowed. `tester` and `doc-writer` are allowed with a warning, because they run commands or write files and the post-step check detects a violation but cannot prevent one.
- Whether the agent exists and is on `subagents.allowAgents` is checked when the step is dispatched, never at validation time.

The dispatch procedure, the child prompt, waiting and retries are in the **`kit-orchestration`** skill; the design is in `docs/decisions/0005-cross-provider-role-dispatch.md`.

## Lane steps (ADR-0007)

`backend: lane` does not name an agent. At dispatch the orchestrator resolves the role from the setup lanes table (`~/.config/ai-resources/lanes.yaml`) to `claude-code` or to a lane agent; an absent table or entry means `claude-code`. The role must have a write contract in `scripts/ai_resources/roles_policy.py` (`LANE_ELIGIBILITY`); an unknown role is rejected.

| Field / hint | Where | Meaning |
|---|---|---|
| `integrating_writer` | workflow level | The one role that integrates lane work; defaults to `implementer`. At most one per workflow, and its `lane` steps are never parallel members |
| `write_scope` | `lane` step of a scope-capable role | Path prefixes the step may write, in its own per-attempt worktree. Relative, normalised, no `..`, no glob, not `.`, never under `.git`, `.claude/`, `.cursor/`, `.openclaw/`, `.githooks/`, `.agent-output/` or the handoff file |
| `integrates` | the integrating writer's `lane` step | Scope step ids the kit merges (`lanes integrate`) into that step's integration worktree; each must run earlier and carry a `write_scope` |
| `verifies` | `verifier` step | Step ids the verifier checks; a Verifier verdict counts only through this step's own verify record bound to `<step-id>@<sha>` |
| `execution_hints.lane_member: second` | `lane` step of a report-contract role in a parallel group | The parallel second member of that role (for example a second code-reviewer) |

Validation rules:

- A step without `write_scope` that is not the integrating writer runs under the **report** contract (it writes only `.agent-output/<role>/<step-id>*`); a `write_scope` step runs under the **scope** contract; the integrating writer's step runs under the **integrating** contract.
- Parallel scope writers need pairwise disjoint scopes. A `claude-code` writer step may not share a parallel group with a `lane` step; a `claude-code` report-contract role may.
- Every `write_scope` step is followed by a `verifier` step that names it in `verifies`, then by an integrating step whose `integrates` names it, then by a `verifier` step that names the integrating step. The kit refuses `integrate` and `land` without those verdicts.
- `lane_member: second` is only valid for report-contract roles, in a parallel group that also holds a primary member of the same role.

**Handoff contract for lane steps.** Lane children never write `{{handoff_file}}` (a prompt that says `Update {{handoff_file}}` on a `lane` step is rejected). Every lane step, sequential or parallel, ends its step-scoped report (`.agent-output/<role>/<step-id>.md`) with the handoff fields it would have set, and the orchestrator joins them into the handoff front matter, exactly as for parallel members (rule 4 below). A Verifier report carries one line `Verdict: PASS`, `Verdict: FAIL` or `Verdict: REQUIRES_CHANGES`.

The kit commands (`ai-resources lanes prepare|verify|integrate|land|count`) give each lane writer its own per-attempt worktree, check the contract after the child ran and are the only code that merges or lands; the design is in `docs/decisions/0007-subscription-lanes.md`.

## Execution hints (optional)

Steps may include an `execution_hints` block with **optional** metadata that runtimes can use to optimize execution. Runtimes that don't support a hint silently ignore it and execute sequentially.

| Hint field       | Type   | Description                                                                                 |
|------------------|--------|---------------------------------------------------------------------------------------------|
| `parallel_group` | string | Steps sharing the same group name CAN run concurrently when the runtime supports it.        |

### How runtimes interpret hints

| Runtime       | `parallel_group` behavior                                                      |
|---------------|--------------------------------------------------------------------------------|
| Claude Code   | Steps in the same group run as concurrent subagents (several Agent calls in one turn) |
| Cursor        | Steps in the same group can be run as parallel Composer / Task calls           |
| Others        | Ignored — sequential execution (handoff file still works)                      |

### Rules for parallel steps

1. **No intra-group dependencies.** Steps in a group share the same `entry_criteria`; no step may require another step of the same group to have finished (e.g. security must not wait for review approval).
2. **Gates stay sequential.** A step whose failure must stop the others (e.g. `test`) is not part of the group; it runs before it.
3. **No shared handoff writes.** Concurrent steps must not edit `{{handoff_file}}` (last writer wins). Each writes its own report under `.agent-output/<role>/` and ends it with the handoff fields it would have set.
4. **Join.** After every step in the group has returned, the orchestrator copies those fields into the handoff — into the YAML front matter (`verdicts.*`, `refs.*`, `status`, `step`, `next`), which is the authoritative state; the prose notes below it are commentary. Then it applies rollback: if any step reports a blocking verdict (e.g. `REQUIRES_CHANGES`, `FAIL`), set `blocked: true`, `block_reason` from that report, and `return_to_step` to that step's `on_concern_return_to` before starting the next step.

5. **`openclaw-agent` members.** Only the six read-only roles (`planner`, `explore`, `software-architect`, `code-reviewer`, `security-auditor`, `verifier`) may be parallel members on that backend; a `tester` or `doc-writer` is a gate or a document step and is never one. A member writes only under `.agent-output/<role>/`, and the orchestrator joins it like any other member.

**Example:**

```yaml
- id: test            # gate: runs first, alone
  subagent_type: tester
- id: review
  subagent_type: code-reviewer
  entry_criteria: Tests pass
  execution_hints:
    parallel_group: post-test
- id: security
  subagent_type: security-auditor
  entry_criteria: Tests pass
  execution_hints:
    parallel_group: post-test
```

`review` and `security` share `post-test`, so a capable runtime runs them concurrently once `test` passes. The `verify` step (no hint) runs after the join.

Handoff files and placeholders (e.g. `{{workspace}}`, `{{handoff_file}}`) are documented in workflow comments and the **`kit-orchestration`** skill (`references/handoff.md`).

See **`workflows/_feature-template.workflow.yaml`** for a full example.
