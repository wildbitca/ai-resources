---
name: workflow-bugfix
description: "User asks to fix a bug, resolve an issue, or correct broken behavior. Runs the kit workflow 'bugfix': Domain-agnostic bugfix workflow. research → explore → implement → test → security → verify."
argument-hint: "[goal]"
---

<!-- Generated from workflows/_bugfix-template.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: bugfix

Domain-agnostic bugfix workflow. research → explore → implement → test → security → verify.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/_bugfix-template.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `research` | `generalPurpose` | — |
| `explore` | `generalPurpose` | — |
| `implement` | `implementer` | — |
| `test` | `tester` | — |
| `security` | `security-auditor` | — |
| `verify` | `verifier` | — |
