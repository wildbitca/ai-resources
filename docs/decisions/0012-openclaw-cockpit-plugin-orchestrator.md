# ADR-0012: OpenClaw Native Cockpit & Lifecycle Orchestrator Plugin

**Status**: Proposed (2026-10-20)
**Date**: 2026-10-20
**Decision makers**: operator, implementer.
**Target Release**: v2.3.0
**Related**:
- [ADR-0001](0001-model-pins-and-auto-update.md) (single-writer model)
- [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (safe configuration without live restarts)
- [ADR-0005](0005-cross-provider-role-dispatch.md) (cross-provider role dispatch)
- [ADR-0006](0006-scored-team-composition.md) (scored team composition)
- [ADR-0007](0007-subscription-lanes.md) (subscription lanes and rendered `lanes.yaml`)
- [ADR-0010](0010-setup-five-phases-channels-team.md) (setup phases and cockpit integration)

---

## Context

The operator uses **OpenClaw** as the primary cockpit and day-to-day interface (via Telegram, Discord, and Web UI), while **ai-resources** provides 144 skills, declarative workflow definitions (`WORKFLOW_CONTRACT.md`), model scoring, and subscription lane management between Claude Code and Gemini (via Antigravity CLI).

Currently, this architecture has a split operational model:
1. **Prompt-driven team orchestration**: Handoffs between roles (`planner` $\to$ `implementer` $\to$ `verifier`) rely on the orchestrator LLM parsing instructions in `AGENTS.md` and writing `handoff.md`. If the LLM drifts or skips a step, the team lifecycle breaks.
2. **Terminal-bound monitoring and setup**: Operators must switch to a Bash shell to run `ai-resources lanes`, `ai-resources doctor`, or inspect workflow states.
3. **Minimal OpenClaw plugin**: The existing plugin (`openclaw-plugin/ai-resources`) only registers CLI backends (`agy-cli`, `agy-cli-lane`, `claude-kit`) and simple shortcuts (`/claude`, `/equipo`), but does not act as an orchestrator or expose live telemetry.

The operator wishes to make OpenClaw the **definitive cockpit** with a full native orchestrator and team dispatch, while preserving `ai-resources setup` as the unified configuration and audit entry point.

---

## Decision

### 1. Hexagonal Architecture: Core Engine vs Cockpit Driver

We maintain a strict separation of concerns:
- **Core Engine (Python / Declarative)**: The existing Python engine remains the authority for offline analysis, interactive setup wizard, credential validation, model scoring, and generating `~/.config/ai-resources/lanes.yaml`. Workflows (`.workflow.yaml`) and skills (`SKILL.md`) remain declarative, portable files.
- **Cockpit & Lifecycle Driver (OpenClaw Plugin in Node.js)**: The plugin in `openclaw-plugin/ai-resources` is expanded from a passive CLI backend registrar to an active **lifecycle orchestrator and cockpit surface**.
- **Configuration Contract**: `ai-resources setup` continues to manage `openclaw.json`, configuring the plugin via `plugins.entries["ai-resources"].config`.

### 2. Plugin Configuration Schema Extension

The plugin manifest (`openclaw.plugin.json`) is updated with optional configuration paths:

```json
{
  "id": "ai-resources",
  "name": "ai-resources",
  "description": "ai-resources kit: CLI backends, native team orchestrator, and cockpit commands.",
  "minGatewayVersion": "2026.9.4",
  "cliBackends": ["agy-cli", "agy-cli-lane", "claude-kit"],
  "setup": { "cliBackends": ["agy-cli", "agy-cli-lane", "claude-kit"], "requiresRuntime": false },
  "activation": { "onStartup": true },
  "configSchema": {
    "type": "object",
    "properties": {
      "agyBin": { "type": "string", "description": "Absolute path to the agy binary." },
      "claudeBin": { "type": "string", "description": "Absolute path to the claude binary." },
      "python3Bin": { "type": "string", "description": "Absolute path to python3." },
      "serializeAgy": { "type": "boolean", "description": "Whether to serialize agy-cli runs globally." },
      "kitRoot": { "type": "string", "description": "Absolute path to ai-resources libexec root." },
      "lanesConfigPath": { "type": "string", "description": "Path to rendered lanes.yaml." },
      "enableOrchestrator": { "type": "boolean", "description": "Enable native lifecycle team orchestration." }
    },
    "additionalProperties": false
  }
}
```

### 3. Cockpit Slash Commands

The plugin registers native commands via `api.registerCommand`:

1. **`/lanes [status]`**:
   - Reads `lanes.yaml` and live `/usage` caches.
   - Formats a compact summary in Markdown of current quotas (`claude-subscription`, `agy-gemini`), active primary assignments, and fallback routes.
   - Exposes quick health/headroom state directly inside Telegram/Discord chats.

2. **`/workflow list`**:
   - Discovers available YAML workflows in `${kitRoot}/workflows/`.
   - Returns a formatted list with descriptions, roles involved, and trigger domains.

3. **`/workflow run <id> [goal]`**:
   - Parses the target YAML workflow.
   - Validates required roles against `lanes.yaml`.
   - Launches Phase 0/Step 1 via the runtime subagent API.

4. **`/equipo <goal>`**:
   - Upgraded from simple single-agent spawn to initiate the full kit lifecycle (`planner` $\to$ `implementer` $\to$ `verifier`).

### 4. Deterministic Lifecycle & Handoff State Machine

To eliminate prompt-driven failures:
1. **Sequential Phase Control**:
   - The plugin tracks the active team phase for a session (`PLANNING`, `IMPLEMENTING`, `VERIFYING`, `COMPLETED`, `BLOCKED`).
   - Upon subagent completion, the plugin intercepts the result:
     - When `planner` completes with an approved plan $\to$ auto-spawns `implementer` with the plan attached.
     - When `implementer` completes changes $\to$ auto-spawns `verifier` to run test suites and check acceptance criteria.
2. **Immutable Verification Barrier**:
   - An `implementer` subagent is strictly forbidden from certifying its own work.
   - Completion status `READY_FOR_MERGE` is accepted by the plugin **only** when emitted by a designated `verifier` session.
3. **Structured Handoff**:
   - Handoff metadata is persisted in `.agent-output/handoff.md` and echoed into the session context to maintain transparency.

### 5. Gateway Safety and Long-Running Execution

- **Non-blocking Dispatch**: In compliance with workspace rules and ADR-0003/T39, all subagent executions use OpenClaw's background runner (`api.runtime.subagent.run({ deliver: true })`).
- **No Synchronous Gateway Stalls**: The plugin command handlers immediately return an acknowledgement to the user (`continueAgent: false`), preventing gateway timeouts while subagents run in the background.

### 6. Backward Compatibility & Zero-Disruption Rollout

- **Additive only**: If `kitRoot` or `lanesConfigPath` are not configured, the plugin falls back gracefully to its existing v2.2.2 behavior (registering CLI backends and basic `/claude` command).
- **Setup as Single Entry Point**: Operators do not need to edit `openclaw.json` by hand. Running `ai-resources setup` automatically detects the paths and writes the expanded plugin configuration.

---

## Consequences

### Positive
- **Single Pane of Glass**: Operators monitor lanes, cuotas, and run multi-agent workflows directly from Telegram, Discord, or Web UI.
- **Deterministic Workflows**: Relevos and sign-offs are enforced in code rather than relying purely on LLM obedience.
- **No Code Re-write**: Preserves the extensive Python testing suite, ADRs 1–11, and 144 skills.

### Negative / Trade-offs
- The OpenClaw plugin gains state tracking logic in JavaScript (`index.js`).
- Session state must be cleanly managed across gateway reloads.
