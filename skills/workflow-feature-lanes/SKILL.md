---
name: workflow-feature-lanes
description: "User asks to implement a feature and wants the team to run on subscription lanes (claude-cli lane agents, and Antigravity agents once that lane is open) under the kit's verified landing path (explicit request only; the plain feature workflow is the default). Runs the kit workflow 'feature-lanes': Feature workflow on subscription lanes. Every role is a lane step resolved from the setup lanes table; scope writers are verified in sequence, the implementer integrates, review has a second member and the kit lands the result."
argument-hint: "[goal]"
---

<!-- Generated from workflows/feature-lanes.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: feature-lanes

Feature workflow on subscription lanes. Every role is a lane step resolved from the setup lanes table; scope writers are verified in sequence, the implementer integrates, review has a second member and the kit lands the result.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/feature-lanes.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `research` | `explore` | lane (resolved at dispatch from the setup lanes table) | — |
| `plan` | `planner` | lane (resolved at dispatch from the setup lanes table) | — |
| `architect` | `software-architect` | lane (resolved at dispatch from the setup lanes table) | — |
| `tests` | `tester` | lane (resolved at dispatch from the setup lanes table), write scope: tests/ | scope-writers |
| `docs` | `doc-writer` | lane (resolved at dispatch from the setup lanes table), write scope: docs/ | scope-writers |
| `verify-tests` | `verifier` | lane (resolved at dispatch from the setup lanes table) | — |
| `verify-docs` | `verifier` | lane (resolved at dispatch from the setup lanes table) | — |
| `implement` | `implementer` | lane (resolved at dispatch from the setup lanes table), integrating writer | — |
| `verify-implement` | `verifier` | lane (resolved at dispatch from the setup lanes table) | — |
| `review` | `code-reviewer` | lane (resolved at dispatch from the setup lanes table) | review |
| `review-second` | `code-reviewer` | lane (resolved at dispatch from the setup lanes table), second member | review |
| `security` | `security-auditor` | lane (resolved at dispatch from the setup lanes table) | — |
| `verify-final` | `verifier` | lane (resolved at dispatch from the setup lanes table) | — |
| `document` | `doc-writer` | claude-code | — |
