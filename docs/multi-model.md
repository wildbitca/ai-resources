# Multi-Model Routing — User Guide

Multi-model routing lets each kit subagent run on a different LLM. Two backends
serve it, and the wizard asks which one you want:

| Backend | What it is | You install | Credential |
|---|---|---|---|
| **OpenRouter** | A hosted gateway reaching 400+ models | nothing | one key |
| **LiteLLM** | A gateway this kit installs and supervises on your machine | a local service | one key per provider |

Neither is a default in disguise. OpenRouter is the shorter path; LiteLLM is the
one that reaches local models through Ollama and enforces per-key budgets
itself.

> **A gateway replaces your claude.ai subscription for Claude Code.** While a
> gateway credential is active the subscription is not used and every token is
> billed to the gateway. That applies to both backends, and the wizard says so
> before you choose.

## Quick start

```sh
ai-resources setup       # interactive wizard — step 1 picks the backend
ai-resources doctor      # verify everything works
```

The wizard handles cockpit detection (Claude Code, Cursor, Gemini CLI, …),
gateway setup where there is one, credentials, profile selection, and
per-cockpit configuration.

## Mental model

The cockpit always talks to a gateway; the gateway routes on the `model` field
of each request. Tools, MCP, skills and workspace access are preserved — only
the LLM behind each subagent changes.

```
┌──────────────────────────────────────────────────┐
│  Cockpit (Claude Code / Cursor / Gemini CLI …)   │
│  ANTHROPIC_BASE_URL=<gateway>                    │
└───────────────────────┬──────────────────────────┘
                        │
        ┌───────────────┴────────────────┐
        ▼                                ▼
┌───────────────────────┐   ┌────────────────────────────┐
│ OpenRouter (hosted)   │   │ LiteLLM (yours, local)      │
│ openrouter.ai/api     │   │ 127.0.0.1:4000              │
│ namespaced model IDs  │   │ bare model IDs              │
│ anthropic/claude-…    │   │ claude-…  gemini-…          │
└───────────┬───────────┘   └─────────────┬──────────────┘
            ▼                             ▼
    every provider              Anthropic · Google · Vertex
    behind one key              OpenAI · local Ollama
```

The two are not interchangeable. OpenRouter needs namespaced IDs
(`google/gemini-3.7-flash`); LiteLLM needs bare ones (`gemini-3.7-flash`). That
is why profiles declare a `backend:` and the wizard only offers the matching
set.

### What carries the model

Three things resolve a model, in order of precedence:

1. **A subagent's `model:`** — generated into `~/.claude/agents/<name>.md` from
   the profile. Claude Code forwards this value to the gateway verbatim, so any
   ID the gateway understands works here.
2. **`classes`** — pins `/model` aliases (opus, sonnet, haiku, fable) for the
   *main* conversation. Only the OpenRouter backend needs it: Claude Code
   resolves an alias to a bare Claude ID, which a namespaced catalogue does not
   recognise.
3. **The session model** — whatever `/model` is set to, for any subagent whose
   model is `inherit`.

## Roles and personas

Two kinds of subagent are generated, forty in total:

- **Roles** (`agents/roles/*.md`, 15) — planner, implementer, code-reviewer, …
  Each one takes its model from `by_role`.
- **Personas** (`agents/personas/<role>-<domain>.md`, 25) — a role plus a
  domain's conventions, such as `code-reviewer-angular`. The generated file is
  the role's body followed by the overlay, and it takes its model from
  `by_persona`, falling back to its base role.

`by_persona` is empty in every shipped profile, so personas inherit their role
until you say otherwise. Filling it in is how an infrastructure review and a
CSS review stop costing the same:

```yaml
by_persona:
  code-reviewer-devops:  { provider: openrouter, model: anthropic/claude-opus-5 }
  code-reviewer-angular: { provider: openrouter, model: anthropic/claude-sonnet-5 }
```

## Configuration files

| Path | Purpose |
|---|---|
| `~/.config/ai-resources/setup-state.yaml` | Wizard answers, including `mode` and `backend` |
| `~/.config/ai-resources/executors.yaml` | **Single source of truth** for the model mapping |
| `~/.config/ai-resources/.env` | Credentials (chmod 600, never commit) |
| `~/.config/ai-resources/litellm.yaml` | LiteLLM backend only — generated from executors.yaml |
| `~/.config/ai-resources/bin/litellm-run` | LiteLLM backend only — wrapper script |
| `~/.config/ai-resources/logs/` | LiteLLM backend only — service logs (macOS; Linux uses journald) |
| `~/Library/LaunchAgents/com.ai-resources.litellm.plist` (macOS) | LiteLLM backend only — auto-start |
| `~/.config/systemd/user/ai-resources-litellm.service` (Linux) | LiteLLM backend only — auto-start |

Under the OpenRouter backend the last five do not exist: there is no local
service, so `ai-resources daemon` has nothing to manage and says so.

## Profiles

