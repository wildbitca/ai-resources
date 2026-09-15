---
name: workflow-security-devsecops
description: "User asks for security audit, pen test, vulnerability scan, DevSecOps review, or exploit validation. Runs the kit workflow 'security-devsecops': DevSecOps audit — recon, SAST, dependency scan, secret detection, DAST, exploit validation, report."
argument-hint: "[goal]"
---

<!-- Generated from workflows/security-devsecops.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: security-devsecops

DevSecOps audit — recon, SAST, dependency scan, secret detection, DAST, exploit validation, report.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/security-devsecops.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `recon` | `security-auditor` | — |
| `sast` | `security-auditor` | — |
| `dependency-scan` | `security-auditor` | — |
| `secret-detection` | `security-auditor` | — |
| `dast-probe` | `security-auditor` | — |
| `exploit-validation` | `security-auditor` | — |
| `report` | `verifier` | — |
| `document` | `doc-writer` | — |
