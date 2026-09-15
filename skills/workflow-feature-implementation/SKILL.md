---
name: workflow-feature-implementation
description: "User asks to implement a feature, new functionality, or multi-step task. Runs the kit workflow 'feature-implementation': Domain-agnostic feature workflow. research → plan → architect → implement → test → (review ‖ security) → verify."
argument-hint: "[goal]"
---

<!-- Generated from workflows/_feature-template.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: feature-implementation

Domain-agnostic feature workflow. research → plan → architect → implement → test → (review ‖ security) → verify.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/_feature-template.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `research` | `generalPurpose` | — |
| `plan` | `planner` | — |
| `architect` | `software-architect` | — |
| `implement` | `implementer` | — |
| `test` | `tester` | — |
| `review` | `code-reviewer` | post-test |
| `security` | `security-auditor` | post-test |
| `verify` | `verifier` | — |
| `document` | `doc-writer` | — |

## Deterministic alternative

`/kit-plan <goal>` then `/kit-implement` runs this loop as workflow scripts: one writer, a test gate, parallel review lenses and findings verified before they are fixed. Prefer it unless a step needs judgement mid-run (workflow scripts cannot ask the user anything until they finish).
