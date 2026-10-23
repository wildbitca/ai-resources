# ADR-0015: Per-cockpit setup, OpenClaw providers, native team orchestrator (round 3)

**Status**: Proposed (2026-10-23), implemented on `feat/per-cockpit-setup-orchestrator`, not released. Plan:
`.agent-output/planner/per-cockpit-setup-openclaw-orchestrator-plan.md` (section 10 carries the operator answers).
**Date**: 2026-10-23
**Decision makers**: operator (plan section 10, binding), implementer (record only).
**Supersedes**: the single global "how should they work together" question of [ADR-0009](0009-setup-tools-how-team.md) (the
`mode` step of the wizard). The saved fields it wrote stay readable.
**Does not amend**: [ADR-0001](0001-model-pins-and-auto-update.md) (fill-only), [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md)
(no restart, no gateway call, every key classified at run time and fail closed), [ADR-0007](0007-subscription-lanes.md)
(gates G0-G7, `ACK_TEXT` and `agy_gate_evidence.json` are untouched), [ADR-0013](0013-token-cost-lean-defaults.md),
[ADR-0014](0014-token-cost-round-2.md).

## Context

Setup asked one global question ("as a team / each on its own / advanced gateway") and derived every cockpit's auth from it.
OpenClaw picked one engine, not the providers the operator actually has. Roles were rendered only for Claude Code, skills were
scoped only for Claude Code, OpenClaw subagent limits were left at platform defaults, and the audit could not say which role or
provider spent what. OpenClaw also had no native way to run a kit workflow as a team.

## Decision

### D1. The global question is gone; every cockpit asks its own auth

The selectable cockpits are Claude Code, Antigravity (`agy`) and OpenClaw. Cursor keeps its current behaviour (a one-line
"managed in Cursor" note, no question). Gemini CLI stays retired (the wizard keeps `HIDDEN_TOOLS = ("gemini",)`); `gemini.py`
survives only for the paths agy needs (`GEMINI.md`, `config/mcp_config.json`).

| Cockpit | Own question | Methods (names only are stored) |
|---|---|---|
| Claude Code | how does it authenticate | `subscription`, `api_key`, `vertex_adc` |
| OpenClaw | which providers, and per provider how | multiselect; per provider `api_key` or `cli` |
| agy | the Google terms acknowledgement of ADR-0007 | `google_account` |
| Cursor | none | `managed` |

The gateway (LiteLLM, OpenRouter) is not offered for any cockpit. Gateway installs on other hosts are left alone: there is no
teardown and no migration that deletes them. A saved `advanced` answer migrates to the cockpit's native auth and the old -> new
mapping is shown once. `--backend litellm|openrouter` and `--backend cli` still parse, print a deprecation note and pre-fill
subscription auth; `--cockpits` pre-fills the selection.

State is additive: a `cockpit_auth` field (`{cockpit: {method, providers{provider: method}}}`), no `SCHEMA_VERSION` bump,
so an older kit still reads the file. `mode`, `backend`, `cli_team` and `cli_team_api` stay readable and are derived from the
cockpit answers; they are no longer the drivers. Migration is fill-only (never overwrites a non-empty entry) and the
old -> new table is shown once, guarded by a marker in `tracking`.

| Saved legacy answer | Claude Code | OpenClaw | agy |
|---|---|---|---|
| `cli_team_api` | `api_key` (`vertex_adc` when a Vertex project is configured) | providers with `api_key` | `google_account` |
| `cli_team`, `alone` | `subscription` | providers with `cli` | `google_account` |
| `advanced` (litellm, openrouter) | `subscription` (gateway no longer offered) | `api_key` | `google_account` |

Secrets are never stored in kit state: only method names and provider ids.

### D2. OpenClaw providers come from OpenClaw

`openclaw models list --all --json` is the catalog; `models auth list --json` says which providers are authenticated. Parsing is
tolerant (unknown shape -> docs fallback -> the eight kit-known providers). Non-chat providers (speech, image, video) are
dropped. Each selected provider is asked API key (`openclaw models auth paste-api-key --provider <p>` on the operator's
terminal, OpenClaw's own store) or a CLI backend where one exists (anthropic -> `claude-cli`, openai -> `codex`, google ->
`agy-cli`). Routing, fallbacks, role agents and the notification route use enabled providers only; a role whose tier has no
enabled provider falls back to the cheapest enabled one and is listed in the review.

### D2b. agy lanes inside OpenClaw (operator answer 3)

OpenClaw may run kit roles on `agy-cli-lane/*`, behind the ADR-0007 Google terms acknowledgement and checker independence; no
agy lane is written without a valid acknowledgement. A `kit-lane-agy-*` agent that points at `anthropic/` or `claude-` is
drift. It is reported by default; repairing it is a confirmed migration (point it back at `agy-cli-lane/*` when acknowledged,
else offer removal), restricted to the kit-owned `kit-lane-agy-` prefix, never silent.

### D3. Native team orchestrator

A pure, resumable Python state machine (`ai-resources orchestrate ... --json`) plus a thin OpenClaw plugin. The engine never
dispatches and never sleeps: `next` returns the actions to perform (dispatch role R with prompt P in worktree W with timeout T),
the plugin dispatches through `api.runtime.subagent.run`, and reports each completion back with
`orchestrate report`. State lives under `~/.local/state/ai-resources/runs/<id>/{state.json,handoff.md}`, written atomically.

