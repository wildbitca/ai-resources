---
name: workflow-k8s-debug
description: "User asks to debug, monitor, or investigate Kubernetes resources — pods, deployments, services, ingress, nodes, events, PVCs, HPA, RBAC, or cluster health. Runs the kit workflow 'k8s-debug': Kubernetes cluster debugging and monitoring. Gathers cluster state, diagnoses root cause, optionally applies fix."
argument-hint: "[goal]"
---

<!-- Generated from workflows/k8s-debug.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: k8s-debug

Kubernetes cluster debugging and monitoring. Gathers cluster state, diagnoses root cause, optionally applies fix.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/k8s-debug.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `gather` | `explore` | — |
| `diagnose` | `generalPurpose` | — |
| `fix` | `implementer` | — |
