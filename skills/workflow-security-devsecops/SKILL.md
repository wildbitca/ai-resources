---
name: workflow-security-devsecops
description: "User asks for security audit, pen test, vulnerability scan, DevSecOps review, or exploit validation. Runs the kit workflow 'security-devsecops': DevSecOps audit — recon, SAST, dependency scan, secret detection, DAST, exploit validation, report."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/security-devsecops.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: security-devsecops

DevSecOps audit — recon, SAST, dependency scan, secret detection, DAST, exploit validation, report.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff). Tests are mandatory in every mode; scores that are missing or invalid mean FULL.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/security-devsecops.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `recon` | `security-auditor` | claude-code | — |
| `sast` | `security-auditor` | claude-code | — |
| `dependency-scan` | `security-auditor` | claude-code | — |
| `secret-detection` | `security-auditor` | claude-code | — |
| `dast-probe` | `security-auditor` | claude-code | — |
| `exploit-validation` | `security-auditor` | claude-code | — |
| `report` | `verifier` | claude-code | — |
| `document` | `doc-writer` | claude-code | — |
