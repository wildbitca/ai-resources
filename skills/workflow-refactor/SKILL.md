---
name: workflow-refactor
description: "User asks to refactor, clean up, restructure, deduplicate, rename, extract, simplify, reduce coupling, improve readability, or pay down technical debt — without adding new functionality. Runs the kit workflow 'refactor': Structured code refactoring — assess technical debt → plan atomic steps → implement without behavior change → verify no regressions."
argument-hint: "[goal]"
---

<!-- Generated from workflows/refactor.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: refactor

Structured code refactoring — assess technical debt → plan atomic steps → implement without behavior change → verify no regressions.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/refactor.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `assess` | `explore` | — |
| `plan` | `planner` | — |
| `implement` | `implementer` | — |
| `test` | `tester` | — |
| `review` | `code-reviewer` | — |
