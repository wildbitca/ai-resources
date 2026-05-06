# Multi-Model Routing — User Guide

`ai-resources` v1.0 introduces multi-model routing: each subagent role can run
on a different LLM (Claude, Gemini, GPT, local Ollama, …) routed through a
local or remote LiteLLM gateway. This guide explains how to set it up and
operate it.

## Quick start

```sh
ai-resources setup       # interactive wizard
ai-resources doctor      # verify everything works
```

The wizard handles cockpit detection (Claude Code, Cursor, Gemini CLI, …),
LiteLLM container install, provider credentials, profile selection, and
per-cockpit configuration.

## Mental model

```
┌─────────────────────────────────────────────────┐
│  Cockpit (Claude Code / Cursor / Gemini CLI)    │
│  ANTHROPIC_BASE_URL=http://127.0.0.1:4000       │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│  LiteLLM gateway  (local container or remote)   │
│  reads {"model": "..."} field of each request   │
└──┬──────────────────┬──────────────────┬────────┘
   ▼                  ▼                  ▼
Anthropic          Google            Vertex/OpenAI/etc.
(claude-*)        (gemini-*)        (provider-translated)
```

The cockpit always talks to the gateway. The gateway routes to the actual
provider based on the `model` field. Tools, MCP, skills, and workspace access
are all preserved — only the LLM behind each subagent changes.

## Configuration files

| Path | Purpose |
|---|---|
| `~/.config/ai-resources/setup-state.yaml` | Wizard answers (re-runs use this as defaults) |
| `~/.config/ai-resources/executors.yaml` | **Single source of truth** for role → model mapping |
| `~/.config/ai-resources/litellm.yaml` | Generated from executors.yaml — gateway routing |
| `~/.config/ai-resources/bin/litellm-run` | Wrapper script (sources .env, execs litellm) |
| `~/.config/ai-resources/.env` | Credentials (chmod 600, never commit) |
| `~/.config/ai-resources/logs/` | Service logs (macOS only — Linux uses journald) |
| `~/Library/LaunchAgents/com.ai-resources.litellm.plist` (macOS) | Auto-start LaunchAgent |
| `~/.config/systemd/user/ai-resources-litellm.service` (Linux) | Auto-start systemd-user service |

## Profiles

Built-in:

| Profile | Description |
|---|---|
| `unified-default` | Mixed: research/explore/test → Gemini, code → Claude. **Recommended.** |
| `all-claude` | Every role on Claude. Highest reliability, highest cost. |
| `all-gemini` | Every role on Gemini 2.5. Cheapest. |
| `cost-optimized` | Aggressively bias toward Flash/Haiku. |
| `vertex-enterprise` | All traffic via Vertex AI for compliance. |

Custom: pick `custom` in the wizard to override each role individually.

## Editing the mapping

```sh
ai-resources executors show         # display current mapping
ai-resources executors edit         # open executors.yaml in $EDITOR
ai-resources executors test research # round-trip the research role's model
```

After editing `executors.yaml` directly, regenerate the gateway config and
restart:

```sh
ai-resources setup                  # re-runs wizard, picks up edits
# or just restart if only the gateway config changed:
ai-resources daemon restart
```

## Per-cockpit configuration

| Cockpit | File written | Notes |
|---|---|---|
| Claude Code | `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/agents/*.md` | Each subagent has `model:` from executors.yaml |
| Gemini CLI | `~/.gemini/settings.json`, `~/.gemini/GEMINI.md` | Engram MCP configured |
| Cursor | `~/.cursor/AGENT_KIT.md`, `~/.cursor/mcp.json` | |
| Codex CLI | `~/.codex/AGENTS.md` | |
| Aider | `~/.aider/CONVENTIONS.md`, `~/.aider.conf.yml` | architect/editor/weak models from executors |
| Windsurf | `~/.codeium/windsurf/memories/global_rules.md` | |
| Continue.dev | `~/.continue/AGENT_KIT.md` | |
| Copilot | `~/.vscode/copilot-instructions.md` | |
| OpenCode | `~/.config/opencode/AGENT_KIT.md` | |

## Operations

```sh
ai-resources daemon status     # is the gateway running?
ai-resources daemon start      # start container
ai-resources daemon stop       # stop container
ai-resources daemon restart    # restart
ai-resources daemon logs       # tail container logs
ai-resources daemon update     # pull latest LiteLLM image + restart

ai-resources doctor            # full health check
ai-resources audit             # cost report from gateway logs
```

## Troubleshooting

**Gateway won't start.**
```sh
ai-resources daemon logs --tail 200
docker ps -a | grep ai-resources-litellm
```

**Subagent fails with "model not found".**
- Check `~/.config/ai-resources/litellm.yaml` lists the model
- Ensure the provider's API key is in `.env`
- Run `ai-resources executors test <role>` to isolate

**Anthropic prompt caching seems off.**
- Caching is preserved when routing to Claude. Lost when routing to Gemini.
- Check log lines: `cache_read_input_tokens` should appear for Claude calls.

**Tool calls fail when subagent uses Gemini model.**
- Most tool calls translate cleanly. Edge cases:
  - Parallel tool calls — Gemini may serialize them
  - Complex JSON Schema with `$ref` — translation may flatten
- Workaround: assign that role to Claude in `executors.yaml`.

**Engram MCP not visible to a non-Claude subagent.**
- MCP tools are translated by LiteLLM. Ensure the cockpit has Engram in its
  MCP config. Verify with `ai-resources doctor`.

**I don't have GCP / want to skip Vertex.**
- Just don't enable Vertex in the wizard. `unified-default` profile works
  with Anthropic + Google AI Studio direct.

## See also

- `rules/017-multimodel-routing.mdc` — kit-internal rule
- `docs/litellm-service.md` — gateway operations
- `https://docs.litellm.ai/` — upstream LiteLLM docs
