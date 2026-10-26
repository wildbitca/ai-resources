---
name: workflow-cloud-ops
description: "User asks to query, check, manage, or integrate with cloud resources (AWS, GCP, Azure) or external APIs (Stripe, Twilio, GitHub, Slack, etc.). Includes cost checks, IAM, storage, queues, DNS, certificates. Runs the kit workflow 'cloud-ops': Cloud resource query, management, and external API integration. Gather resource state → analyze → optionally act."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/cloud-ops.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: cloud-ops

Cloud resource query, management, and external API integration. Gather resource state → analyze → optionally act.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; scores that are missing or invalid mean FULL.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/cloud-ops.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `query` | `explore` | claude-code | — |
| `analyze` | `generalPurpose` | claude-code | — |
| `act` | `implementer` | claude-code | — |
