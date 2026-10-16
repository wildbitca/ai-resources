# ADR-0006: Scored team composition (a scores database and a composition engine for `ai-resources setup`)

**Status**: Accepted (2026-10-13, architect review against HEAD e0fb7bc: every decision below is implemented; the limits of the shipped data are stated in "Limits of the shipped data". This accepts the mechanism, not the shipped scores as a basis for per-role choices.)
**Date**: 2026-10-13
**Amended by**: [ADR-0007](0007-subscription-lanes.md) (§4 and §8 scoped to composition; "Output limits" and "Rollback" unaffected for compose; the revisit trigger "the Antigravity FAQ changes" is not met)
**Decision makers**: planner, implementer, operator (decisions of 2026-10-13 recorded in the plan `.agent-output/planner/scored-team-composition-plan.md`, section 11)

## Context

Setup picks a profile (`balanced-claude-55`, `measured-best`, ...) as a fixed role-to-model table. The operator wants a team proposed from the providers and cockpits that were enabled, in three modes, with a "why" table, and with subscription quota or spend balanced across pools without losing performance.

Constraints: [ADR-0002](0002-provider-agnostic-model-selection.md) (WHAT the user wants lives in `setup-state.yaml`, HOW a slot moves lives in the overlay), [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md), [ADR-0005](0005-cross-provider-role-dispatch.md) (roles run on Claude Code; no `backend` or `agentId` in `by_role`), `rules/017-multimodel-routing.mdc`, and the repository `AGENTS.md` (T26: nothing scratch under `/tmp`; T39: no tool call open longer than about 2 minutes).

The structural fact: under the shipped compat matrix Anthropic is the only provider with a verified cell that can serve kit roles through `by_role` (`tests/test_composition_matrix_facts.py` pins it). A multi-provider team is therefore unverified by construction until a cross-provider cell is verified on purpose.

## Decision

### 1. Score database ownership

The kit ships `scores/model-scores.yaml` read-only, versioned independently of the kit (`schema_version`, `dataset_version`). User overrides live in a separate file, `$XDG_CONFIG_HOME/ai-resources/model-scores.yaml`, never in `model-pins.json` (ADR-0002: WHAT vs HOW). An override row must carry `reason` and `as_of`, shadows a shipped row by key, and is flagged in every report. Refresh is by proposal (`ai-resources models scores refresh`): nothing is auto-approved, a price or a score is never changed without an answer.

### 2. Label taxonomy

`MEASURED-HERE`, `MEASURED-3P`, `VENDOR`, `INFERRED`, `UNKNOWN`, each with a date. A composed score carries the weakest contributing label. Importance and tolerance are POLICY, never evidence. A benchmark proxy is `MEASURED-3P` and `role_agnostic: true`; a role row derived from a proxy is `INFERRED` and names its `basis`. Source spellings are folded onto the five labels by an explicit map in `scoring/evidence.py`; an unrecognised spelling is `UNKNOWN`.

### 3. The verification gate

`setup/compat.py` (`cell()` and `plan()`) is the only gate. The database never stores verification: a `verified` key in the shipped file is a validation error, and one in an override row is dropped with a warning. The engine derives a coarse grade from the cell: `UNVERIFIED` when `verified=False`, `DECLARED` when `verified=True`. It is never called "live", because the matrix cannot prove runtime behaviour, and the free-text `source` note is not parsed.

### 4. The usage-policy gate

Separate from verification and run before it. Each cockpit, engine and pool has a `usage`: `dispatch-ok`, `direct-only` or `instructions-only`.

- `agy` (CLI) and the OpenClaw antigravity engine are `direct-only`: the Antigravity FAQ says third-party tools (Claude Code, OpenClaw and OpenCode are named) that access Antigravity violate its ToS, and `agy -p` is for direct scripting and CI. Account suspension covers Antigravity, Gemini CLI and Code Assist. They are never a composition target and never proposed as an `openclaw-agent` or dispatch backend by composition. In composition, Antigravity is direct use only; no composition flag lifts that, `--allow-unverified` included. The lanes pass is governed by [ADR-0007](0007-subscription-lanes.md).
- Codex is `instructions-only` as a cockpit (all 21 compat cells) and the kit has no `codex exec` dispatch path.

