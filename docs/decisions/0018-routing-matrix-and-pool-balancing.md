# ADR-0018: One routing matrix, four pools balanced, Antigravity and Gemini API on by default

**Status**: Accepted (2026-10-27, operator decisions, final). Implemented in `scoring/routing.py`, rendered into `lanes.yaml`
under `routing:`, measured by `pool_usage.py` and shown by `ai-resources doctor`.
**Date**: 2026-10-27
**Decision makers**: operator (all decisions below), implementer (record only). The independent Verifier has not signed off.
**Supersedes**: the "off by default / opt-in / gates G0-G7 decide admission" parts of [ADR-0007](0007-subscription-lanes.md)
section 11 and of [ADR-0009](0009-setup-tools-how-team.md) decision 8; the "SAFE subset only" scope of
[ADR-0017](0017-balanced-credit-policy.md) (the policy order is kept, the routing is now this matrix); the "Antigravity is never
pre-ticked / a recommended run never signs the Google acknowledgement" parts of [ADR-0016](0016-delegated-auth-live-source-of-truth.md)
(its D1 to D3 auth rules are unchanged).
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md). Nothing here restarts the gateway;
the Gemini key drift below is reported, and fixed by an operator-run change that takes effect at the next planned restart.

> **Superseded by ADR-0019:** the 'Operator waiver' section, the Google/Anthropic acknowledgements, `--lanes-antigravity`, 'Writers stay on Claude Code', the secondary-per-cell matrix (now one primary + one fallback chain, dynamic checkers), static role-count weights and class pools as the only balancing (primaries are assigned by role class, spill at 0.85 stays).

## Context

The operator pays for four resources and wants all four used, in proportion, without losing performance:

| Pool id | Resource | Cost model |
|---|---|---|
| `claude-subscription` | Claude Code on Claude Max 20x | flat, quota windows (5 h, 7 d) |
| `agy-gemini`, `agy-claude-gpt` | Antigravity on Google AI Pro | flat, weekly quota per model family |
| `anthropic-api` | Anthropic API credits | prepaid, $200/month |
| `gemini-api` | Gemini API (Google Cloud credit) | prepaid, ~$10/month |

Before this ADR the kit routed per role through lane candidates (a tie-break order, not a matrix), the agy lane was "off by default
behind eight gates", chat agents were hand-edited in `~/.openclaw`, and nothing measured how much of each pool was used. Balanced
named a profile; it did not distribute work.

## Operator waiver

The operator (bitgandtter@wildbit.dev) waives the Antigravity lane gates **G1..G7** (and G0, the phase 2 merge gate) as admission
preconditions, and waives the "off by default" state of the lane, on 2026-10-27. Antigravity and the Gemini API are enabled now.
This extends the narrower waiver of 2026-10-24 recorded in `agy_lane_gates.py`. What the waiver does **not** change, and what the
code keeps:

- The Google acknowledgement stays. It is one acknowledgement per account, stored with the text hash, and still gates a role. The
  recommended run (`--auto`) records it for the account already on file, or for `bitgandtter@wildbit.dev`, because the operator has
  already given it here; it is printed, never silent. A text change still invalidates it.
- A waived gate is not a proven gate. `agy_lane_gates.gate_proven` and `status_table` still read the literals and the evidence
  record. G5 (the kit-owned agy home) stays off: agy keeps the operator's real login. The sandbox (G2) and tool-surface (G3) risks in
  ADR-0007 section 11 items 5 and 6 are still true and are now accepted by the operator, not mitigated.
- Writers stay on Claude Code. The routing matrix carries no agy writer.
- Checker independence by model family (ADR-0007 section 8) is unchanged.
- `--no-lanes-antigravity` still turns the lane off, and `--lanes-antigravity` still works. With neither flag the recommended run
  turns it on when `agy` is installed and `cli_team` is set. Without `agy` installed nothing is written.

## Decision

### 1. One routing matrix (role -> primary -> secondary)

The matrix lives in `scoring/routing.py` (`TEAM_MATRIX`, `CHAT_MATRIX`). A profile may override single cells with a top-level
`routing:` block (`routing.<plane>.<role>.{primary,secondary}`, same cell grammar), and setup copies the override into the lanes
state. Setup renders the merged result into `~/.config/ai-resources/lanes.yaml` under `routing:` (matrix with each cell's pool, the
classes and the spill rule) and the digest covers it. A cell is `backend[:model]`; backends are `claude-code`, `claude-cli`, `agy`,
`anthropic-api`, `gemini-api`.

