# Scored team composition

`ai-resources setup` can propose a team (a model per role) from a dated scores database and explain every
choice. It is opt-in: a setup run without the new flags and without picking "compose" in step 6 produces the
same `executors.yaml` as before. Decisions and rationale: [ADR-0006](decisions/0006-scored-team-composition.md).
The database and its schema: [`scores/README.md`](../scores/README.md).

## Modes

| Mode | Maps to selection shape | What it does |
|---|---|---|
| `uniform` | `single` | One model for every role. (Not `single-model`, which is the claude-native setup mode.) |
| `single-provider` | `single-provider` | A model per role, all from one provider. |
| `mixed` | `multi-provider` | Balances pools across providers within each role's tolerance band, without losing performance. |

```sh
ai-resources setup --compose uniform
ai-resources setup --compose mixed --allow-unverified \
    --headroom claude-subscription=scarce,openai-api=ample --why-json ~/.cache/ai-resources/why.json
```

In single-model mode (no gateway) only `--compose uniform` is offered, and the team is stored as Claude aliases.

## Mixed mode is unverified today

Under the shipped compat matrix Anthropic is the only provider with a verified cell that can serve kit roles
(`tests/test_composition_matrix_facts.py` pins it). Every cross-provider cell is `not verified yet`. So mixed mode
is offered only with `--allow-unverified`, refused with a message otherwise, and every non-Anthropic assignment is
marked UNVERIFIED in the why table, the wizard summary and the stored provenance. When a cross-provider cell is
verified on purpose, that test changes and mixed teams can become verified. Real balancing will mostly happen
among Anthropic models until then.

## Evidence labels, policy and tolerance

Scores carry a dated label: `MEASURED-HERE` > `MEASURED-3P` > `VENDOR` > `INFERRED` > `UNKNOWN`; a composed score
carries the weakest contributing label. **Importance, tolerance classes and load weights are policy, not evidence.**
Tolerance classes: critical 0.3 (planner, implementer, software-architect, security-auditor), standard 0.5
(code-reviewer and verifier, generalPurpose, the maintainers, tester), low 1.0 (package-upgrade, doc-writer,
explore). A pick is inside `score >= S* - tolerance`, where `S*` is the best verified score.

Be clear about what the seed data is: the only cross-provider measured table is Vals Terminal-Bench 4.0
(2026-10-13), a role-agnostic proxy. Code roles use `accuracy / 10` and are labelled INFERRED. No independent
benchmark exists for planning, review, verification or security, so those rows are INFERRED placeholders from the
same proxy and say so. The operator's earlier example numbers are never seed data.

## Headroom and billing pools

Pools are keyed by billing class: **subscription** (Claude plan, Codex plan, Antigravity), **metered-api**
(Anthropic API, Vertex, Gemini API, OpenAI API, DeepSeek) and **gateway-metered** (OpenRouter). Under a gateway,
balancing moves metered money, not subscription quota. The engine balances with a per-pool weight:

- the static level you give in setup (`ample` 1.0, `normal` 0.7, `scarce` 0.3; default 1.0), or
- when you opt into a live read and it succeeds, that weight scaled down by the used fraction.

A live reading can only change which band member is picked. It never widens a band or admits a candidate. When a
reading is `unknown` or `stale` the static level is used, and the why table says so.

| Pool | Live source (opt-in) | Notes |
|---|---|---|
| Claude subscription | an **existing** status-line JSON sidecar (`rate_limits.five_hour` / `seven_day`) | **Live today.** Pro and Max only; plan-wide across models; present only after the first response. The kit installs nothing and never edits your status line or `~/.claude`. Default path `~/.cache/ai-resources/claude-rate-limits.json` (override with `AI_RESOURCES_CLAUDE_SIDECAR`). |
| OpenRouter | `GET /api/v1/key` (`limit`, `limit_remaining`) | **Not live today:** the reader exists but the kit ships no fetcher, so setup does not offer the opt-in and the static level is used. A null limit would be unlimited credit: unknown. Credit counters only, not upstream throttles. |
| Vertex | Cloud Quotas / Monitoring | **Not live today:** no fetcher is shipped, so the static level is used. Google warns the quota-page usage may be inaccurate. |
| Anthropic API, Gemini API, OpenAI API, DeepSeek | none at setup | Response headers exist only at runtime. |
| Antigravity, Codex | none | No readable quota: always unknown. |