The compat cell of the antigravity engine is `CONFIGURE` in code. Composition excludes it through this gate without changing compat; reconciling the cell with the FAQ is a follow-up ADR.

### 5. Mode names and their mapping

| Engine mode | `selection.SHAPES` | Meaning |
|---|---|---|
| `uniform` | `single` | one model for every role (not `single-model`, which is the existing `state.mode` for claude-native) |
| `single-provider` | `single-provider` | a model per role, all from one provider |
| `mixed` | `multi-provider` | balance pools within each role's tolerance band; offered only with `--allow-unverified` and two or more enabled providers, and always marked |

In single-model setup (claude-native, no gateway) only `uniform` is offered and the team is stored as Claude aliases. There the enabled set is what setup declared: with no provider step it is `anthropic` only and never widens to detected providers.

### 6. Algorithm and tolerance classes

Deterministic greedy, per role: candidates (enabled, gated, not stale, scored) -> `S*(r)` (best verified score; best overall only when none is verified) -> band `score >= S* - tolerance(class(r))` -> pick by projected pool pressure. Classes (POLICY): critical 0.3 (planner, implementer, software-architect, security-auditor), standard 0.5 (code-reviewer and verifier by operator decision Q2, generalPurpose, the maintainers, tester), low 1.0 (package-upgrade, doc-writer, explore). Roles are walked in ascending importance so low-importance roles move first. Reviewer and verifier prefer another vendor than the implementer's, softly, within 0.5 of `S*` (Q9; the single supporting paper is not peer reviewed, so the rule is `INFERRED` and soft). `weighted_loss = sum importance x max(0, S* - chosen)`; above `loss_cap` the result is a refusal. In mixed mode every pick is inside its band, so the loss is at most the sum of importance x tolerance (4.91 today, below the cap of 5.0) and the cap cannot bind there; it guards `uniform` and `single-provider`, where the shape contract can force a role out of its band.

The greedy above is the `mixed` procedure. The other shapes:

- `uniform` picks the one model with the highest importance-weighted score over the roles it covers (then verified in every role, lower cost, id); it does not walk bands.
- `single-provider` runs the mixed procedure inside each provider's candidate subset (routes collapsed per subset, see 8) and ranks providers by weighted score, then cost, then id. A loss-cap refusal on the best-ranked provider falls through to the next one; the team is refused only when every provider refuses, and the reported refusal is the best-ranked one's.
- Default kept: when the result is Anthropic-only and the base profile's model is inside the band on every role (and honours the mode's shape), the base profile is kept and no overlay is written.

Unverified candidates are admitted only under `--allow-unverified`. `S*` stays anchored at the best verified score, and an unverified candidate never wins a full tie against a verified one (see the plan deviation recorded in the handoff). It can still be picked inside the band on lower pool pressure, or by the diversity preference; it is then marked `unverified`.

### 7. Headroom

Pools are keyed by billing class: `subscription`, `metered-api`, `gateway-metered`. Each pool has a headroom source (`statusline-sidecar`, `openrouter-key`, `vertex-quotas`, `kit-counters`, `none`) and a state (`known`, `stale`, `unknown`). `unknown` is first class: the engine then uses the static weight the operator chose in setup (ample 1.0, normal 0.7, scarce 0.3; default 1.0). A live reading may only scale the static weight down; it can change which band member is picked, never the band. Antigravity and Codex pools are always `unknown` for live reads. Live reads are opt-in per pool and never run from detection. The Claude headroom is read from an existing status-line sidecar only: the kit installs nothing and never writes `~/.claude`. Only that reader is live in production: the OpenRouter key and Vertex quota readers take an injected fetcher and the kit ships none (a network fetcher needs its own review), so setup does not offer the live opt-in for those pools; `flow.has_live_reader` is true only where a read can work.

Under a gateway, balancing moves metered money, not subscription quota. Claude subscription headroom is plan-wide (not per model), Pro and Max only, and absent until the first API response.

