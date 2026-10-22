# ADR-0014: Token-cost round 2 (balanced retune, hygiene, billing awareness, polling guard)

**Status**: Accepted (2026-10-22), implemented, not released. Plan: `.agent-output/planner/token-cost-round-2-plan.md`
(section 9 carries the operator decisions).
**Date**: 2026-10-22
**Decision makers**: operator (section 9 of the plan, answered with buttons), implementer (record only).
**Amends**: [ADR-0013](0013-token-cost-lean-defaults.md) section 6 (a second, separate OpenClaw config writer next to the
Slack one), and the AGENTS.md rule "content outside the kit block is never touched" for one narrow, confirmed case
(the global CLAUDE.md migration, D3.1).
**Does not amend**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (no restart, no gateway call, every key
classified at run time and fail closed), [ADR-0007](0007-subscription-lanes.md) (agy lanes stay closed, gates G0-G7
untouched, no `--strict-mcp-config`), [ADR-0010](0010-setup-five-phases-channels-team.md) (`PRESET_IDS` is unchanged, no
preset is added or renamed), [ADR-0011](0011-setup-writes-scoped-tools-deny.md) (no `tools.*` write).

## Context

Round 1 (ADR-0013) cut the fixed prefix and made the audit trustworthy. The remaining cost drivers, from the same analysis, are
sessions that poll with `sleep`, user-level MCP servers and plugins that are never called, persona subagents for domains the
operator does not use, a large global `~/.claude/CLAUDE.md`, a Haiku role set that can exceed the 100k price tier, and no
awareness of how Claude Code is billed (the host runs on prepaid credit with the weekly quota exhausted on 2026-10-22).

## Decision

### D1. `balanced` is retuned; there is no new `pro-lowcost` profile (operator decision 1)

The plan proposed a `pro-lowcost` profile. The operator rejected it: the existing single-model `balanced` preset becomes the
performant and low-cost mix. The profile name, the wizard entry and every saved selection are kept; nothing is renamed and
`PRESET_IDS` is unchanged. `low-cost`, `performance` and every other profile keep their teams.

| Tier | Roles |
|---|---|
| Haiku 5.5 | explore, tester, doc-writer, package-upgrade |
| Sonnet 5.5 | planner, software-architect, implementer, code-reviewer, generalPurpose and the four fixers/maintainers |
| Opus 5.5 | verifier, security-auditor (`fallbacks: []`) |

The independence rule of ADR-0007 section 8 holds: no writer role on Opus, no fallback edge from a checker to a writer's
family. The Opus fallbacks that sat behind the planner and the architect are dropped (round 1 named Opus fallbacks behind
Sonnet primaries as a cost driver). The Haiku roles must stay under the 100k Haiku price tier: `validate_kit.py` estimates each
Haiku role's static prompt (agent text, `ROLE_SKILLS`, the scoped skill bodies) at 4 characters per token and fails above
`HAIKU_PROMPT_BUDGET_TOKENS` (100000, with a recorded safety margin); the runtime counterpart is the audit's share of Haiku
calls above 100k.

For an installed host whose saved profile is `balanced`, the model change follows the ADR-0001 policy: nothing is replaced
silently. The wizard prints the old -> new role table the first time the new table differs from the one it last recorded
(`state.profile.roles_seen`, falling back to the pre-retune table), and records the new table. `balanced-claude-55` (the
multi-model recommendation) is unchanged, and so is `RECOMMENDED_PROFILE`.

### D2. Billing awareness (read-only)

`billing.py` reads `~/.claude.json` `oauthAccount.billingType` (`subscription`, `prepaid`, `unknown`; never an email, token
or account id), the statusline rate-limit cache (the freshest of `~/.cache/ai-resources/claude-rate-limits.json` and the
daily-report workspace copy `~/.openclaw/workspace/scripts/ai-usage/rate-limits-cache.json`, or `AI_RESOURCES_CLAUDE_SIDECAR`:
five-hour and seven-day used percentage and reset time; a window whose reset already passed is flagged and raises no alert) and
a manual credits file `~/.config/ai-resources/credits.json` with
`{"prepaid_usd":..., "promo_usd":..., "promo_expires":"YYYY-MM-DD", "updated":"YYYY-MM-DD"}` (mode 0600, no secrets). No local
source holds the remaining credit, so the file is the source: absent means "unknown", older than 7 days means "stale". Alerts:
prepaid billing, weekly quota at 100%, five-hour at 90% or more, promo expiring within 7 days, balance below
`AI_RESOURCES_CREDIT_LOW_USD` (default 10). doctor, `audit` (`--json`, `--billing-only`) and the daily Slack section show them.
Nothing here writes. On 2026-10-22 the host read `stripe_subscription` (the plan expected `prepaid`); both are handled.

