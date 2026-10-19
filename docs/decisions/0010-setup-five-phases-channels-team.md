# ADR-0010: Setup in five phases, unified team presets, disable-only channels (LiteLLM leaves the new-setup choice)

**Status**: Proposed: conditions applied, awaiting re-review. Plan: `.agent-output/planner/setup-five-steps-plan.md`,
revision 3, approved by the operator. The software-architect and security-auditor reviewed this ADR on 2026-10-19 with
the verdict APPROVE WITH CONDITIONS; the conditions are written into "Review conditions (architect, security-auditor,
2026-10-19)" below and applied by the steps that carry them. Nobody has re-reviewed the result, so this ADR is NOT
Accepted and its sign-offs are not self-signed.
Implemented so far: S1 (checker-independence test), S2 (checker fixes), S3 (read-only channel inventory), S5 (LiteLLM
leaves the wizard; see the S5 note), S9 (five-phase headers), S11 (main-tool step, channel intent without execution),
S13 (Audio step), S14 (sign-in) and S15 (More phase). See "Implementation status" at the end for the later steps.
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
presets carry no agy writer (every preset keeps agy off the writer roles); Custom and gate-based unlocking of a
writable lane stay governed by ADR-0007. Agy Gemini goes only on roles the stored Google acknowledgement lists.

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
| 0001 d3 | the kit never patches `channels.*`; channel accounts are disabled only by running the OpenClaw CLI after per-change confirmation | operator (approved per N5); security-auditor APPROVE WITH CONDITIONS, awaiting re-review |
| 0003 | carve-out: a confirmed disable may stop that account and restart its listener, never the Gateway | operator (approved); security-auditor APPROVE WITH CONDITIONS, awaiting re-review |
| 0007 §8, §11 | the checker fix and the gateway fallback rule; the Google role list adds the planner and software-architect seconds; presets carry no agy writer candidates; `EVIDENCE` is merged from a validated committed record | operator; software-architect and security-auditor APPROVE WITH CONDITIONS, awaiting re-review |
| 0008 | gateway choice: LiteLLM load-only, with the 4/10 evidence | operator |
| 0009 §2, §8 | five phases; held/on cells and the restore-saved-tick rule | operator |

Channels-rule restatements that S12 re-words (line numbers re-checked before editing): `openclaw_host.py`
`assert_channels_safe` and its comments, `cockpits/_openclaw_host.py`, `docs/openclaw/pitfalls.md`,
`docs/openclaw/runbook.md`, `docs/multi-model.md`, `skills/openclaw-operations/SKILL.md`,
`profiles/openclaw-host.json5`. No gate symbol (`GATES`, `AGY_LANE_ADR_ACCEPTED`, `CODEX_LANE_ADR_ACCEPTED`,
`WRITABLE_BACKENDS`) changes in any step.

## Review conditions (architect, security-auditor, 2026-10-19)

Both reviewers returned APPROVE WITH CONDITIONS. Status after this section: **Proposed, conditions applied, awaiting
re-review.** The implementer wrote the conditions here; it did not sign for either reviewer.

### Operator decisions taken with the review

- **(a) Evidence record, hook plus strict loader (S7).** A PreToolUse hook rule denies Write, Edit and shell writes to
  `scripts/ai_resources/data/agy_gate_evidence.json` in every agent session, not only lane children. The loader fails
  closed when the file is untracked, or differs from `HEAD`, in a git kit root. `doctor` and `lanes gates` show the
  last-touching commit and its author. A hand-written JSON that passes the schema but was not made by
  `lanes gates record` is rejected (test).
- **(b) Wording.** "Agy is never a writer" is read as "presets carry no agy writer". Custom and gate-based unlocking
  stay governed by ADR-0007 and are not changed here.
