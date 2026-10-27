---
name: workflow-infra-triage
description: "User reports broken infrastructure, resources not syncing, provider errors, Crossplane composition failures, or needs to debug and stabilize IaC resources. Runs the kit workflow 'infra-triage': Infrastructure triage and stabilization. Diagnose broken IaC resources, fix provider/composition issues, monitor until healthy."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/infra-triage.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: infra-triage

Infrastructure triage and stabilization. Diagnose broken IaC resources, fix provider/composition issues, monitor until healthy.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff); the skill holds the fallbacks and the tests rule.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/infra-triage.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `investigate-cluster` | `generalPurpose` | claude-code | investigate |
| `investigate-code` | `generalPurpose` | claude-code | investigate |
| `analyze` | `software-architect` | claude-code | — |
| `implement` | `implementer` | claude-code | — |
| `review` | `code-reviewer` | claude-code | post-fix |
| `monitor` | `generalPurpose` | claude-code | post-fix |
| `verify` | `verifier` | claude-code | — |
| `document` | `doc-writer` | claude-code | — |
