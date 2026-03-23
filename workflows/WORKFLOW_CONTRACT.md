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

Handoff files and placeholders (e.g. `{{workspace}}`, `{{handoff_file}}`) are documented in workflow comments and **`rules/051-handoff-protocol.mdc`**.

See **`workflows/_feature-template.workflow.yaml`** for a full example.
