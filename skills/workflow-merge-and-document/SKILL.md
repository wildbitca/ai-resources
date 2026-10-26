---
name: workflow-merge-and-document
description: "User asks to merge a branch and write docs, SDD specs, or handoff documents. Includes 'merge + document', 'merge and write specs/docs', 'merge and update SDD'. Runs the kit workflow 'merge-and-document': Merge feature branch to develop and write/update SDD specs. Delegates all heavy work to cheap models."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/merge-and-document.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: merge-and-document

Merge feature branch to develop and write/update SDD specs. Delegates all heavy work to cheap models.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; scores that are missing or invalid mean FULL.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/merge-and-document.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `gather-context` | `explore` | claude-code | — |
| `document` | `doc-writer` | claude-code | — |
| `merge` | `implementer` | claude-code | — |
