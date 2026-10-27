---
name: workflow-log-triage
description: "User asks to search logs, analyze errors in logs, find what caused something in logs, trace a request, investigate log anomalies, or understand log patterns. Runs the kit workflow 'log-triage': Collect, filter, and analyze logs from any source to identify error patterns, root causes, or user journeys."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/log-triage.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: log-triage

Collect, filter, and analyze logs from any source to identify error patterns, root causes, or user journeys.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; invalid scores mean FULL, and no scores plus no classifier answer mean light.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/log-triage.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `collect` | `explore` | claude-code | — |
| `analyze` | `generalPurpose` | claude-code | — |
