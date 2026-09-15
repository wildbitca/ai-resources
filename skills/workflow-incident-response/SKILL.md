---
name: workflow-incident-response
description: "User reports a production incident, outage, degraded service, SLO breach, alert storm, or asks for emergency rollback or hotfix. Runs the kit workflow 'incident-response': Full incident lifecycle — triage (gather signals) → diagnose (root cause) → fix (apply resolution) → document (postmortem)."
argument-hint: "[goal]"
---

<!-- Generated from workflows/incident-response.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: incident-response

Full incident lifecycle — triage (gather signals) → diagnose (root cause) → fix (apply resolution) → document (postmortem).

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/incident-response.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `triage` | `explore` | — |
| `diagnose` | `generalPurpose` | — |
| `fix` | `implementer` | — |
| `document` | `doc-writer` | — |
