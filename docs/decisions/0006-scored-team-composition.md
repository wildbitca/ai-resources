# ADR-0006: Scored team composition (a scores database and a composition engine for `ai-resources setup`)

**Status**: Proposed (Phase A is implemented; the architect review and the browser re-check of the LLM-summarised board figures are still pending, see "Open items")
**Date**: 2026-10-13
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

`setup/compat.py` (`cell()` and `plan()`) is the only gate. The database never stores verification (a `verified` key is a validation error). The engine derives a coarse grade from the cell: `UNVERIFIED` when `verified=False`, `DECLARED` when `verified=True`. It is never called "live", because the matrix cannot prove runtime behaviour, and the free-text `source` note is not parsed.

### 4. The usage-policy gate

Separate from verification and run before it. Each cockpit, engine and pool has a `usage`: `dispatch-ok`, `direct-only` or `instructions-only`.

- `agy` (CLI) and the OpenClaw antigravity engine are `direct-only`: the Antigravity FAQ says third-party tools (Claude Code, OpenClaw and OpenCode are named) that access Antigravity violate its ToS, and `agy -p` is for direct scripting and CI. Account suspension covers Antigravity, Gemini CLI and Code Assist. They are never a composition target, never proposed as an `openclaw-agent` or dispatch backend, and **no flag lifts this**, `--allow-unverified` included.
- Codex is `instructions-only` as a cockpit (all 21 compat cells) and the kit has no `codex exec` dispatch path.

The compat cell of the antigravity engine is `CONFIGURE` in code. Composition excludes it through this gate without changing compat; reconciling the cell with the FAQ is a follow-up ADR.

### 5. Mode names and their mapping

| Engine mode | `selection.SHAPES` | Meaning |
|---|---|---|
| `uniform` | `single` | one model for every role (not `single-model`, which is the existing `state.mode` for claude-native) |
| `single-provider` | `single-provider` | a model per role, all from one provider |
| `mixed` | `multi-provider` | balance pools within each role's tolerance band; offered only with `--allow-unverified` and always marked |

### 6. Algorithm and tolerance classes

Deterministic greedy, per role: candidates (enabled, gated, not stale, scored) -> `S*(r)` (best verified score; best overall only when none is verified) -> band `score >= S* - tolerance(class(r))` -> pick by projected pool pressure. Classes (POLICY): critical 0.3 (planner, implementer, software-architect, security-auditor), standard 0.5 (code-reviewer and verifier by operator decision Q2, generalPurpose, the maintainers, tester), low 1.0 (package-upgrade, doc-writer, explore). Roles are walked in ascending importance so low-importance roles move first. Reviewer and verifier prefer another vendor than the implementer's, softly, within 0.5 of `S*` (Q9; the single supporting paper is not peer reviewed, so the rule is `INFERRED` and soft). `weighted_loss = sum importance x (S* - chosen)`; above `loss_cap` the result is a refusal.

Unverified candidates enter a band only under `--allow-unverified`, are anchored at the best verified score and never displace a verified candidate on a tie (see the plan deviation recorded in the handoff).

### 7. Headroom

Pools are keyed by billing class: `subscription`, `metered-api`, `gateway-metered`. Each pool has a headroom source (`statusline-sidecar`, `openrouter-key`, `vertex-quotas`, `kit-counters`, `none`) and a state (`known`, `stale`, `unknown`). `unknown` is first class: the engine then uses the static weight the operator chose in setup (ample 1.0, normal 0.7, scarce 0.3; default 1.0). A live reading may only scale the static weight down; it can change which band member is picked, never the band. Antigravity and Codex pools are always `unknown` for live reads. Live reads are opt-in per pool and never run from detection. The Claude headroom is read from an existing status-line sidecar only: the kit installs nothing and never writes `~/.claude`. Only that reader is live in production: the OpenRouter key and Vertex quota readers take an injected fetcher and the kit ships none (a network fetcher needs its own review), so setup does not offer the live opt-in for those pools; `flow.has_live_reader` is true only where a read can work.

Under a gateway, balancing moves metered money, not subscription quota. Claude subscription headroom is plan-wide (not per model), Pro and Max only, and absent until the first API response.

### 8. Output limits

Composition emits `ProfileState.customizations` over `balanced-claude-55`. It never emits `backend`, `agentId` or a cross-vendor fallback. A profile entry names a model and never a route, so the engine keeps ONE route per model across the whole team (the route covering the most roles, then verified, then the higher weighted score, then the lower cost) and reports the other routes of that model as `route-collapsed` rejections. The pool, the mark and `provenance.routes` therefore describe the route the stored overlay stands for; a route-level pick that no profile field can express is never proposed. Balancing between two routes of the same model is out of scope until a profile can express a route. Fable 5.1 is an opt-in candidate only on a declared Max or Premium seat, or API billing (decision Q16), never by default.

### 9. Harness approval rule (Phase B)

Live evaluation is a separate plan and approval: `--budget-usd`, `--max-tokens` and `--approved-by` are required, there is no default budget, the runner never runs `agy`, and its output is a candidate-row file for operator review, never merged automatically. Phase A spends nothing.

### 10. `validate_kit.py` and the `scores/` directory

Checked read-only on 2026-10-13: `scripts/validate_kit.py` addresses specific paths (`skills`, `workflows`, `agents`, `hooks`, plugin manifests, `resources.json`) and does not iterate the repository root, so a new top-level `scores/` directory is ignored and needs no registration. The scores file is validated by `tests/test_scores_schema.py` and `ai-resources models scores check`. The Homebrew formula installs `Dir["*"]` into `libexec`, so `scores/` ships with the kit.

## Consequences

- Mixed mode is honest: unverified, marked, and refused without `--allow-unverified`.
- Real balancing will mostly happen among Anthropic models until a cross-provider cell is verified.
- Two price sources exist (this database and `audit.PRICES`); `audit` keeps showing n/a for 5.5 (accepted for v1).
- The dispatch side has no usage-counter writer yet; the counter file format is defined and read if present.

## Open items

- Architect review of this ADR; then Status moves to Accepted.
- Browser re-check of the board figures that were summarised by an LLM (Vals Terminal-Bench 4.0 rows, Sonnet 5.5 64.14 vs 53.0).
- Follow-ups: the antigravity engine compat cell vs the Antigravity FAQ; a `codex exec` dispatch path; the dispatch-side counter writer; the Vertex alias-pinning pitfall in the Vertex setup docs.