### 8. Output limits

Composition emits `ProfileState.customizations` over `balanced-claude-55`. It never emits `backend`, `agentId` or a cross-vendor fallback. A profile entry names a model and never a route, so the engine keeps ONE route per model across the whole team (the route covering the most roles, then verified in every role, then the higher importance-weighted score, then the lower cost, then the route id) and reports the other routes of that model as `route-collapsed` rejections. In `single-provider` the collapse runs inside each provider's subset, so a global collapse cannot strip a provider of the route it needs. The pool, the mark and `provenance.routes` therefore describe the route the stored overlay stands for; a route-level pick that no profile field can express is never proposed. Balancing between two routes of the same model is out of scope until a profile can express a route. Fable 5.1 is an opt-in candidate only on a declared Max or Premium seat, or API billing (decision Q16), never by default. The declaration is the operator's (`--allow-fable` or a default-No question in the wizard); the kit does not detect the seat.

### 9. Harness approval rule (Phase B)

Live evaluation is a separate plan and approval: `--budget-usd`, `--max-tokens` and `--approved-by` are required, there is no default budget, the runner never runs `agy`, and its output is a candidate-row file for operator review, never merged automatically. Phase A spends nothing.

### 10. `validate_kit.py` and the `scores/` directory

Checked read-only on 2026-10-13: `scripts/validate_kit.py` addresses specific paths (`skills`, `workflows`, `agents`, `hooks`, plugin manifests, `resources.json`) and does not iterate the repository root, so a new top-level `scores/` directory is ignored and needs no registration. The scores file is validated by `tests/test_scores_schema.py` and `ai-resources models scores check`. The Homebrew formula installs `Dir["*"]` into `libexec`, so `scores/` ships with the kit.

## Limits of the shipped data (Phase A)

This ADR ships a mechanism. The shipped scores cannot yet tell roles apart, and the engine does not show that a composed team is better than the curated baseline.

- All 360 role rows in `scores/model-scores.yaml` (dataset `2026-10-13`) are `INFERRED`. Each one is the same role-agnostic proxy, Vals Terminal-Bench 4.0 accuracy / 10. A model, route and effort has the same score in all 15 roles. No independent benchmark exists for planning, review, verification or security, so those rows are placeholders taken from a terminal-coding benchmark.
- Claude Sonnet 5.5 (6.3) has the top score in every role. On the shipped data with only verified cells (every Anthropic route, any enabled set), every role in all three modes (`uniform`, `single-provider`, `mixed`) resolves to Sonnet 5.5. The single-model `uniform` path does the same.
- The one exception is not per-role evidence. In `mixed` with `--allow-unverified`, OpenAI enabled and the LiteLLM gateway, pool balancing moves six standard-class and low-class roles (code-reviewer, verifier, crashlytics-fixer, crossplane-upjet-maintainer, terraform-maintainer, explore) to `openai/gpt-6-astra` (6.0, UNVERIFIED). That pick spreads load. It is not a judgement that astra is better at those roles. Under the OpenRouter gateway the same run stays all-Sonnet.
- Because of this, the default-kept rule never fires on the shipped data. A composed team departs from `balanced-claude-55` and from the rule 017 recommendation on six roles. Planner, software-architect and security-auditor go from Opus 5.5 to Sonnet 5.5, because Opus's 5.8 falls outside the critical band of 6.3 - 0.3 = 6.0. Explore, tester and package-upgrade go from Haiku 5.5 to Sonnet 5.5. The only basis for this is the coding proxy, and the why table shows every one of these picks as `INFERRED`.
- Real per-role differentiation needs one of two things. The first is Phase B measurement (`MEASURED-HERE` rows per role, section 9). The second is operator override rows in `$XDG_CONFIG_HOME/ai-resources/model-scores.yaml`, each with a `reason` and an `as_of`. Until one of them lands, read the composed team as "the best model on one coding benchmark, with load balancing", not as a role-aware recommendation. For the critical roles the curated baseline stays the safer default.
- The Terminal-Bench 4.0 figures were summarised by an LLM from the Vals board and have not been re-checked in a browser (see Follow-ups). They are dataset content, versioned on their own (decision 1).

