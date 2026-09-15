---
name: workflow-release-dart-flutter
description: "User asks to prepare a release or generate release notes before publishing to GitHub. Runs the kit workflow 'release-dart-flutter': Pre-publish hook. Diff since latest tag → humanized release notes (CHANGELOG + release_notes_*.txt). No agent commit/push; human integrates. Pipeline trusts these files."
argument-hint: "[goal]"
---

<!-- Generated from workflows/release-dart-flutter.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file. -->

# Workflow: release-dart-flutter

Pre-publish hook. Diff since latest tag → humanized release notes (CHANGELOG + release_notes_*.txt). No agent commit/push; human integrates. Pipeline trusts these files.

**Goal:** $ARGUMENTS

1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, parallel groups, and the subagent return format.
2. Read the full definition at `$AGENT_KIT/workflows/release-dart-flutter.workflow.yaml` (`kit-orchestration` explains how to locate `$AGENT_KIT`) and run its steps in order.

## Steps

| Step | Agent | Parallel group |
|------|-------|----------------|
| `gather` | `generalPurpose` | — |
| `generate` | `implementer` | — |
| `verify` | `verifier` | — |
| `document` | `doc-writer` | — |
