# ADR-0010: Setup in five phases, unified team presets, disable-only channels (LiteLLM leaves the new-setup choice)

**Status**: Proposed. Plan: `.agent-output/planner/setup-five-steps-plan.md`, revision 3, approved by the operator.
The software-architect and security-auditor sign-offs that plan step S0 requires are NOT recorded (see "Sign-offs"),
so this ADR cannot become Accepted and the steps that wait on it (S2, S4a, S4b, S6, S7, S12 and what depends on
them) are not implemented.
**Date**: 2026-10-18
**Decision makers**: operator (N1-N5 answered, D1-D3 given), implementer (record only).
**Amends**: [ADR-0001](0001-model-pins-and-auto-update.md) d3, [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md),
[ADR-0007](0007-subscription-lanes.md) §8 and §11, [ADR-0008](0008-setup-wizard-tui-on-rich-questionary.md) (gateway
choice) and [ADR-0009](0009-setup-tools-how-team.md) §2 and §8. The amendment text lives in each ADR under
"Amendment (ADR-0010, proposed)" and in the table below.

## Context

`ai-resources setup` asks its questions in the order of its internal steps, the starting-team presets have different
names on every row (`subscription-claude-55`, `balanced-claude-55`, `openrouter-balanced`, ...), Balanced is
Claude-only, shipped profiles let a checker share a model family with a writer, LiteLLM is offered although
Balanced with Gemini is not proven on it, and channels can only be left alone, never disabled from setup.

## Operator decisions

- **D1, starting-team semantics.** Performance: the best model per role, multi-provider when possible, price
  considered. Balanced: the best models, mandatorily multi-provider, Claude and Gemini. Low cost: price first,
  combining providers when possible. Custom: the user picks.
- **D2.** The kit executes channel changes through the OpenClaw CLI.
- **D3.** The implementer may run `openclaw channels list --all --json` once, read-only, to capture a scrubbed fixture.
  Done for S3: the real output lists accounts as plain ids with no enabled flag, so `enabled` is inferred True for a
  listed account (a dict entry with `enabled` is honoured).

## N1: LiteLLM readiness for Balanced with Gemini scores 4/10, so LiteLLM is removed from the wizard (PD-8)

The scale is readiness (10 = Balanced with Gemini works end to end today with recorded evidence). The operator's rule
was: 7 or more implements Gemini on LiteLLM; below 7 removes LiteLLM from the wizard, keeps old saved states loadable
and breaks no schema. The score is 4/10 because:

- The renderer maps `google/<m>` to `gemini/<m>` and the Providers and Credentials steps exist (+4 in total).
- Claude Code with Google on LiteLLM is UNVERIFIED (`compat.py`); nothing records that LiteLLM's translation of the
  Anthropic `/v1/messages` surface into Gemini (tool_use, thinking, cache_control, `drop_params=True`) is faithful.
- It needs a second, direct AI Studio key that account detection does not require.
- The per-model fallback union crosses checker and writer families.
- No test renders a full Gemini Balanced through LiteLLM, and no live check is allowed to lift the cell.

### Consequences (implemented, S5)

- `GATEWAY_CHOICES` offers OpenRouter only. With one gateway left, "Advanced" picks it with one line and does not
  ask "Which gateway?". The Advanced text reads "gateway (OpenRouter) with an API key and budgets".
- A saved `mode=multi-model, backend=litellm` state is offered `LiteLLM (saved; no longer offered for new setups)` as
  the default, beside OpenRouter, and prints one notice line:
  `LiteLLM is no longer offered for new setups; your saved gateway keeps working. Choose OpenRouter to switch.`
- `--backend litellm` stays accepted and prints the same notice.
- `state.backend` still defaults to `litellm` for cli and single states, silently. No schema change.
- `setup/litellm.py` rendering, the service unit, `ai-resources litellm`, the doctor LiteLLM checks and the daemon are
  unchanged. S5-ALT (keep LiteLLM and implement Gemini on it) is not used.

## N2: Gemini split by role (PD-1)

Rules: a checker (verifier, security-auditor) never shares a family with any writer and never takes a second member;
agy is never a writer (only `claude-cli` is a writable lane); agy Gemini goes only on roles the stored Google
acknowledgement lists.

`balanced` on the cli row (Gemini over Antigravity lanes, held until the gates are proven):