## Consequences

- Mixed mode is honest: unverified, marked, refused without `--allow-unverified`, and not offered with fewer than two enabled providers.
- Real balancing will mostly happen among Anthropic models until a cross-provider cell is verified. With the shipped scores and default weights, the review runs never saw pool pressure move a role from Sonnet 5.5 to another Anthropic model. Inside the low band Opus 5.8 ties Sonnet's pool, and the tie goes to the higher score.
- On the shipped data, accepting a composed team replaces the curated baseline on six roles, and only a role-agnostic proxy supports the change (see "Limits"). The feature is opt-in and shows the role diff before it stores anything, so the operator sees this before agreeing.
- Two price sources exist (this database and `audit.PRICES`); `audit` keeps showing n/a for 5.5 (accepted for v1).
- The dispatch side has no usage-counter writer yet; the counter file format is defined and read if present.
- Of the live headroom readers, only the Claude status-line sidecar is live in production. The OpenRouter and Vertex readers exist but have no shipped fetcher, and the kit-counter reader has no writer.

## Rollback

- Composition is opt-in. A setup run without `--compose`, `--headroom`, `--headroom-live`, `--why-json` or `--allow-fable`, and without choosing "compose" in step 6, writes the same `executors.yaml` as before this ADR.
- To drop a stored composed team, re-run `ai-resources setup`, choose a plain profile in step 6 and decline "Customize any role?". This clears `profile.customizations`. The composed team never wrote `backend`, `agentId` or a host config outside the profile path, so nothing else needs undoing.
- To remove the feature, delete `scores/`, `scripts/ai_resources/scoring/` and the compose branch of the wizard (`_compose_profile`, `_preflight_compose`, the `--compose` family of flags). `profiles.merge_customizations` and `profiles.to_executors` are unchanged, and the `profile.composed` and `selection.headroom` state fields are additive.
- Known gap: the plain-profile path clears `customizations` but leaves the earlier `profile.composed` provenance in `setup-state.yaml`. Nothing reads that field, so routing is correct, but the record describes a team that is no longer stored (follow-up below).

## What made this Accepted, and what would revisit it

Accepted on 2026-10-13 because the architect review found every decision above implemented at HEAD:

- the band rule and the `--allow-unverified` anchoring;
- one route per model, collapsed per provider subset in single-provider;
- only the sidecar headroom reader is live, and `has_live_reader` gates the opt-in;
- `agy` and the antigravity engine are direct-only in composition, and no composition flag lifts that;
- Fable is opt-in;
- the soft diversity preference;
- the loss cap with provider fall-through;
- no `backend` or `agentId` in the output.

The scoring and composition suites passed at review (2234 tests), and the limits are stated above.

Revisit this ADR, rather than only the dataset, when any of these happens:

- a cross-provider compat cell is verified;
- a profile can express a route;
- Phase B produces `MEASURED-HERE` rows that change the band or class model;
- the Antigravity FAQ changes.

## Follow-ups (not blocking)

- Browser re-check of the board figures that were summarised by an LLM (Vals Terminal-Bench 4.0 rows, Sonnet 5.5 64.14 vs 53.0). This is a dataset item; a correction ships as a new `dataset_version`.
- Phase B (section 9) or operator override rows, to give roles real differentiation.
- Clear `profile.composed` when a plain profile replaces a composed team.
- The antigravity engine compat cell vs the Antigravity FAQ; a `codex exec` dispatch path; the dispatch-side counter writer; the Vertex alias-pinning pitfall in the Vertex setup docs.

## Note (2.2.0): headroom for lane placement

The live headroom reader added for the lanes pass (ADR-0007 FU-5) reuses this ADR's headroom model unchanged: a known, fresh reading may only scale the static weight down by the used fraction (floor 0.05); unknown or stale keeps the static weight. It reads local state only and is never called from detection. It does not touch composition: the band and the model choice are as before.

## Amended by

- [ADR-0007](0007-subscription-lanes.md): governs the lanes pass; §4 is scoped to composition.
