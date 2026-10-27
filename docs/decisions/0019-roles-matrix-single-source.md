# ADR-0019: Roles matrix as the single source, OpenClaw as the orchestrator, acceptance as the proof

**Status**: Accepted (2026-10-27, operator decisions D1-D10, clarifications C1-C3 and O1-O4, final). Implemented in 3.2.0.
**Date**: 2026-10-27
**Decision makers**: operator (all decisions below), implementer (record only). Release gate: the full suite and the independent Verifier.
**Supersedes**: wherever they conflict, [ADR-0006](0006-scored-team-composition.md) (composition engine), [ADR-0007](0007-subscription-lanes.md)
(worktree lanes, gates G0-G7, second members), [ADR-0009](0009-setup-tools-how-team.md) decision 8, [ADR-0010](0010-setup-five-phases-channels-team.md)
(compose step), [ADR-0015](0015-per-cockpit-setup-openclaw-orchestrator.md) D2b, [ADR-0016](0016-delegated-auth-live-source-of-truth.md) D3,
[ADR-0017](0017-balanced-credit-policy.md) and [ADR-0018](0018-routing-matrix-and-pool-balancing.md) (waiver, acknowledgements, static weights).
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md).

## Context

3.1.0 left three overlapping systems: a scoring engine that composed teams, a worktree "lanes" ledger with eight evidence gates and
acknowledgements, and the 3.1.0 routing matrix. Roles were chosen in three places, agy was restricted to read-only reports by
machinery the operator no longer wants, nobody but `claude` could spawn the `kit-role-*` agents, and nothing measured what actually ran.

## Decision

### 1. Roles are the unit; one table
`scoring/routing.ROLES` (role -> primary -> fallback chain -> class) is the only source. `lanes.yaml`, the Claude Code agent
definitions (`agents/roles/*.md` `model:` block), the OpenClaw `kit-role-*` agents (`model.primary` + `model.fallbacks`) and the
setup tables are generated from it; `tests/test_matrix_equality.py` asserts every artifact equals the matrix (D2).

### 2. One primary, one fallback chain; fallback only on exhaustion
A role has ONE primary and ONE fallback chain. The fallback fires only when the pool is exhausted (quota/credit text via
`classify_quota`, or `pool_usage >= spill_at` = 0.85) or the model does not respond (timeout, 5xx, empty). It uses OpenClaw's
native `model.fallbacks`. Every fallback is recorded with its cause (`FALLBACK_CAUSES`); there is no silent fallback, and a missing
tool is the recorded cause `agy-missing` / `codex-missing`, never a quiet substitution. Second opinions, parallel second members,
candidates and display-only rows are gone (D2).

### 3. Checker independence is dynamic (C3)
The checker (verifier, security-auditor) uses the cheapest adequate model whose family is not one of the writer families recorded
in the run ledger for that run (`CHECKER_LADDER`: sonnet, opus, agy opus). Sonnet checks Gemini/Haiku-written work; Opus only as the
ceiling; no independent rung means `blocked` with cause `no-independent-checker`, never a weaker checker. **INFERRED evidence**:
the profile scores give Sonnet 59.6 vs Opus 55.4 on untagged security, so a static Opus requirement is not justified.

### 4. agy has the same privileges as claude (D3, O1)
Where the matrix assigns it, Antigravity reads and writes in the checkout like Claude Code. Removed: the "Gemini must not write"
rules, the Google acknowledgement, the Anthropic acknowledgement, the operator waiver, gates G0-G7 and their evidence guard,
`--lanes-antigravity`, held-lane reasons and agy admission. agy is on iff the matrix assigns the cell and `detection.agy_active()`
(binary found, Google account state present). The scope contract (`path_in_scope`) stays as advisory verification and logging
on the dispatch row, never a refusal. No worktrees: agy writes in the checkout.

### 5. The matrix (INFERRED; overridable per profile with a `routing:` block)

| Role | Primary | Fallback | Class |
|---|---|---|---|
| explore, doc-writer, package-upgrade | agy `gemini-3.8-flash-high` (AI Pro) | claude-code haiku (Max) | team-read / team-write |
| code-reviewer, tester, generalPurpose | agy `gemini-4-argon-high` (AI Pro) | claude-code sonnet (Max) | team-read / team-write |
| planner, software-architect | claude-code sonnet (Max) | agy `gemini-4-argon-high` | team-plan |
| implementer, crashlytics-fixer, sentry-fixer, terraform-maintainer, crossplane-upjet-maintainer | claude-code sonnet (Max) | agy `gemini-4-argon-high` | team-write |
| verifier, security-auditor | dynamic ladder (section 3) | n/a | team-check |
| main, channel-agents | anthropic-api `claude-sonnet-5-5` | anthropic-api `claude-haiku-5-5` | chat |
| utility, summarization, explore-bulk | gemini-api `gemini-3.8-flash` | anthropic-api `claude-haiku-5-5` | chat-light |
| heartbeat | anthropic-api `claude-haiku-5-5` | gemini-api `gemini-3.8-flash` | chat-light |