### D3. Writers added in this round (all confirmed, reversible, recorded in state, never headless)

1. **Global CLAUDE.md migration** (exception to AGENTS.md "outside the kit block is never touched"). Only with a per-block
   confirmation (default keep), an exclusive backup first, the destination (a skill `operator-<slug>` with an operator-typed
   trigger description, or a rule with operator-supplied `paths:`) written exclusively before the block is replaced by a
   one-line pointer, text moved verbatim, a sha256 recorded, and a restore (`ai-resources setup --restore-claude-md`) that
   refuses if the destination or the pointer was edited since. The kit block is never read as a block, never edited (its hash
   must be unchanged) and keeps the polling guidance. Text with no headings, or an unclosed code fence, is one block, never split.
2. **Hygiene.** Unused user-level MCP servers and plugins (zero calls in 30 days, from the usage index) are offered one by
   one, default No. The user-scope MCP config is read from `~/.claude.json` `mcpServers` (read-only; `claude mcp list` health-checks
   every server and has no JSON output), saved to a private copy (`<config>/ai-resources/hygiene/`, mode 0600, created
   exclusively; no private copy, no removal) and removed with `claude mcp remove <name> -s user`; the state holds a copy with
   env and header values redacted, so no secret lands in the state file. Plugins are disabled with
   `claude plugin disable <id> --scope user`. `ai-resources setup --restore-hygiene` re-adds from the private copy
   (`claude mcp add-json`, checked against its recorded sha256) or re-enables the plugin, and says out loud what it could not
   restore. Kit-owned entries (engram, the kit plugin) are never offered. Persona subagents are rendered only for the
   active domains (`domains.active_domains()`, from `AI_RESOURCES_SKILLS`); inactive kit personas go through the existing stale
   path and come back when the domain is enabled.
3. **Haiku notification route** (D4). A sibling module with its own guard.
4. **agy MCP file.** `~/.gemini/config/mcp_config.json` is written by the kit after one confirm for `engram` (fill-only) and a
   multiselect (default none) over the Claude Code user-level servers that are plain stdio commands with no environment values.
   Merge-preserving, fill-only, backed up exclusively before the first edit, state-recorded (`tracking.agy_mcp_added`);
   `ai-resources setup --restore-agy-mcp` removes only an entry that still equals what the kit wrote. This reconciles the older
   wording "the kit does not write the agy MCP file" (ADR-0007 section 9 forbids `--strict-mcp-config`, which this does not
   use). `gemini-3.8-flash-low` sits in a fallback chain only when the lane flag is on, a Google acknowledgement for the
   CURRENT text exists and gates G0-G7 are proven (`agy_flash_low_reason`, the same order as `agy_role_reason`); otherwise it is
   left out and the operator is told. Gates G0-G7 and the acknowledgement text are untouched.

### D4. Haiku notification route is its own guarded module

`openclaw_notification_route.py` may write only an `agents.entries.<kit notifications agent>` entry on the `haiku` pin and
`bindings[]` peer entries for the `*-notifications` channels the operator ticks. It is not a widening of the two-key Slack
writer (`openclaw_slack_cost.py` is untouched) and has its own `assert_notification_route_safe`. Every key is classified at run
time by `openclaw_reload_rules` (fail closed). On OpenClaw 2026.10.5 the spike (below) classifies `bindings` as `none` and
`agents.entries` as hot; the writer still prints the patch and records `host_restart_pending` when a key reads restart or the
rules cannot be read. It is never applied unattended.

### D5. Workboard disable and heartbeat off

