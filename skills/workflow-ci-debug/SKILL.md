---
name: workflow-ci-debug
description: "User reports a failing CI pipeline, broken GitHub Actions workflow, failing deployment job, flaky test in CI, or CI environment error. Runs the kit workflow 'ci-debug': Diagnose and fix CI/CD pipeline failures. Fetch CI logs → identify failing step → fix cause."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/ci-debug.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: ci-debug

Diagnose and fix CI/CD pipeline failures. Fetch CI logs → identify failing step → fix cause.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). The skill also holds the fallbacks (invalid scores mean FULL) and the rule that tests are mandatory in every mode.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/ci-debug.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `fetch` | `explore` | claude-code | — |
| `diagnose` | `generalPurpose` | claude-code | — |
| `fix` | `implementer` | claude-code | — |