| Role | Primary (`by_role`) | Gemini |
|---|---|---|
| explore | Claude Haiku | `lanes.roles.explore = [agy:gemini-3.8-flash-high, claude-cli:haiku, claude-code]`, pinned |
| code-reviewer | Sonnet | `second = [agy:gemini-3.8-flash-high, claude-cli:sonnet]`, pinned |
| planner | Sonnet | `second = [agy:gemini-3.1-pro-low, claude-cli:opus]`, pinned |
| software-architect | Opus | `second = [agy:gemini-3.1-pro-low, claude-cli:sonnet]`, pinned |
| verifier, security-auditor | Opus | none (checker rule; rule 017) |
| implementer, tester, doc-writer, generalPurpose, package-upgrade, fixers | Claude (claude-cli) | none |

All `agy:claude-*` candidates leave the writer roles of `balanced`, `performance` and `low-cost`: once the gates are
proven, `writable_backends()` adds `agy-cli-lane`, and a profile listing an agy candidate on a writer would move
writer work onto agy. If the architect finds that the existing ban on agy for explore, planner and architect encodes
an orchestrator rule, the planner and architect seconds are dropped.

`gateway/balanced` (OpenRouter): explore, tester and doc-writer on `google/gemini-3.7-flash` with Claude fallbacks
keyed on the Gemini model; implementer, generalPurpose and fixers on Sonnet with a Sonnet-keyed list; package-upgrade
on Haiku; planner, software-architect, code-reviewer, verifier and security-auditor on Opus with no fallback;
`defaults.fallbacks: []`. The single row shares `balanced` (its lanes block is inert outside cli_team) and prints
`Gemini not active: 'Each on its own' runs Claude Code only ...`. The legacy LiteLLM row keeps a Claude-only,
checker-fixed Balanced and prints `Gemini not active: LiteLLM is legacy; choose OpenRouter for Gemini`.

## N3: production mode, the end-to-end ready path (PD-7)

`scripts/ai_resources/agy_gate_evidence.py` (new) validates a committed record,
`scripts/ai_resources/data/agy_gate_evidence.json`, and `agy_lane_gates.EVIDENCE` becomes `merge(GATES, load())`.
The merge only upgrades a gate to `proven` when the whole record is valid; an absent file changes nothing; an invalid
file changes nothing and `refusal()` adds `evidence file invalid: <reason>`. Loader: no symlinks, 64 KiB cap, strict
schema v1 (unknown keys, duplicate gate ids and wrong types rejected), per-gate required fields (`run_id`
`^\d{8}T\d{6}Z$`, `manifest_sha256`, `recorded_at`, `recorded_by`; G0 `anchor`, G1 `confirmed_slugs` covering every
preset agy slug, G6 `cost_per_step_tokens`, G7 `ack_text_sha256` equal to the hash of `ACK_TEXT['google']`). An
optional `cells` entry can verify only `claude/google/openrouter`. `AGY_LANE_MODELS_CONFIRMED` keeps its name and
becomes derived. The record path joins `LANE_WRITE_FORBIDDEN` and `TRIPWIRE_PATHS`.
`ai-resources lanes gates` is read-only; `lanes gates record --run-dir` refuses non-TTY, `--yes`, piped stdin and a
non-git kit root, needs a typed attestation, never runs `agy` or `openclaw`, and prints the local `git commit`.
Trust model: tamper evidence plus git review, not authentication. No record is shipped and no gate symbol is edited.

The single remaining operator action: run the announced probe window (V5, V6, V7, V9-agy, V12, the isolated phase-2
merge gate for G0, measured cost for G6, G7 approval of the Google text, one Claude Code Gemini tool-call probe
through OpenRouter), then `ai-resources lanes gates record --run-dir <window dir>` and its commit. Reaching the
Homebrew install follows the operator's normal release and upgrade path; publishing is out of scope.

Before the record: cli Balanced cells read `google <slug> via Antigravity [unconfirmed slug] (held)` with the line
`Gemini held: Antigravity gates 0/8 proven (G6, G7 partial). Next: run the probe window, then ai-resources lanes
gates record`; runtime is Claude-only. OpenRouter Balanced asks once (default No, never auto-answered) `Use Gemini on
explore, tester and doc-writer through OpenRouter (not yet verified with Claude Code)?`; Yes records
`profile.allow_unverified`, No names the roles that use the session model. After the record the cells read `(on)`,
the opt-in disappears, and writers stay on `claude-cli`.

## N4: naming and unification (PD-6)