| Profile | Backend | Description |
|---|---|---|
| `openrouter-balanced` | OpenRouter | Judgement on Claude, bulk reading on Gemini Flash. A good default. |
| `openrouter-cheap` | OpenRouter | Budget models throughout, except review and security. |
| `quality-first` | LiteLLM | Claude for code-critical roles, Gemini for high-volume support. |
| `all-claude` | LiteLLM | Every role on Claude. Highest reliability, highest cost. |
| `all-gemini` | LiteLLM | Every role on Gemini. Cheapest. |
| `cost-optimized` | LiteLLM | Aggressively biased toward Flash and Haiku. |
| `vertex-enterprise` | LiteLLM | All traffic via Google Cloud's Agent Platform, for compliance. |
| `claude-native` | — | Single-model: each role picks a Claude alias. No gateway. |
| `claude-inherit` | — | Single-model: every role inherits the session model. |

Custom: pick `custom` in the wizard to override each role individually.

## Editing the mapping

```sh
ai-resources executors show              # display current mapping
ai-resources executors edit              # open executors.yaml in $EDITOR
ai-resources executors set implementer   # reassign one role (or persona)
ai-resources executors test implementer  # round-trip that model
ai-resources executors apply quality-first
```

`set` and `test` accept a persona name as readily as a role, since a persona is
an executor in its own right.

After editing `executors.yaml` directly:

```sh
ai-resources setup            # re-runs the wizard, picks up edits
ai-resources daemon restart   # LiteLLM backend only, if just the gateway changed
```

## Per-cockpit configuration

| Cockpit | File written | Notes |
|---|---|---|
| Claude Code | `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/agents/*.md` | 40 subagents: 15 roles + 25 personas, each with its `model:` |
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
ai-resources doctor            # full health check, both backends
ai-resources audit             # cost report

# LiteLLM backend only — under OpenRouter these report that there is nothing to manage
ai-resources daemon status
ai-resources daemon start
ai-resources daemon stop
ai-resources daemon restart
ai-resources daemon logs
ai-resources daemon update
```

## Cost reporting is wrong under OpenRouter

Claude Code computes every figure it shows — `/usage`, the status line,
`--max-budget-usd` — locally, from token counts at **Anthropic list prices**.
Route through OpenRouter and those numbers describe a bill nobody is sending
you. A run costing cents can report dollars.

The spend limit on the OpenRouter key is the real ceiling, and
<https://openrouter.ai/activity> is the real breakdown. Set a limit per key at
<https://openrouter.ai/settings/keys>.

The LiteLLM backend has the same problem for non-Anthropic routes; there,
`ai-resources audit` reads the gateway's own logs instead.

## Troubleshooting

**Every request fails under OpenRouter, and Claude Code is logged in.**
OpenRouter rejects a request carrying both a bearer token and an OAuth login.
Run `claude /logout`, open a new terminal, then `ai-resources doctor`.

**`[claude-code:unrecognized_model]` on every start.**
Expected noise, not a failure. Claude Code does not recognise namespaced IDs
such as `anthropic/claude-sonnet-5`, warns, and sends them anyway.

**The main conversation fails but subagents work (OpenRouter).**
The profile has no `classes` block, so `/model` aliases resolve to bare Claude
IDs the catalogue does not know. Add one, or pick a shipped openrouter profile.

**A model ID is rejected.**
OpenRouter IDs are namespaced and exact: `anthropic/claude-haiku-4.5` exists,
`anthropic/claude-haiku-4-5` does not. Check the live catalogue rather than
guessing. On the LiteLLM backend, check that `litellm.yaml` lists the model and
the provider key is in `.env`. Either way, `ai-resources executors test <name>`
isolates it.

**Gateway won't start (LiteLLM).**
```sh
ai-resources daemon logs --tail 200
docker ps -a | grep ai-resources-litellm
```

**Nothing current works on Vertex.**
Regional endpoints such as `us-east5` serve Claude Sonnet 4.6 and earlier only.
Set `GOOGLE_CLOUD_LOCATION=global`, which is also the tier without the 10%
premium that multi-region and regional endpoints carry.

**Prompt caching seems off.**
Caching survives when routing to Claude and is lost when routing elsewhere, so a
cheaper model can cost more per completed task. Check that
`cache_read_input_tokens` appears on Claude calls.

**Tool calls fail on a non-Claude model.**
Most translate cleanly. Known edges: parallel tool calls may be serialised, and
complex JSON Schema with `$ref` may be flattened. Not every model supports tool
use at all — on OpenRouter, check `supported_parameters` before assigning one to
a role. Workaround: put that role back on Claude.

**Engram MCP not visible to a non-Claude subagent.**
Ensure the cockpit has Engram in its MCP config; verify with `ai-resources
doctor`.

## See also

- `rules/017-multimodel-routing.mdc` — kit-internal rule
- `docs/litellm-service.md` — gateway operations
- <https://openrouter.ai/docs> — OpenRouter docs
- <https://docs.litellm.ai/> — upstream LiteLLM docs
