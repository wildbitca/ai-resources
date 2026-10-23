"""The native team orchestrator engine (ADR-0015 D3).

A pure, resumable state machine over a kit workflow. It NEVER dispatches, sleeps or polls: `next` returns the actions
a runtime must perform, `report` feeds a finished action back. The OpenClaw plugin is the thin runtime that dispatches
(`api.runtime.subagent.run`) and arms the per-role timers; every command is `ai-resources orchestrate <cmd> --json`.
"""