**Team plane** (kit roles; Claude Code on Max and Antigravity on AI Pro are both PRIMARY routes):

| Role | Primary | Secondary | Class |
|---|---|---|---|
| explore | agy `gemini-3.8-flash-high` (AI Pro) | claude-cli haiku (Max) | team-read |
| code-reviewer | agy `gemini-4-argon-high` (AI Pro) | claude-code sonnet (Max) | team-read |
| planner | claude-code sonnet (Max) | agy `gemini-4-argon-high` | team-plan |
| software-architect | claude-code sonnet (Max) | agy `gemini-4-argon-high` | team-plan |
| implementer, generalPurpose | claude-code sonnet (Max) | claude-cli sonnet | team-write |
| tester, doc-writer, package-upgrade | claude-code haiku (Max) | claude-cli haiku | team-write |
| verifier, security-auditor | claude-code opus (Max) | agy `claude-opus-5-5` | team-check |

**Chat plane** (OpenClaw agents and utility work; API credits):

| Work | Primary | Secondary | Class |
|---|---|---|---|
| `main`, channel agents | `anthropic/claude-sonnet-5-5` (Anthropic credits) | `claude-haiku-5-5` | chat |
| utility (titles), summarization, bulk explore | `google/gemini-3.8-flash` (Gemini credits) | `claude-haiku-5-5` | chat-light |
| heartbeat | `claude-haiku-5-5` (gated) | `gemini-3.8-flash` | chat-light |

The heartbeat stays on Haiku until Gemini tool-calling through OpenClaw is proven (see Unverified).

### 2. Consumption-balancing policy

Each class of work drains an ordered list of pools; the first pool under `spill_at` (0.85) serves it. An unknown reading counts as
available (no reading is not evidence of exhaustion). When every pool of a class is hot, the least used serves it.

| Class | Pools, in order | Why |
|---|---|---|
| chat | anthropic-api | owner decision: chat agents use Anthropic credits as primary |
| chat-light | gemini-api, anthropic-api | cheap, tool-light, high volume goes to the Google credit |
| team-read | agy-gemini, claude-subscription | reading-heavy work drains AI Pro first, saving Max |
| team-plan | claude-subscription, agy-gemini | judgment on Max; Gemini second opinion on AI Pro |
| team-write | claude-subscription | writers stay on Claude Code |
| team-check | claude-subscription, agy-claude-gpt | opus checkers on Max, same-model agy as the availability fallback |

`routing.choose_pool(class, usage)` is the rule. Balanced keeps its name and its model table; what changed is that its lanes now
actually distribute across the four pools through this table. Evidence label: INFERRED. The thresholds and the class map are design
choices, not measurements; they move only by a measured before/after (ADR-0013 rule).

### 3. Ideas adopted from gentle-ai and the vfarcic proposals