Pools: `claude-subscription`, `agy-gemini`, `agy-claude-gpt`, `anthropic-api`, `gemini-api`, and `codex`. Max-primary roles fall back
to agy, never to API credits (O4). The static role-count weights (claude 1.0 / agy 0.7) are gone; balance comes from assigning primaries
by role class and spilling at `spill_at` 0.85 from the live `pool_usage`. The heartbeat stays on Haiku until the acceptance case
`gemini.tool-call` proves Gemini tool-calling through OpenClaw. Codex is an optional pool: valid in the cell grammar, active only when
detected and authenticated (O3), in no default cell.

### 6. OpenClaw is the main orchestrator (D1, C1, C2)
Setup wires `allowAgents` so `main` and every channel agent can `sessions_spawn` every `kit-role-*` agent (recorded in state for
set-difference teardown). Spawn limits are raised, never lowered or capped (`maxSpawnDepth` 4, children 16, concurrent 32); instead there is
no collision: per-run ids and `.agent-output/<run>/<role>/` directories, one handoff per team, a flock-protected write-scope claim
(`scope_guard`, `scopes_overlap`) and atomic ledger appends; the MCP child reaper and `tools.deny bundle-mcp` stay. Sizing is by
measurement: on this host before `setup`, the gateway ran 384 MCP children and sat at MemoryCurrent ~11.6 GiB of MemoryHigh 12 GiB
(peak 12292 MiB), which is the overflow `ai-resources openclaw reconcile` owes. The by-model `kit-lane-*` agents are merged into the
`kit-role-*` agents; `migrate_kit_lanes` removes only agents setup created and that are unused, otherwise reports `kept: <reason>`.
`ai-resources agy run --role R` calls `agy -p` from a Claude Code session and returns the report contract when `sessions_spawn` is absent.

### 7. Chat plane (D4)
`main` and channel agents use Anthropic API credits (sonnet, fallback haiku); utility, summarization and bulk explore use Gemini API
credits (`gemini-3.8-flash`); the heartbeat stays Haiku until `gemini.tool-call` passes.

### 8. Measurement and proof (D7, D8, D10)
Every dispatch appends a row to `dispatches.jsonl` (role, pool, backend, model, tokens, duration, outcome, fallback cause, run id, writer
families). `doctor` compares actual against matrix per pool and role and alerts on a role that ran outside its primary without a
recorded cause, plus a weekly summary. The kit-orchestration sizing scorecard and `scores/` scorecards are wired to this ledger.
`ai-resources acceptance` (52 cases, 7 groups: route, fault, checker, spawn, gemini, agy, setup-live) resolves each role, makes one
real minimal call and asserts the model/pool that answered equals the matrix; `verify --live` is the setup-live subset of the same
registry. Cases that kill turns are listed as operator-run with their exact command.

### 9. Removed (counts from the phase results)
- Worktree `lanes` ledger: `lanes_cmd.py` (1491 lines), `orchestrator/worktree.py`, the `lanes` subcommand and the `backend: lane`
  workflow backend; `workflows/feature-lanes` and its skill (18 workflows remain, default backend `route`). Replaced by `scope_guard.py` (78 lines).
- Scoring engine: `compose`, `flow`, `explain`, `detect`, `gate`, `headroom`, `headroom_sources`, `profile_out`, `scoring/lanes.py` (519 lines);
  flags `--compose`, `--headroom`, `--headroom-live`, `--why-json`, `--allow-fable`; `docs/team-composition.md`.
- Gates G0-G7 (`agy_lane_gates`, `agy_gate_evidence`, `agy_evidence_guard` hook), Google and Anthropic acknowledgements and the waiver,
  `--lanes-antigravity`, `--lanes-orchestrator`, `--lanes-dry-run`, `lane_acks`, `LANE_API_FALLBACKS`, `lane_member: second`,
  display-only rows, `STATIC_WEIGHTS`, `gemini_tripwires`, `kit-lane-*` agents and `openclaw_lanes.py`.
- Size: P2b-a 67 files +507/-4440; P2b-b 91 files +676/-8494 (35 files deleted); the targeted suite fell from 10568 to 7639 tests because the
  tests of the removed concepts were deleted with them.

### 10. Kept (D9)
The credits ledger and `key_check` env repair, the hooks (StopFailure quota signal, narration, `kit_sleep_guard` one-blocking-wait),
`openclaw reconcile`, off-box credential regeneration, the MCP child reaper, `scores/` + `scorecard.py`, `quota_signal.py`.

## Unverified / only a real run proves
Measured on this host before `ai-resources setup` 3.2.0; the owner's real run must prove, then `ai-resources acceptance` end with no FAIL:
- `ai-resources setup` applied on the live gateway (the drained restart clearing `plugin-fresh` and `mcp-child-cap`).
- The `allowAgents` patch applying through `openclaw config patch`, and a kit-role spawn from `main` (`spawn.allow`, `spawn.explore-run`, `spawn.from-main`).
- Native `model.fallbacks` firing under the `claude-cli` runtime pin (needs a forced provider failure on the real gateway).
- SIGTERM and watchdog behaviour (operator-run cases).
- Gemini tool-calling through OpenClaw: `gemini.tool-call` is SKIP here (`Secret store entry "OPENROUTER_API_KEY" was not found`); no verdict written.
- MCP load under many concurrent teams (C1 sizing is a sample, not a limit).

## Consequences
One place to change a route; setup shows one table; the ledger makes drift visible. Cost: the evidence-gated lane model and its
tests are gone for good, and a reviewer must trust the matrix (INFERRED, not benchmarked per role) until the ledger accumulates data.
