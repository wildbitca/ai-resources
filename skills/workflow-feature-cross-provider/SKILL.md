---
name: workflow-feature-cross-provider
description: "User asks to implement a feature and wants part of the team to run on a different OpenClaw agent or provider (explicit request only; the plain feature workflow is the default). Runs the kit workflow 'feature-cross-provider': Feature workflow with a mixed team. research and plan run on other OpenClaw agents, review runs on another agent in parallel with the Claude Code security audit."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/feature-cross-provider.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: feature-cross-provider

Feature workflow with a mixed team. research and plan run on other OpenClaw agents, review runs on another agent in parallel with the Claude Code security audit.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/feature-cross-provider.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `research` | `explore` | openclaw-agent (`agy-pro`) | — |
| `plan` | `planner` | openclaw-agent (`agy-flash`) | — |
| `architect` | `software-architect` | claude-code | — |
| `implement` | `implementer` | claude-code | — |
| `test` | `tester` | claude-code | — |
| `review` | `code-reviewer` | openclaw-agent (`agy-flash`) | post-test |
| `security` | `security-auditor` | claude-code | post-test |
| `verify` | `verifier` | claude-code | — |
| `document` | `doc-writer` | claude-code | — |
