---
name: workflow-openclaw-host-setup
description: "User wants to set up, rebuild, restore or re-align an OpenClaw gateway host, onboard a new agent workspace on it, or verify that its documented setup is in place. Runs the kit workflow 'openclaw-host-setup': Set up or rebuild an OpenClaw gateway host from the kit. ai-resources setup first, thin openclaw subcommands as the fallback, then verify."
argument-hint: "[goal]"
---

<!-- Generated from workflows/openclaw-host-setup.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: openclaw-host-setup

Set up or rebuild an OpenClaw gateway host from the kit. ai-resources setup first, thin openclaw subcommands as the fallback, then verify.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/openclaw-host-setup.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `preflight` | `generalPurpose` | — |
| `setup` | `generalPurpose` | — |
| `agents` | `generalPurpose` | — |
| `verify` | `verifier` | — |
