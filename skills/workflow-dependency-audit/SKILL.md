---
name: workflow-dependency-audit
description: "User asks to audit dependencies, check for vulnerabilities, update packages, upgrade libraries, review CVEs, check outdated deps, or reduce bundle size. Runs the kit workflow 'dependency-audit': Audit dependencies for vulnerabilities and outdated packages, plan upgrades by risk, apply and verify."
argument-hint: "[goal]"
---

<!-- Generated from workflows/dependency-audit.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: dependency-audit

Audit dependencies for vulnerabilities and outdated packages, plan upgrades by risk, apply and verify.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/dependency-audit.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `audit` | `explore` | — |
| `plan` | `generalPurpose` | — |
| `upgrade` | `implementer` | — |
| `test` | `tester` | — |
