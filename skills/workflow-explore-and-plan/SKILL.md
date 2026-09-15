---
name: workflow-explore-and-plan
description: "User wants to understand the codebase or get a plan before implementing. Runs the kit workflow 'explore-and-plan': Discovery and planning only (no implementation). Works for any domain."
argument-hint: "[goal]"
---

<!-- Generated from workflows/explore-and-plan.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: explore-and-plan

Discovery and planning only (no implementation). Works for any domain.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/explore-and-plan.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `explore` | `generalPurpose` | — |
| `plan` | `planner` | — |
