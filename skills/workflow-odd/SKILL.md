---
name: workflow-odd
description: "User asks for one concrete change and wants the least delegation that is still safe (a fix, a small feature, a rename, a config or doc change), or says 'odd', 'size it first' or 'keep it light'. Runs the kit workflow 'odd': Size-gated delivery for one change. Classify (inline, light, full), keep a temporal tasks ledger next to the spec, implement inline unless a delegation trigger fires, verify by risk, close by folding the ledger into the spec."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/odd.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: odd

Size-gated delivery for one change. Classify (inline, light, full), keep a temporal tasks ledger next to the spec, implement inline unless a delegation trigger fires, verify by risk, close by folding the ledger into the spec.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/odd.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `classify` | `planner` | claude-code | — |
| `implement` | `implementer` | claude-code | — |
| `verify` | `verifier` | claude-code | — |
| `close` | `doc-writer` | claude-code | — |
