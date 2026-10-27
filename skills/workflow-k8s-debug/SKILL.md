---
name: workflow-k8s-debug
description: "User asks to debug, monitor, or investigate Kubernetes resources — pods, deployments, services, ingress, nodes, events, PVCs, HPA, RBAC, or cluster health. Runs the kit workflow 'k8s-debug': Kubernetes cluster debugging and monitoring. Gathers cluster state, diagnoses root cause, optionally applies fix."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/k8s-debug.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: k8s-debug

Kubernetes cluster debugging and monitoring. Gathers cluster state, diagnoses root cause, optionally applies fix.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff); the skill holds the fallbacks and the tests rule.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/k8s-debug.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `gather` | `explore` | claude-code | — |
| `diagnose` | `generalPurpose` | claude-code | — |
| `fix` | `implementer` | claude-code | — |
