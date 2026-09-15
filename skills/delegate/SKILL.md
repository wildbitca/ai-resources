---
name: delegate
description: "Switch this session to orchestrator-only mode: route work through workflow-* skills and subagents and keep main-thread replies short. Use when the user says /delegate, 'orchestrator mode' or 'delegate everything'."
disable-model-invocation: true
---

# /delegate — orchestrator-only mode

This mode stays on until the user asks for direct work (for example "direct mode" or "do it yourself").

## Behavior

1. For each request, run the matching `workflow-*` skill following `kit-orchestration`. For scoped work without a workflow, send one subagent with a complete prompt (see "Running a step" in `kit-orchestration`).
2. Do not implement application code or run broad multi-file exploration in the main thread — put that work in the subagent's prompt.
3. After each subagent returns, read the handoff file or its return block and reply with status and next step only. Point to paths instead of pasting code or logs.

## Exception

Trivial requests — a one-line change in one file, or a question that needs no repository changes — can be handled directly.
