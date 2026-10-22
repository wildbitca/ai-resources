---
name: workflow-cross-domain-backend-infra
description: "User asks to deploy, change infra, or run a flow that touches both app and infra. Runs the kit workflow 'cross-domain-backend-infra': Task crossing app (backend) and infra — app → infra → test → review → verify."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/cross-domain-backend-infra.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: cross-domain-backend-infra

Task crossing app (backend) and infra — app → infra → test → review → verify.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/cross-domain-backend-infra.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `app-step` | `implementer` | claude-code | — |
| `infra-step` | `generalPurpose` | claude-code | — |
| `test` | `tester` | claude-code | — |
| `review` | `code-reviewer` | claude-code | — |
| `verify` | `verifier` | claude-code | — |
| `document` | `doc-writer` | claude-code | — |
