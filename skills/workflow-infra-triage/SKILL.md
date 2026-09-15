---
name: workflow-infra-triage
description: "User reports broken infrastructure, resources not syncing, provider errors, Crossplane composition failures, or needs to debug and stabilize IaC resources. Runs the kit workflow 'infra-triage': Infrastructure triage and stabilization. Diagnose broken IaC resources, fix provider/composition issues, monitor until healthy."
argument-hint: "[goal]"
---

<!-- Generated from workflows/infra-triage.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: infra-triage

Infrastructure triage and stabilization. Diagnose broken IaC resources, fix provider/composition issues, monitor until healthy.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/infra-triage.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `investigate-cluster` | `generalPurpose` | investigate |
| `investigate-code` | `generalPurpose` | investigate |
| `analyze` | `software-architect` | — |
| `implement` | `implementer` | — |
| `review` | `code-reviewer` | post-fix |
| `monitor` | `generalPurpose` | post-fix |
| `verify` | `verifier` | — |
| `document` | `doc-writer` | — |