Preset ids are the same on every row: `balanced`, `performance`, `low-cost` (the internal key `low-pricing` stays
accepted); Custom is unchanged. One resolver, `setup/profile_names.resolve(name, mode)`, is the only code that knows
the layout (`profiles/<id>.yaml` for cli and single, `profiles/gateway/<id>.yaml` for a gateway). ALIASES (old to
new, accepted forever, admitted only when a test proves the same runtime team with the gates closed):
`subscription-claude-55` to `balanced`, `subscription-performance` to `performance`, `subscription-economy` to
`low-cost`, `performance-claude-55` to `gateway/performance`, `economy-claude-55` to `gateway/low-cost`.
LEGACY_PRESET (preset-question default only; the profile's own file keeps loading): `claude-native`,
`balanced-claude-55`, `openrouter-balanced` map to `balanced`. Other shipped profiles keep their names under Custom.
`RECOMMENDED_PROFILE` becomes `recommended_profile(backend)`. The checker overlap is fixed in every shipped profile
(S2); the shrink-only baseline of S1 lists what is still open. `claude-inherit` and `all-gemini` follow open question
Q-1 (recommended: a declared in-profile exemption the wizard warns about).

## N5: channels (PD-2)

The ADR-0001 d3 and ADR-0003 amendments are approved by the operator. The kit executes DISABLE only:
`["openclaw","channels","remove","--channel",C,"--account",A]`, plus `["--agent",G]` only when the inventory names an
explicit owner. Enable is always printed for the operator (undo line, and a ticked disabled channel), never executed.
No `--delete`, no credential flag. `assert_channels_safe` stays byte-identical with its body hash pinned.
Preconditions: TTY; not `--dry-run`; not `--yes`; OpenClaw cockpit; readable inventory; reload rules classify
`channels.<C>` as hot; per-change default-No confirm; an extra Telegram confirm; scrubbed env; 60 s timeout; redacted,
bounded output; a re-list afterwards; runs after the config patch and lane writes, never in parallel.
`channels remove` may ask the Gateway to stop that account and restart its listener, never the Gateway itself.

## Other decisions

- **PD-3, five phases.** tools -> 1; cockpits -> 2; mode, gateway, providers, credentials, preset, models, profile,
  lanes -> 3; voice -> 4; lifecycle, review, apply -> 5; unknown -> 5. Internal step ids and `TOTAL_STEPS=10` stay.
- **PD-4.** Cockpit choice among ticked, installed, cockpit-capable tools; automatic when there is one.
- **PD-5, auto-answers.** Never automatic: engine, risk acknowledgement, Antigravity/Google terms and role checklist,
  OpenRouter Gemini opt-in, evidence attestation, operator and group ids, every channel confirm.

## Amendments

| ADR | Amendment | Approvers |
|---|---|---|
| 0001 d3 | the kit never patches `channels.*`; channel accounts are disabled only by running the OpenClaw CLI after per-change confirmation | operator (approved per N5); security-auditor PENDING |
| 0003 | carve-out: a confirmed disable may stop that account and restart its listener, never the Gateway | operator (approved); security-auditor PENDING |
| 0007 §8, §11 | the checker fix and the gateway fallback rule; the Google role list adds the planner and software-architect seconds; presets carry no agy writer candidates; `EVIDENCE` is merged from a validated committed record | operator; software-architect PENDING; security-auditor PENDING |
| 0008 | gateway choice: LiteLLM load-only, with the 4/10 evidence | operator |
| 0009 §2, §8 | five phases; held/on cells and the restore-saved-tick rule | operator |

Channels-rule restatements that S12 re-words (line numbers re-checked before editing): `openclaw_host.py`
`assert_channels_safe` and its comments, `cockpits/_openclaw_host.py`, `docs/openclaw/pitfalls.md`,
`docs/openclaw/runbook.md`, `docs/multi-model.md`, `skills/openclaw-operations/SKILL.md`,
`profiles/openclaw-host.json5`. No gate symbol (`GATES`, `AGY_LANE_ADR_ACCEPTED`, `CODEX_LANE_ADR_ACCEPTED`,
`WRITABLE_BACKENDS`) changes in any step.

## Sign-offs

| Reviewer | Scope | Verdict |
|---|---|---|
| software-architect | this ADR; pre-checks S1 agy translation, S4a single-row lanes, S6 test ban, S7 manifest and packaging, S11 writes, S13 flow | NOT RECORDED: the implementing session had no way to spawn a reviewer subagent |
| security-auditor | this ADR, channel restatements; later S7 and S12 | NOT RECORDED: same reason |

Until both verdicts are recorded here, S2, S4a, S4b, S6, S7 and S12 do not start (plan S0).

## Rollback

Each plan step is one local commit and one `git revert`. There is no state schema change: `SCHEMA_VERSION` stays 1.0
and new fields are optional. This ADR can go back to Proposed or Superseded.

## Related

- Plan: `.agent-output/planner/setup-five-steps-plan.md`
- [ADR-0007](0007-subscription-lanes.md), [ADR-0008](0008-setup-wizard-tui-on-rich-questionary.md),
  [ADR-0009](0009-setup-tools-how-team.md)