* Steps, roles, personas by domain, step skills, `handoff.md`, parallel groups (read-only roles only, joined before the next step).
* `Blocked` with `Return_to_step`: at most 3 returns, the 4th marks the run `failed`.
* Verifier independence: `READY` is accepted only from the verifier role; the verifier is a distinct agent id
  (`kit-role-verifier`) and, where the runtime exposes a session id, a distinct session. Where it does not, independence is
  enforced by agent id plus model family (ADR-0007 section 8) and the limitation is recorded in the run state.
* Plan approval pauses the run until `approve`.
* Per-role timeouts (planner 1200 s, implementer 9000 s, tester 2700 s, verifier 1800 s, default 1800 s) are plugin timers that
  call `report --timeout`, recorded as a `Blocked` outcome.
* `maxTeams` (default 2) with a FIFO queue; every team gets its own worktree and branch. One writer per run; teams are parallel
  only across worktrees.
* No spend cap (operator answer 4): provider limits are the brake. Cost is reported per run from the OpenClaw transcript events
  (USD API-equivalent, an estimate, ADR-0013).

Installing a new plugin version on a running gateway is a restart-class change: setup records exactly one `host_restart_pending`
entry and prints the `apply-pending` command; it never restarts (ADR-0003).

### D4. One plan, all at once

The orchestrator is not deferred.

## Items (a)-(f)

* (a) One shared role renderer (`setup/role_render.py`) feeds Claude subagents (byte-identical to before), OpenClaw `kit-role-*`
  agents and the agy `GEMINI.md` role block.
* (b) Effort: the profile key stays `role_effort` (the profile tests forbid an `effort` key). `setup/effort.py` translates per
  cockpit: Claude subagent `effort:` frontmatter, OpenClaw `thinkingDefault`.
* (c) Skill scoping reaches every cockpit through a kit-managed scoped symlink directory; OpenClaw gets `skills.load.extraDirs`.
* (d) OpenClaw subagent limits, fill-only (see "Limits rationale").
* (e) Audit attributes spend by origin, provider and role; `docs/validating-cost.md` explains how to check the numbers.
* (f) `docs/cockpit-recommendations.md` says which wizard answers give a performant and low-cost setup.

## Verified facts (read-only spikes, 2026-10-23, OpenClaw 2026.10.5, Claude Code 2.1.309)

| Question | Command / source | Result | Decision |
|---|---|---|---|
| Does Claude Code honour `effort:` in subagent frontmatter? | the 2.1.309 binary: subagent frontmatter key list `["name","description","tools","disallowedTools","model","effort","permissionMode","mcpServers",...]` and schema text "Thinking effort: `low`, `medium`, `high`, `max`, or an integer." | accepted (static evidence; no `claude -p` run, which would write the live `~/.claude`) | capability flag `effort_frontmatter = yes`; emitted fill-only |
| Provider catalog shape | `openclaw models list --all --json` | `{count, models[{key "provider/model", name, input, contextWindow, local, available, tags}], providerOutcomes[{provider, profileId?, status}]}`; providers seen: anthropic, google, google-vertex, claude-cli, agy-cli-lane | tolerant parser; fixture `tests/fixtures/openclaw/providers_models_list.json` (synthetic, secret-free) |
| Authenticated providers | `openclaw models auth list --json` | `{agentId, profiles[{id "<provider>:<name>", provider, type, label}]}` | provider ids only |
| API key entry | `openclaw models auth paste-api-key --provider <p> [--profile-id] [--agent]` | prompts on the operator's TTY | kit never sees the key |
| Subagent limits keys | `openclaw config schema` | `agents.defaults.subagents.{maxConcurrent,maxChildrenPerAgent,maxSpawnDepth,runTimeoutSeconds}` exist | classified at run time by `openclaw_reload_rules` (prefix `agents` = none) |
| Per-agent thinking | `openclaw config schema` | `agents.entries.<id>.thinkingDefault` and `agents.defaults.thinkingDefault` exist | role agents carry the mapped level |
| Skill dirs | `openclaw config schema` | `skills.load.extraDirs`, `skills.load.allowSymlinkTargets`, `skills.load.watch` exist | prefix `skills` = none (no restart) |

## Limits rationale

Defaults written only when the key is unset: `maxConcurrent 3`, `maxChildrenPerAgent 3`, `maxSpawnDepth 2`,
`runTimeoutSeconds 7200`. Each concurrent subagent can hold MCP child processes; `maxTeams 2 x maxConcurrent 3` keeps the
worst case under the gateway `MemoryHigh` of 12G and under the Anthropic rate limits seen in the audit. The values are
conservative and fill-only: an operator value is kept and listed.

## Assumed answers

None remain: plan section 10 answered every open question (gateway removed, no Gemini CLI cockpit, full agy lanes in OpenClaw
behind the acknowledgement, no spend cap, orchestrator not deferred).

## Non-goals

Parity with `/kit-plan` three-plans-and-judges; opening agy gates G0-G7 or touching `ACK_TEXT` / `agy_gate_evidence.json`;
parallel writers in one run; `--strict-mcp-config`; touching `CLAUDE.md` outside the kit block; `channels.*` beyond the existing
disable-only path; secrets in kit state.

## Consequences

Setup asks fewer, better-aimed questions; OpenClaw can run a workflow as a team without a Claude Code parent; the audit can show
where tokens go per role and provider. The cost is a larger wizard test rewrite and a new engine to maintain.
