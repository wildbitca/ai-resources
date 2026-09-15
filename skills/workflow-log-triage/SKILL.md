---
name: workflow-log-triage
description: "User asks to search logs, analyze errors in logs, find what caused something in logs, trace a request, investigate log anomalies, or understand log patterns. Runs the kit workflow 'log-triage': Collect, filter, and analyze logs from any source to identify error patterns, root causes, or user journeys."
argument-hint: "[goal]"
---

<!-- Generated from workflows/log-triage.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: log-triage

Collect, filter, and analyze logs from any source to identify error patterns, root causes, or user journeys.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/log-triage.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `collect` | `explore` | — |
| `analyze` | `generalPurpose` | — |