Studied in `docs/research/gentle-ai` (gentle-ai deliberately does not route models in prompts: "model assignment is client-specific
... not a formal development-phase matrix"). Adopted where it beats the previous design:

- **A role -> model matrix as configuration, not prose** (gentle-ai's Configure Models screen): the matrix is data, rendered by
  setup, and overridable per profile.
- **Per-phase routing as classes**: read, plan, write and check phases drain different pools, which is what the SDD phase split buys.
- **Negative routing rules**: a writer's model never adds a checker (kept from ADR-0007 section 8; `routing` never moves a checker
  onto a writer's family).
- **Token budgets as a routing constraint**: `routing.TOKEN_BUDGETS` records the per-class prompt budget (100k for the Haiku-priced
  read and light classes, ADR-0014 D1) next to the matrix.
- **Delegation by size**: the size classes of the research plan decide whether a task is delegated at all; the matrix only decides
  where a delegated task runs.

Not adopted: gentle-ai's delivery-strategy matrix (conflicts with `merge_to_base`), and the capable/small prompt variants (G13, skipped).

### 4. Live measurement

`pool_usage.py` reads, read-only and tolerant: Max from the status-line sidecar and quota block, AI Pro from the kit counters, and
the two credit pools from `~/.cache/ai-resources/api-spend.json` against `ANTHROPIC_API_CREDITS_USD` (default 200) and
`GOOGLE_API_CREDITS_USD` (default 10). A pool with no feed reads `unknown`, never zero. Doctor sub-check 4g2 prints one row per
pool, flags 80% and prints where each class would run now. It never prints a key value; it reports only whether a key is set in the
process environment. **No writer of `api-spend.json` exists yet**: the two credit pools read `unknown` until one does (see
Unverified).

### 5. Setup applies it by default

Under the recommended run (`--auto`, the default) setup asks nothing about it: the Antigravity lane turns on when `agy` is
installed (the Google acknowledgement is recorded under the waiver above), the Claude parallel runs are kept only where an Anthropic
acknowledgement is already on file, and `lanes.yaml` is rendered with `routing:`. `tests/test_routing_auto_agy.py` pins this with
simulated state.

## Live verification (2026-10-27, this host, no key printed)

| Pool | Call | Result |
|---|---|---|
| Claude Code (Max) | `claude -p 'reply ok' --model haiku` | `ok`, 972 ms API time (4.4 s wall), model `claude-haiku-5-5`, 2 in / 4 out tokens, subtype success |
| Antigravity (AI Pro) | `agy -p 'reply ok' --model gemini-3.8-flash-low --output-format json` | `SUCCESS`, `ok`, 2.05 s turn (6.0 s wall), 10,767 in / 1 out tokens |
| Gemini API | `openclaw infer model run --model google/gemini-3.8-flash` (profile key) | HTTP 200, 1.4 to 3.4 s, text `ok` |
| Anthropic API | `openclaw infer model run --model anthropic/claude-haiku-5-5` (profile key) | provider transport log: `status=200 elapsedMs=1004`, text `Ok`, ~8.6 s wall including CLI start |

**Finding: the Gemini key drift is real and one key is dead.** `GEMINI_API_KEY` (the shell and `~/.openclaw/gateway.systemd.env`,
identical) returns **HTTP 401 UNAUTHENTICATED "API key not valid"** from `generativelanguage.googleapis.com` (header and query
forms), while `google:default` works: `openclaw infer` succeeds with the env key unset and with the env key set to garbage, so
OpenClaw uses the profile for its own calls. Anything that reads the env key directly (a child process, the image generator, a
script) fails. ADR-0017 had recorded the two keys as different; this run shows which one is funded. Fix (operator-run, no restart
now): put the `google:default` key into `gateway.systemd.env` as `GEMINI_API_KEY` and pick it up at the next planned gateway restart.

## Consequences

- The agy lane is a primary route for explore and code-reviewer, so Max quota is spent on judgment and writing, not reading.
- The quality risk is Gemini on report roles; the checkers and writers are unchanged. The Google egress of ADR-0007 section 11 item 4
  now applies by default to explore and code-reviewer content.
- Superseded text is marked in the four earlier ADRs; the gate machinery stays as the record of what is waived, not as a switch.
- Kept, not deleted: `agy_lane_gates`, `agy_gate_evidence` and their tests. They still back `lanes gates`, the ledger fields and doctor,
  and the waiver is an input to them; deleting them would drop the record of what the operator accepted.

## Unverified

- **Anthropic API direct call.** Proven by the 3.2.0 acceptance route cases (2026-10-27): `main`, `channel-agents` and `heartbeat`
  call `anthropic/claude-sonnet-5-5` and `claude-haiku-5-5` through `openclaw infer model run`, and the wire `api=anthropic-messages`
  line of the transport log (not a CLI runtime) decides the pool. The call still uses the profile key in OpenClaw's auth store, not an env key.
- **Gemini tool-calling reliability through OpenClaw.** Still open. The acceptance case `gemini.tool-call` could not run on this host
  (`Secret store entry "OPENROUTER_API_KEY" was not found`), so it is SKIP and writes no verdict; the heartbeat stays on Haiku and the finops
  cron on the default model (ADR-0017: 189 s and ~10x output tokens on Gemini) until it passes.
- **API credit spend.** A spend feed exists now: the `credits` ledger (`ai-resources credits collect`) and the `pool_usage` credits entry
  feed doctor, so the 80% alert can fire once a pool has an opening credit and recorded spend. Rows stay `unknown` until then.
- **Whether the Google Cloud credit applies to the Gemini API** (ADR-0017 unknown, unchanged), and whether unused Anthropic credit expires.
