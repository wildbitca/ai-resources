---
name: workflow-dependency-audit
description: "User asks to audit dependencies, check for vulnerabilities, update packages, upgrade libraries, review CVEs, check outdated deps, or reduce bundle size. Runs the kit workflow 'dependency-audit': Audit dependencies for vulnerabilities and outdated packages, plan upgrades by risk, apply and verify."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/dependency-audit.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: dependency-audit

Audit dependencies for vulnerabilities and outdated packages, plan upgrades by risk, apply and verify.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; scores that are missing or invalid mean FULL.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/dependency-audit.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `audit` | `explore` | claude-code | — |
| `plan` | `generalPurpose` | claude-code | — |
| `upgrade` | `implementer` | claude-code | — |
| `test` | `tester` | claude-code | — |
