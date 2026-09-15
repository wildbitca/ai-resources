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
4. **Join.** After every step in the group has returned, the orchestrator copies those fields into the handoff, then applies rollback: if any step reports a blocking verdict (e.g. `REQUIRES_CHANGES`, `FAIL`), set `Blocked: true`, `Block_reason` from that report, and `Return_to_step` to that step's `on_concern_return_to` before starting the next step.

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
