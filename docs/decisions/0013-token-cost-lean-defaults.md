# ADR-0013: Token-cost lean defaults (and a measured before/after)

**Status**: Accepted (2026-10-22), implemented, not released. Plan: `.agent-output/planner/token-cost-lean-defaults-plan.md`
(sections 9 and 10 carry the operator decisions). Source analysis: the 2026-10-22 cost analysis (`REPORT.md` in the
operator's cache, outside the repo).
**Date**: 2026-10-22
**Decision makers**: operator (Q1 and Q7 answered, the other questions took their defaults), implementer (record only).
**Amends**: [ADR-0001](0001-model-pins-and-auto-update.md) (the price table gains sourced 5.5 and Gemini 4 rows) and,
for exactly one narrow writer, ADR-0001 and [ADR-0010](0010-setup-five-phases-channels-team.md) on `channels.*` (Q1).
**Does not amend**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (every new key is hot or none),
[ADR-0007](0007-subscription-lanes.md) (agy lanes stay closed, gates G0-G7 untouched, `--strict-mcp-config` stays forbidden),
[ADR-0011](0011-setup-writes-scoped-tools-deny.md) (`tools.profile` and `tools.deny` are never written by the profile).
The plan named this record "ADR-0012"; that number was already taken by the OpenClaw cockpit ADR, so it is 0013.

## Context

The analysis priced 30 days of real use at about $6.4k API-equivalent (subscription use is not billed per token). The drivers
were not the model choice: a 161-skill listing in every session prefix, 39.6% of calls above a 200k prompt, long-lived sessions
re-reading their whole history on every poll turn, Opus fallbacks behind Sonnet primaries, and tool-schema fan-out. The audit
itself over-counted (streamed messages were counted several times, first record winning) and missed subagent transcripts and
the whole OpenClaw native runtime, so no change could be proven.

## Decision

`ai-resources setup` always produces the lean configuration, idempotently, fill-only where an operator value exists, with
nothing restart-required applied unattended, and `ai-resources audit snapshot|compare` measures it.

1. **Audit.** Dedupe by `message.id` (fallback `requestId`), last record wins (streamed records are cumulative);
   subagent transcripts (`<session>/subagents/**`) and `--all-projects`; OpenClaw per-agent `transcript_events` (read-only,
   zstd decoder chain with a visible "undecoded rows" count); prices for `claude-opus-5-5`, `claude-sonnet-5-5`,
   `claude-haiku-5-5` (with a >100k prompt tier), `claude-sonnet-5` read price corrected to 0.20, `claude-fable-5-1`,
   `gemini-4-argon` and `gemini-3.8-flash` (date-bounded rows with an `until` date). Source: the official pricing pages
   verified 2026-10-22 (Q7), cited in `audit.py`. Explicit ids match before the prefix rule, so 5.5 is never priced as 5.
2. **Snapshot / compare (AC-20).** Same metric definitions as the operator's `bench.py`; the `before-*.json` files are
   readable by `compare`. **Method skew:** `bench.py` kept the FIRST record per (message id, requestId); the kit keeps the LAST
   record per message id (streamed records carry cumulative usage, so the last is the complete one). The kit's method is
   kept because it is the more correct one, and every snapshot records it (`method`). Counts therefore differ, and a
   `bench.py` baseline is not a fair "before": re-take it with the kit over the same window
   (`ai-resources audit snapshot before --since <ISO> --until <ISO>`; the loaders honour both bounds, `until` exclusive).
   `compare` prints a warning when either side lacks the kit method. The config inventory in a re-taken snapshot is the config
   at the time it is taken, not at the window. A snapshot also records the config inventory of the keys this ADR changes. `--probe` is opt-in and
   prints its cost first; tests never run it.
3. **Claude Code.** `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` (fill-only; verified in the 2.1.308 binary); the kit-written
   `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is removed once (value `1` only; no kit workflow uses agent teams, rules/014 only
   cites the upstream agent-teams-lite docs); only the skills in `skill_scope.py` are linked natively
   (`AI_RESOURCES_SKILLS=all` restores every one, `flutter,terraform` widens by prefix); `workflow-*` skills and four
   step-loaded skills carry `disable-model-invocation: true`; role subagents preload `skills:` (the skills every step of the
   role names). Claude Code 2.1.308 resolves a frontmatter skill through the linked set and skips a missing one ("Skill
   '<id>' specified in frontmatter was not found"), so every `ROLE_SKILLS` id is linked too, and the `common-*` ones carry
   `disable-model-invocation: true` so they stay out of the main listing. The preload path in the 2.1.308 binary checks
   existence, prompt type and policy, not that flag (read from the binary; not exercised with a live subagent call).
   `validate_kit.py` fails a `ROLE_SKILLS` id that is not linked, a preloaded skill outside `INCLUDE_IDS` without the flag,
   and a workflow-step skill that is not linked, not preloaded for the step's role and not listed in
   `skill_scope.LOADABLE_BY_PATH` (read by path); the kit CLAUDE.md block stays small and holds the polling guidance once.
4. **engram.** The external plugin's first-message "CRITICAL FIRST ACTION ... ToolSearch" directive is removed by a
   hash-guarded, backed-up, best-effort edit inside the plugin directory (core `mem_*` tools need no ToolSearch). A plugin
   update can restore it; the next setup re-applies. The SessionStart payload comes from the server and the plugin script has no
   size knob, so it is not kit-controllable.
5. **OpenClaw.** The host profile gains the lean keys (see the runbook). Values a host already sets are changed only by
   the operator-confirmed `openclaw_cost_migrations` (per path, recorded by id). The MCP mirror is opt-in for new installs; in
   opt-in mode a mirrored server that is no longer listed is denied per non-lane agent by exact token (`<server>__*`),
   fill-only, recorded in state, never through the global `tools.deny` (ADR-0011); `engram` is engine-managed and never
   denied. agy fallback chains use `gemini-3.8-flash-low`.
6. **Q1 (operator): a narrow Slack writer.** `openclaw_slack_cost.py` may set exactly
   `channels.slack.implicitMentions.threadParticipation=false` and `channels.slack.channels.<id>.allowBots="mentions"` for
   channels the operator picks. Interactive confirm per path, fill-only, recorded in state, deleted on teardown,
   guarded by `assert_slack_cost_safe` (no allowlist, no replace-path, fixed values). `assert_channels_safe` is unchanged and
   still refuses everything else on `channels`, including Slack, for every host patch.

7. **claude-kit (Q3, superseded 2026-10-22).** The first decision added `--disable-slash-commands` to the `claude-kit`
   backend. The operator reversed it: the flag also strips `/kit-plan`, `/kit-implement` and every skill from the Claude workers
   OpenClaw spawns. The backend argv is as before; the saving comes from skill scoping instead. `--strict-mcp-config` stays
   forbidden (ADR-0007 section 9).
8. **Pin bumps (accepted 2026-10-22).** With the sourced 5.5 rows, the automatic pin bump of sonnet 5 -> 5.5 (equal price)
   and opus 5 -> 5.5 (lower price) is accepted; `tests/test_models_policy.py` keeps asserting it, and haiku still waits for
   approval (major jump).

Declined or left at their defaults: agy lanes closed with a drift report only (Q2); claude-kit without `--strict-mcp-config` (Q3, see 7);
no `tools.profile` write (Q4); confirmed force migration for this host (Q5); hash-guarded engram patch (Q6); remove the
AGENT_TEAMS key only because no kit workflow uses it (Q8); repo `AGENTS.md` is authoritative over the workspace copy that promises
an automatic agy fallback (Q9); main heartbeat isolated and light, `every` kept (Q10).

## Facts (verified 2026-10-22 on OpenClaw 2026.10.5, Claude Code 2.1.308, read-only)

| Key | In schema | Reload class | Source |
|---|---|---|---|
| `session.reset.{mode,idleMinutes}`, `session.resetByType.{group,thread}` | yes | none | `openclaw config schema`; reload table |
| `agents.defaults.contextPruning.mode` (`off` or `cache-ttl`) | yes | hot | schema |
| `agents.defaults.models.<ref>.params.anthropicServerCompaction` / `anthropicCompactThreshold` | `params` is free-form | hot | OpenClaw `docs/providers/anthropic.md` (documented; a threshold below 50000 is clamped) |
| `agents.defaults.subagents.{model,maxConcurrent,maxChildrenPerAgent}` | yes | hot | schema |
| `agents.defaults.utilityModel`, `agents.defaults.thinkingDefault` | yes | hot | schema |
| `agents.entries.<id>.heartbeat.{isolatedSession,lightContext}` | yes | hot | schema |
| `channels.slack.implicitMentions.threadParticipation`, `channels.slack.channels.<id>.allowBots` | yes | hot | schema |
| `plugins.entries.workboard.enabled` | yes | hot | schema (printed only) |

`openclaw config patch --dry-run --json` of the generated patch against a COPY of the live config
(`OPENCLAW_CONFIG_PATH` and `OPENCLAW_STATE_DIR` pointing at a scratch directory) answered `ok: true, operations: 18`.
Nothing was applied to the live file and the gateway was never restarted.

Dropped because not proven: `contextTokens` per model, and any `gemini-4-argon-low` id (`openclaw models list` shows
`agy-cli-lane/gemini-4-argon-high` only; the pins list carries `-low`, which this host has not confirmed). The workspace
`AGENTS.md` copy under the development root was left as it is: only the repo copy is the kit's artifact.

## Consequences

- A host that already sets thinking high, an Opus fallback or a mirrored MCP server keeps them until the operator confirms
  (or sets `OPENCLAW_MCP_SERVERS`); the lean values reach it through the confirmed migration and the printed snippets.
- Native skill discovery is smaller on purpose: other kit skills are read by path (the CLAUDE.md block says so) and
  `workflow-*` are invoked as `/workflow-<name>` or through `/kit-plan` and `/kit-implement`.
- Cost claims are API-equivalent estimates. No saving is claimed without an `audit compare` between two snapshots.
- Rollback: each lever is independent. `AI_RESOURCES_SKILLS=all`; remove the migration ids from the state; restore the
  engram script from `user-prompt-submit.sh.ai-resources-backup`; teardown deletes the Slack keys the kit set.
