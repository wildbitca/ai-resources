---
name: workflow-feature-implementation
description: "User asks to implement a feature, new functionality, or multi-step task. Runs the kit workflow 'feature-implementation': Domain-agnostic feature workflow. research → plan → architect → implement → test → (review ‖ security) → verify."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/_feature-template.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: feature-implementation

Domain-agnostic feature workflow. research → plan → architect → implement → test → (review ‖ security) → verify.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; scores that are missing or invalid mean FULL.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/_feature-template.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `research` | `generalPurpose` | claude-code | — |
| `plan` | `planner` | claude-code | — |
| `architect` | `software-architect` | claude-code | — |
| `implement` | `implementer` | claude-code | — |
| `test` | `tester` | claude-code | — |
| `review` | `code-reviewer` | claude-code | post-test |
| `security` | `security-auditor` | claude-code | post-test |
| `verify` | `verifier` | claude-code | — |
| `document` | `doc-writer` | claude-code | — |

## Deterministic alternative

`/kit-plan <goal>` then `/kit-implement` runs this loop as workflow scripts: one writer, a test gate, parallel review lenses and findings verified before they are fixed. Prefer it unless a step needs judgement mid-run (workflow scripts cannot ask the user anything until they finish).
