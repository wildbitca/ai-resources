---
name: workflow-db-investigation
description: "User asks to investigate slow queries, missing indexes, N+1 queries, database schema, migration conflicts, data integrity issues, connection pool problems, or DB performance. Runs the kit workflow 'db-investigation': Investigate database performance, schema, data integrity, or migration issues. Inspect → analyze → fix."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/db-investigation.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: db-investigation

Investigate database performance, schema, data integrity, or migration issues. Inspect → analyze → fix.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; scores that are missing or invalid mean FULL.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/db-investigation.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `inspect` | `explore` | claude-code | — |
| `analyze` | `generalPurpose` | claude-code | — |
| `fix` | `implementer` | claude-code | — |
