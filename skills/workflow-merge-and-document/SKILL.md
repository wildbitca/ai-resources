---
name: workflow-merge-and-document
description: "User asks to merge a branch and write docs, SDD specs, or handoff documents. Includes 'merge + document', 'merge and write specs/docs', 'merge and update SDD'. Runs the kit workflow 'merge-and-document': Merge feature branch to develop and write/update SDD specs. Delegates all heavy work to cheap models."
argument-hint: "[goal]"
---

<!-- Generated from workflows/merge-and-document.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: merge-and-document

Merge feature branch to develop and write/update SDD specs. Delegates all heavy work to cheap models.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/merge-and-document.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `gather-context` | `explore` | — |
| `document` | `doc-writer` | — |
| `merge` | `implementer` | — |