- **(c) Gemini second opinions on the planner and the software-architect are approved.** This widens the Google
  acknowledgement role list (ADR-0007 section 9: adding a role means re-signing). An acknowledgement stored before
  lists neither role, so it is not valid for them and the operator is asked again through the existing "Extend the
  Google acknowledgement to: planner, software-architect?" prompt (default No, never auto-answered). `ACK_TEXT` and
  its hash do not change. This **reverses OQ-P3-1 (pre-selection)** of the phase 3 plan, which left the planner, the
  software-architect and explore unticked: the terms checklist now pre-ticks them; the implementer stays unticked.
  The planner and architect seconds are **display-only**: no workflow step with `lane_member: second` is added for them
  (adding workflow steps changes generated skills and the dispatch contract and is not risk-free), so they show in the
  team table and are held, and nothing dispatches them until a later change adds those steps.
- **(d) S5 note.** S5 (LiteLLM leaves the wizard) landed before this ADR was signed off, with the operator's explicit
  approval in the session. It is one isolated commit and `git revert` undoes it.

### Architect conditions

- **C3. Agy slug confirmation is per slug.** `AGY_LANE_MODELS_CONFIRMED` stays a constant `False`. A slug counts as
  confirmed when it is in the record's `confirmed_slugs` (G1), so the "[unconfirmed slug]" tag is decided per slug.
  `AGY_LANE_MODELS_CONFIRMED` joins the gate symbols that never change by hand (with `GATES`, `AGY_LANE_ADR_ACCEPTED`,
  `CODEX_LANE_ADR_ACCEPTED`, `WRITABLE_BACKENDS`); the constants are never flipped.
- **C4. Lazy evidence loader.** The loader is an explicit cached function; `EVIDENCE` stays a literal; there is no
  import-time I/O. The manifest listing with file hashes is committed with the record. The G0 anchor must name a
  heading, in the ADR-0007 Evidence section, that contains the record's `run_id`.
- **C6. No saved disable intent.** The channel default comes from the current inventory on every run. Nothing about a
  disable decision is stored in the state. The `agy_lane_gates.py` docstring is reworded to describe the loader and
  the hand-edit flow accurately.

### Security conditions (S12)

- Re-list immediately before each change and plan only accounts that are still enabled in the fresh list; refuse when
  the list is unreadable; re-list after each change and report any difference from what was expected.
- The undo line says "re-add with credentials" unless re-adding without credentials is proven. No openclaw command is
  run other than the single read-only `openclaw channels list --all --json` fixture capture, and only if the fixture
  is not already captured.
- Names are validated with `fullmatch`, never with `$`.
- The inventory read has the executor's cap (`max_bytes`), scrubbed environment and 60 s timeout.
- The `--agent` field is removed from the argv.
- `execute()` itself checks every guard (TTY, not `--dry-run`, not `--yes`, the cockpit is OpenClaw, reload rules
  readable and hot); the wizard does not rely on a caller having checked.
- No secret appears in a log or a print (tested). The extra Telegram confirm defaults to No and is never auto-answered.
  A stale `_channel_inventory` is cleared when a re-list comes back empty.

## Sign-offs

| Reviewer | Scope | Verdict |
|---|---|---|
| software-architect | this ADR; pre-checks S1 agy translation, S4a single-row lanes, S6 test ban, S7 manifest and packaging, S11 writes, S13 flow | APPROVE WITH CONDITIONS (2026-10-19); conditions applied, awaiting re-review |
| security-auditor | this ADR, channel restatements; S7 and S12 | APPROVE WITH CONDITIONS (2026-10-19); conditions applied, awaiting re-review |

The implementing session cannot spawn a reviewer, so it records the verdicts it was given and the conditions it
applied; it does not sign. This ADR stays Proposed until both reviewers re-review the implemented result.

## Rollback

Each plan step is one local commit and one `git revert`. There is no state schema change: `SCHEMA_VERSION` stays 1.0
and new fields are optional. This ADR can go back to Proposed or Superseded.

## Related

- Plan: `.agent-output/planner/setup-five-steps-plan.md`
- [ADR-0007](0007-subscription-lanes.md), [ADR-0008](0008-setup-wizard-tui-on-rich-questionary.md),
  [ADR-0009](0009-setup-tools-how-team.md)
