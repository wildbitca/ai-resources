# Engram (MCP) — summary for any agent

This project uses **Engram** MCP tools: `mem_save`, `mem_search`, `mem_get_observation`, `mem_context`, `mem_session_start`, `mem_session_summary`, etc.

## Two-step read (mandatory for large artifacts)

`mem_search` returns **truncated** previews. For full content:

1. `mem_search(query: "…", project: "…")` → note **observation id**
2. `mem_get_observation(id: …)` → **full** body

Skipping step 2 causes silent information loss on specs, SDD artifacts, and long decisions.

## Topic keys (SnoutZone + SDD-style)

| Use | `topic_key` pattern |
|-----|---------------------|
| Session / decisions | `decision/…`, `architecture/…` per `mem_suggest_topic_key` |
| SDD change (optional) | `sdd/{change-name}/proposal`, `…/spec`, `…/design`, `…/tasks`, `…/state` |
| Init context | `sdd-init/{project}` |

Full tables: `$AGENT_KIT/cursor/skills/_shared/engram-convention.md` (from Agent Teams Lite).

## Who reads / who writes

| Situation | Reader | Writer |
|-----------|--------|--------|
| General sub-task; orchestrator has context | Orchestrator may search, pass summary | Sub-agent **mem_save** for discoveries/fixes |
| SDD phase with dependencies | Sub-agent reads artifacts via search + get_observation | Sub-agent **mem_save** for its phase output |

## Persistence modes

Prefer **Engram** for cross-session state when MCP is configured. File-based product truth stays in **`specs/`** in git — Engram complements, not replaces `specs/PROJECT.md`.
