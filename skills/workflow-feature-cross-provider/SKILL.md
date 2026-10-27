---
name: workflow-feature-cross-provider
description: "User asks to implement a feature and wants the team's provider mix spelled out step by step (explicit request only; the plain feature workflow is the default). Runs the kit workflow 'feature-cross-provider': Feature workflow for a mixed-provider team. Every step is routed through the matrix; review runs in parallel with the security audit."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/feature-cross-provider.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: feature-cross-provider

Feature workflow for a mixed-provider team. Every step is routed through the matrix; review runs in parallel with the security audit.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff); the skill holds the fallbacks and the tests rule.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/feature-cross-provider.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `research` | `explore` | route | — |
| `plan` | `planner` | route | — |
| `architect` | `software-architect` | route | — |
| `implement` | `implementer` | route | — |
| `test` | `tester` | route | — |
| `review` | `code-reviewer` | route | post-test |
| `security` | `security-auditor` | route | post-test |
| `verify` | `verifier` | route | — |
| `document` | `doc-writer` | route | — |