Claude pool facts that shape the advice: no numeric allowance or per-model weight is published (the tiers are
qualitative: Haiku low, Sonnet moderate, Opus high, Fable very high); `claude -p` draws the same plan limits as
interactive work; usage credits change the prompt-cache TTL from 1 hour to 5 minutes; an Anthropic API org spend
cap returns 429 `enforced_spend_limit_reached` with no `retry-after` (a hard stop until 00:00 UTC next month;
`CLAUDE_CODE_RETRY_WATCHDOG=1` fails fast on it). Fable 5.1 is an opt-in candidate (`--allow-fable`) only on a
declared Max or Premium seat or API billing: it runs on usage credits on Pro and Team Standard, and `claude -p`
bills it without asking.

## Antigravity is direct use only

The Antigravity FAQ says third-party tools (Claude Code, OpenClaw and OpenCode are named) that access Antigravity
violate its terms of service, and `agy -p` is for direct scripting and CI. Reported suspensions cover Antigravity,
Gemini CLI and Code Assist on the account. The kit therefore never proposes `agy` or the OpenClaw antigravity
engine as a composition target, an `openclaw-agent` or a dispatch backend, and no flag lifts that. Its pools are
listed for reporting only. (Gemini CLI also stopped serving consumer accounts on 2026-06-18: Google models are
reached through the API, Vertex or a gateway.)

## Codex

Codex is detected as a cockpit and as a provider route, and the kit cannot dispatch to it: the Codex cockpit is
`instructions only` in all 21 compat cells, and there is no `codex exec` dispatch path. `CODEX_API_KEY` is the CI
path (it bills API rates, pool `openai-api`); whether ChatGPT-auth headless runs count like interactive use is
unknown. `gpt-6.1-sol`, `gpt-6-sol` and `gpt-6-luna` are in the openai known-models list, but the why table shows
the "all OpenAI via Codex" reference as blocked.

## The why table

One row per role: importance, tolerance class, band, chosen model with route and effort, score, label, date, price
in/out with its validity window (and a `[verify]` marker when a single unconfirmed listing is the only source),
pool and billing class, headroom (state, source, used percent, `resets_at`, or "static weight"), gate
(verified / UNVERIFIED), usage (dispatch-ok / direct-only / instructions-only), the runner-up and why it lost, and
flags. The footer gives the weighted loss, dataset version, input digest, and says when the team is unverified
or when headroom was unknown. Reference profiles (all-gemini, measured-best, quality-first, cost-optimized and
"all OpenAI via Codex") are scored with their gate status and are never chosen automatically. `--why-json PATH`
writes the same document as JSON (the path must be under the workspace or `~/.cache`; `/tmp` is refused).

A stored profile entry names a model, never a route. A model that has candidates on several routes (for example
`anthropic-direct` and `vertex-global`) therefore keeps one route across the whole team, and the others show up
as `route-collapsed` rejections. The route, pool and mark shown for a role are the ones of that route.

Per-token price is not cost per task: on the Vals board Sonnet 5.5 costs half of Opus 5.5 per token but more per
test ($16.51 vs $13.20). The table shows prices; it does not turn them into a decision.

## Overrides and refresh

`~/.config/ai-resources/model-scores.yaml` holds your override rows (each needs `reason` and `as_of`). They shadow
shipped rows by key and are flagged. Reports:

```sh
ai-resources models scores show      # what the database holds (read-only, no network)
ai-resources models scores check     # stale rows, closing price windows, unconfirmed prices, unroutable models; exit 1 if any
ai-resources models scores diff      # your overrides against the shipped rows
ai-resources models scores refresh --from proposals.yaml   # proposals; nothing is auto-approved
```

## Gateway hazards on the 5.5 ids

Forced `tool_choice` returns HTTP 400 on Opus 5.5, Sonnet 5.5 and Fable 5.1; non-default `temperature`, `top_p` or
`top_k` returns 400 on Sonnet 5.5 and Haiku 5.5; thinking cannot be disabled; thinking blocks are bound to the
producing model, so a cross-vendor fallback restarts reasoning. Composition therefore writes same-vendor fallbacks
only, and never `backend` or `agentId` (ADR-0005).

## Phase B

Live evaluation (a bounded, budgeted harness that would turn review, verification and planning rows into
MEASURED-HERE evidence) is a separate plan and approval: it needs `--budget-usd`, `--max-tokens` and
`--approved-by`, never runs `agy`, and its output is a candidate-row file for your review. Phase A spends nothing.