`plugins.entries.workboard.enabled=false` and `agents.entries.main.heartbeat.every="0m"` are confirmed value migrations in
`openclaw_cost_migrations.py`, recorded by id in `state.openclaw.cost_migrations_applied`, classified at run time and
applied only if hot; otherwise they are printed and recorded as pending for the drained path (ADR-0003). The heartbeat question
defaults to keeping the 2h isolated light heartbeat.

### D6. One domain map

`domains.py` holds the persona domain map (`dart-flutter` -> `flutter`; `devops`, `angular`, `symfony`, `api-platform` -> their
own names) and `active_domains()` over `AI_RESOURCES_SKILLS`. `skill_scope.py` and the persona filter both use it; a parity
test requires every `agents/personas` domain to be mapped.

### D7. Polling guard

`hooks/kit_sleep_guard.py` (stdlib only, fail-open) blocks with exit 2 a Bash `sleep` longer than 30 seconds (suffixes `s`,
`m`, `h`, `d`; chained and `&&` forms) and points to `run_in_background` and Monitor. The sleep regex lives in a tiny shared
stdlib helper (`sleep_pattern.py`) used by the hook and by `audit_sources.py`; the hook imports only that helper. The audit
alerts when the poll share is strictly above `POLL_SHARE_ALERT` (0.05, overridable with `AI_RESOURCES_POLL_SHARE_ALERT`). The
hook does not fire for OpenClaw `claude-cli` children (ADR-0005, ADR-0007 section 9); the AGENTS.md polling rules stay the
guard there.

## Spike results (read-only, 2026-10-22, OpenClaw 2026.10.5, Claude Code 2.1.308)

| Question | Answer |
|---|---|
| reload class of `bindings` | `none` (no restart); an indexed path such as `bindings[0]` classifies `restart`, so the writer classifies the top-level key and falls back to print-only when the rules disagree |
| `agents.entries.<new>`, `plugins.entries.workboard.enabled`, `agents.entries.main.heartbeat.every`, `agents.defaults.utilityModel` | hot |
| announcement suppression key | none. `openclaw config schema` has only `agents.defaults.subagents.announceTimeoutMs` (a timeout). Nothing is written; doctor prints an INFO line |
| `claude mcp list|get <name>|remove <name> -s <scope>|add-json <name> <json>` | no `--json` on `list` or `get`, and `list` health-checks every server; the kit reads `~/.claude.json` `mcpServers` (read-only) for the candidates and the restorable record, and only `remove` and `add-json` are run |
| `claude plugin list --json`, `disable <id> --scope user [--json]`, `enable` | present; `--json` prints one machine-readable line |
| engram `mem_judge` / SessionStart size | `user-prompt-submit.sh` names `mem_judge` only inside the ToolSearch `select:` line that ADR-0013 section 4 already replaces. `session-start.sh` fetches `/context?project=` from the server and prints it; it has no size or limit knob and the server API for a limit is not documented, so no second hash-guarded patch is written |
| `claude-haiku-5-5` valid for the workspace key | not probed. The probe is opt-in and needs the workspace key; no secret was read. The host profile is unchanged |

## Not kit-controllable (documented, nothing written)

The engram SessionStart `/context` payload and the server-side `mem_judge`; OpenClaw completion announcements (only a
timeout exists); the remaining prepaid credit and the promo expiry (no local source: the manual credits file is the source);
hooks inside OpenClaw `claude-cli` children; the `--strict-mcp-config` flag (forbidden by ADR-0007 section 9).

## Consequences

- A host with a saved `balanced` profile changes models at its next `setup` (Tester and doc-writer and package-upgrade to Haiku,
  architect to Sonnet) after an old -> new diff is shown. `AI_RESOURCES_SKILLS` still decides which personas exist.
- doctor and audit warn immediately on a prepaid host with an exhausted weekly quota.
- Cost claims remain API-equivalent estimates; no saving is claimed without `ai-resources audit snapshot` before and after and
  `ai-resources audit compare` (ADR-0013 rule c).
- Rollback: each writer has a restore path in teardown and state; revert the commit for code; delete
  `~/.cache/ai-resources` to drop the usage index; remove the hook entry from settings to drop the guard.
