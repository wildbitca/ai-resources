---
name: workflow-openclaw-host-setup
description: "User wants to set up, rebuild, restore or re-align an OpenClaw gateway host, onboard a new agent workspace on it, or verify that its documented setup is in place. Runs the kit workflow 'openclaw-host-setup': Set up or rebuild an OpenClaw gateway host from the kit. ai-resources setup first, thin openclaw subcommands as the fallback, then verify."
argument-hint: "[goal]"
disable-model-invocation: true
---

<!-- Generated from workflows/openclaw-host-setup.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: openclaw-host-setup

Set up or rebuild an OpenClaw gateway host from the kit. ai-resources setup first, thin openclaw subcommands as the fallback, then verify.

**Goal:** $ARGUMENTS

**Score first.** Before step 1 score the task with the `kit-orchestration` "Scorecard" (`ai-resources scorecard '<json>'`): the card picks LITE, STANDARD or FULL and the roster, so this workflow's roles run only when the card names them (LITE works inline, no handoff); the skill holds the fallbacks and the tests rule.

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/openclaw-host-setup.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Backend | Parallel group |
|------|-------|---------|----------------|
| `preflight` | `generalPurpose` | route | — |
| `setup` | `generalPurpose` | route | — |
| `agents` | `generalPurpose` | route | — |
| `verify` | `verifier` | route | — |
