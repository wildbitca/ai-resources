# Workflow YAML contract

Workflow files live under **`workflows/*.workflow.yaml`**. They are consumed by **`rules/010-orchestrator.mdc`** and **`rules/011-orchestrator-reference.mdc`** (workflow map).

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
| `skills`               | yes      | List of flat skill ids — resolve to `SKILL.md` per **`rules/050-subagent-delegation.mdc`** |
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
| Claude Code   | Steps in the same group spawn as Agent Team teammates (or concurrent subagents)|
| Cursor        | Steps in the same group can be run as parallel Composer / Task calls           |
| Others        | Ignored — sequential execution (handoff file still works)                      |

**Example:**

```yaml
- id: test
  subagent_type: tester
  execution_hints:
    parallel_group: post-implement
- id: review
  subagent_type: code-reviewer
  execution_hints:
    parallel_group: post-implement
- id: security
  subagent_type: security-auditor
  execution_hints:
    parallel_group: post-implement
```

All three steps share `post-implement`, so a capable runtime may execute them concurrently after the `implement` step completes. The `verify` step (no hint) runs sequentially after the group finishes.

Handoff files and placeholders (e.g. `{{workspace}}`, `{{handoff_file}}`) are documented in workflow comments and **`rules/051-handoff-protocol.mdc`**.

See **`workflows/_feature-template.workflow.yaml`** for a full example.
