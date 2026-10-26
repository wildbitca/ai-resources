---
name: workflow-refactor
description: "User asks to refactor, clean up, restructure, deduplicate, rename, extract, simplify, reduce coupling, improve readability, or pay down technical debt — without adding new functionality. Runs the kit workflow 'refactor': Structured code refactoring — assess technical debt → plan atomic steps → implement without behavior change → verify no regressions."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/refactor.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: refactor

Structured code refactoring — assess technical debt → plan atomic steps → implement without behavior change → verify no regressions.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/refactor.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `assess` | `explore` | claude-code | — |
| `plan` | `planner` | claude-code | — |
| `implement` | `implementer` | claude-code | — |
| `test` | `tester` | claude-code | — |
| `review` | `code-reviewer` | claude-code | — |

## Deterministic alternative

`/kit-plan` with kind "refactor", then `/kit-implement` runs this loop as workflow scripts: one writer, a test gate, parallel review lenses and findings verified before they are fixed. Prefer it unless a step needs judgement mid-run (workflow scripts cannot ask the user anything until they finish).

Score first: before the first step run the scorecard (`kit-orchestration`, "Scorecard"); it picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the scorecard names them. Tests are mandatory in every mode.
