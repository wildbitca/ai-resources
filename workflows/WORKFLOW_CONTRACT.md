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
| `backend`              | optional | `route` (default), or an override: `claude-code`, `openclaw-agent`, `agy` (see "Step backend") |
| `agentId`              | with `openclaw-agent` | OpenClaw agent id, `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`; an error on any other backend |

## Step backend (optional)

`backend` decides which runtime runs the step's role. Omitting it means `route`: the role resolves through the routing matrix (`ai-resources route <role> --run $RUN`: role -> primary -> fallback, live pool usage, checker independence from the writer families recorded for the run) and the result names the runtime. `claude-code` is one possible result, never a default. There is no `model` field: the model belongs to the matrix cell (or to the agent for `openclaw-agent`). There is no `lane` backend.

| Backend          | Runs the role on                                                         |
|------------------|--------------------------------------------------------------------------|
| `route`          | Whatever the matrix resolves for the role (default)                      |
| `claude-code`    | Explicit override: the kit role subagent (Task tool)                     |
| `openclaw-agent` | Explicit override: the OpenClaw agent named by `agentId`, through `sessions_spawn` |
| `agy`            | Explicit override: Antigravity, through `sessions_spawn` or `ai-resources agy run` |

| Runtime / route result                                          | Behavior                                                                  |
|-----------------------------------------------------------------|---------------------------------------------------------------------------|
| OpenClaw-run orchestrator, any route                            | `sessions_spawn` of the role's `kit-role-*` agent (or the named agent) with an explicit `cwd` |
| Claude Code, route resolves to a Claude cell                    | Task tool                                                                 |
| Claude Code, route resolves to agy                              | `ai-resources agy run --role <role>` (returns the report contract)        |
| `sessions_spawn` absent and the route needs it (`openclaw-agent`) | `Blocked: <cause>`, printed explicitly                                  |
| Route blocked (no independent checker, tool missing)            | `Blocked: <cause>`; never a silent switch to `claude-code`                |

A fallback runs only when the pool is exhausted or the model does not respond, and it is recorded with its cause (`ai-resources dispatch record`).

Validation rules (`scripts/validate_kit.py`):

- `backend` must be `route`, `claude-code`, `openclaw-agent` or `agy`; `agentId` is required for `openclaw-agent` and invalid otherwise.
- On `openclaw-agent`, only read-only roles are accepted without a warning: `planner`, `explore`, `software-architect`, `code-reviewer`, `security-auditor` and `verifier`. `tester` and `doc-writer` are allowed with a warning, because they run commands or write files and the post-step check detects a violation but cannot prevent one.
- Whether the agent exists and is on `subagents.allowAgents` is checked when the step is dispatched, never at validation time.

The dispatch procedure, the child prompt, waiting and retries are in the **`kit-orchestration`** skill; the design is in `docs/decisions/0005-cross-provider-role-dispatch.md`.

## Execution hints (optional)

Steps may include an `execution_hints` block with **optional** metadata that runtimes can use to optimize execution. Runtimes that don't support a hint silently ignore it and execute sequentially.

| Hint field       | Type   | Description                                                                                 |
|------------------|--------|---------------------------------------------------------------------------------------------|
| `parallel_group` | string | Steps sharing the same group name CAN run concurrently when the runtime supports it.        |
| `inline`         | bool   | `true`: the agent holding the plan performs the step itself and delegates only when a "Delegation triggers" row of `kit-orchestration` fires. Ignored by runtimes that always dispatch (the orchestrator CLI dispatches every step). |

### How runtimes interpret hints

| Runtime       | `parallel_group` behavior                                                      |
|---------------|--------------------------------------------------------------------------------|
| Claude Code   | Steps in the same group run as concurrent subagents (several Agent calls in one turn) |
| Cursor        | Steps in the same group can be run as parallel Composer / Task calls           |
| Others        | Ignored — sequential execution (handoff file still works)                      |

### Rules for parallel steps

1. **No intra-group dependencies.** Steps in a group share the same `entry_criteria`; no step may require another step of the same group to have finished (e.g. security must not wait for review approval).
2. **Gates stay sequential.** A step whose failure must stop the others (e.g. `test`) is not part of the group; it runs before it.
3. **No shared handoff writes.** Concurrent steps must not edit `{{handoff_file}}` (last writer wins). Each writes its own report under `{{run_dir}}/<role>/` and ends it with the handoff fields it would have set.
4. **Join.** After every step in the group has returned, the orchestrator copies those fields into the handoff — into the YAML front matter (`verdicts.*`, `refs.*`, `status`, `step`, `next`), which is the authoritative state; the prose notes below it are commentary. Then it applies rollback: if any step reports a blocking verdict (e.g. `REQUIRES_CHANGES`, `FAIL`), set `blocked: true`, `block_reason` from that report, and `return_to_step` to that step's `on_concern_return_to` before starting the next step.

5. **`openclaw-agent` members.** With that explicit override only the six read-only roles (`planner`, `explore`, `software-architect`, `code-reviewer`, `security-auditor`, `verifier`) may be parallel members on that backend; a `tester` or `doc-writer` is a gate or a document step and is never one. A member writes only under `{{run_dir}}/<role>/`, and the orchestrator joins it like any other member.

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

`{{run_dir}}` is the run's own output directory, `.agent-output/<run>`: `<run>` is the orchestrator's run id (a session that runs a workflow without the orchestrator uses `<workflow>-<yyyymmddThhmmss>-<pid>`). Two teams on one repository never share a run directory, a handoff file or a write scope (`scope_guard`), so neither can overwrite the other.

See **`workflows/_feature-template.workflow.yaml`** for a full example.

## Native orchestrator mapping (ADR-0015)

`ai-resources orchestrate` runs any workflow above as a team on OpenClaw. It reads the same fields and maps them like this:

| Field | In the orchestrator |
|---|---|
| `steps` order, `id`, `subagent_type`, `skills` | The step order, the `kit-role-<role>` agent each step is dispatched to, and the skill paths in the child prompt |
| `execution_hints.parallel_group` | Members dispatched together and joined, only when every member is a read-only role; otherwise the group runs sequentially and the run says so |
| `on_concern_return_to` | The step a `Blocked` reply returns to when it names none (`Return_to_step` wins); at most 3 returns per step, the 4th fails the run |
| `prompt_template` | Filled (`{{workspace}}` is the team's workspace, `{{handoff_file}}` the run's handoff) and prefixed with the orchestrated-run rules; "update the handoff" becomes "put the fields in the return block" |
| `backend`, `agentId` | Not used: every step is dispatched to the role's `kit-role-*` agent. Teams share the checkout and are kept apart by their write-scope claims (`ai-resources orchestrate start --param write_scope=src/,docs/`; default the whole checkout when a step writes) |
| First step run by a writer role (`implementer`, fixers, maintainers) | The plan approval pause: the run waits for `approve` before that step |
| `verifier` steps | The only sign-off: a `READY` from any other role is rejected; the verifier runs on its own agent and an independent model family |

`ai-resources workflows show <name> --json` prints the parsed steps, roles, groups and the approval gate.

