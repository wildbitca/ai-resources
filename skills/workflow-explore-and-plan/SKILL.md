---
name: workflow-explore-and-plan
description: "User wants to understand the codebase or get a plan before implementing. Runs the kit workflow 'explore-and-plan': Discovery and planning only (no implementation). Works for any domain."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/explore-and-plan.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: explore-and-plan

Discovery and planning only (no implementation). Works for any domain.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). The skill also holds the fallbacks (invalid scores mean FULL) and the rule that tests are mandatory in every mode.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/explore-and-plan.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `explore` | `generalPurpose` | claude-code | — |
| `plan` | `planner` | claude-code | — |
