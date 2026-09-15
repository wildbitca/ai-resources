---
name: workflow-cloud-ops
description: "User asks to query, check, manage, or integrate with cloud resources (AWS, GCP, Azure) or external APIs (Stripe, Twilio, GitHub, Slack, etc.). Includes cost checks, IAM, storage, queues, DNS, certificates. Runs the kit workflow 'cloud-ops': Cloud resource query, management, and external API integration. Gather resource state → analyze → optionally act."
argument-hint: "[goal]"
---

<!-- Generated from workflows/cloud-ops.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: cloud-ops

Cloud resource query, management, and external API integration. Gather resource state → analyze → optionally act.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/cloud-ops.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `query` | `explore` | — |
| `analyze` | `generalPurpose` | — |
| `act` | `implementer` | — |
