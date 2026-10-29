# ADR-0017: Balanced credit consumption policy

**Status**: Accepted (2026-10-25, operator defaults: the SAFE subset only). Applied to the live host with `openclaw config patch`
(hot reload, no restart) and encoded in the kit; the heartbeat and the finops job are deliberately NOT moved.
Companion patch: [0017-balanced-credit-policy.patch.json5](0017-balanced-credit-policy.patch.json5).
**Superseded in routing by [ADR-0018](0018-routing-matrix-and-pool-balancing.md)** (2026-10-27): the order of spend and the verified facts stay; the "SAFE subset only" scope, "agent main and all channel agents stay on Anthropic" as the only chat rule, and the workload map are replaced by the routing matrix and the class-to-pool balancing table. The Gemini key finding there ("env key differs") is now resolved: the env key is the dead one (401), `google:default` is the funded one.
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (no live gateway restart).

> **Superseded by ADR-0019:** the `kit-lane-agy-*` fallback entry in the companion patch and the 'Workload map: Gemini candidates' (candidates are not a concept any more); the order of spend and the verified facts stand.

> **Amended by Track 4 (2026-10-29)**: the chat plane moved to Gemini API. `main`, `channel-agents` and `heartbeat` are
> `gemini-api:gemini-3.8-flash` with the Anthropic API as fallback (`claude-sonnet-5-5`; `claude-haiku-5-5` for heartbeat). The
> heartbeat no longer stays on Haiku, and Anthropic API carries an advisory USD 10/day cap. The current matrix is
> `docs/routing.md`; statements below about the chat plane on Anthropic, or the heartbeat staying on Haiku, are historical.

## Goal

Spend each paid resource in the right order so no prepaid credit is wasted and out-of-pocket
spend is the last resort, with the policy reproducible from the kit rather than hand-edited in
`~/.openclaw`.

## Resources

| Resource | Cost | Used by | Status |
|---|---|---|---|
| Claude Max 20x | $200/mo, cycle day 27 | `kit-lane-cli-*` (Claude Code) | owner-confirmed |
| Anthropic API credits | $200/mo | provider `anthropic`: main, channel agents, roles | expiry of unused credit: **unknown** |
| Google One AI Pro | $19.99/mo, billed day 1 | Antigravity quota, `kit-lane-agy-*` | weekly quota exhausted on gemini lanes |
| Google Cloud credit | ~$10/mo, billing account 0111DB-02B54A-CB17A5 | Gemini API key (`google:default`) | applies to Gemini API: **unverified** |

## Policy (order of spend)

1. Subscriptions first: CLI lanes on Claude Code, agy lanes on AI Pro quota.
2. API credits second. Anthropic for main, reasoning and code. Google credit for cheap,
   high-volume, tool-light work on `google/gemini-3.8-flash`.
3. Out-of-pocket last, with a fallback chain and an alert at 80% of each credit.
4. `google/gemini-3.8-flash` is the API fallback for the `gemini38flashhigh` agy lane when
   Antigravity quota is 0%.
5. Agent `main` and all channel agents stay on Anthropic (unchanged).

## Verified facts (this session, read-only)

- `google:default` exists as an api-key profile; `anthropic:default` and `openrouter:default` too.
- The exported `GEMINI_API_KEY` env value is **different** from the one stored in `google:default`
  (the env wins for the `google` provider per `models status`: `source=env: GEMINI_API_KEY`).
  So the env key, not the loaded profile, may be what is billed. Needs owner decision (below).
- No cron job is model-driven except three `agentTurn` jobs. Everything else is a shell
  `command` payload (usage reports, finops data, security scans) and costs no tokens.
- Google is used today only for image generation (`google/gemini-3.1-flash-image`).
- `ai-usage-report.sh` lives in `~/.openclaw/workspace/scripts/ai-usage/`, **not in the kit**.
  It reads `CLAUDE_BILLING_DAY` only. `SUB_CLAUDE_USD`, `SUB_AGY_USD`,
  `ANTHROPIC_API_CREDITS_USD`, `GOOGLE_API_CREDITS_USD` and a Credits column do **not** exist yet.

## Workload map: Gemini candidates

| Workload | Today | Proposal | Quality risk | Tool-use risk | Verdict |
|---|---|---|---|---|---|
| `agents.defaults.utilityModel` (titles, short summaries) | haiku-5-5 | gemini-3.8-flash | low | none (no tools) | **Move** |
| `agents.defaults.heartbeat.model` (main, 2h, light context, isolated) | haiku-5-5 | gemini-3.8-flash | low | medium: heartbeat may call tools; unproven with Gemini tool schemas in OpenClaw | **Move, verify one run** |
| Lane `kit-lane-agy-gemini38flashhigh` fallback | haiku-5-5 | gemini-3.8-flash then haiku | low (same model family as the lane) | low | **Move** |
| Cron `finops-daily-cost-report` (agentTurn, Spanish, posts via message tool) | default (sonnet) | gemini-3.8-flash | medium: Spanish prose and number fidelity | medium: needs the Slack message tool | **Candidate, test first** |
| Cron `security-scan-weekly-deep-report` (disabled, haiku-4-5) | haiku-4-5 | leave | low | low | Leave (disabled) |
| Cron `security-scan-weekly-deep` (agentTurn, long agentic, devops) | sonnet | stay Anthropic | high | high | **Do not move** |
| Memory Dreaming Promotion (memory-core agentTurn) | default | stay | medium | medium (internal tool contract) | Do not move |
| Channel agents (snoutzone, elinvo, devops, ai, security) | sonnet | stay | high | high | **Do not move** |
| `main`, subagents, `kit-role-*` | anthropic | stay | high | high | **Do not move** |

Expected Google spend is small (heartbeat 12 runs/day plus utility calls), so the ~$10/mo credit is
unlikely to be fully consumed by these alone. Honest consequence: moving more would be needed to
"use" the whole credit; that is a quality tradeoff the owner should choose, not the default.

## Decision: the safe subset (see patch file)

1. `agents.defaults.utilityModel` -> `google/gemini-3.8-flash`
2. `agents.defaults.models["google/gemini-3.8-flash"] = { alias: "flash" }`
3. `agents.entries["kit-lane-agy-gemini38flashhigh"].model.fallbacks` ->
   `["google/gemini-3.8-flash", "anthropic/claude-haiku-5-5"]` (array replaces whole)

Not moved: the heartbeat stays on Haiku until one verified Gemini tool-calling run exists.

Finops test (2026-10-25): `finops-daily-cost-report` ran once on `google/gemini-3.8-flash`. It delivered through the message
tool without a fallback, but took 189 s against 30 s on Sonnet and used about 10x the output tokens (24k against 2.5k), and
number fidelity could not be checked from the host. It was reverted to the default model; revisit with a cheaper prompt.

Dry-run and apply: `ok: true`, schema and SecretRef resolvability pass, 3 operations applied, no restart.

## Reproducibility (implemented)

- `openclaw_cost_migrations.balanced_credit_proposals` proposes the utility model, the `flash` alias and the lane API
  fallbacks, each as an `old -> new` confirm in setup (ids `credit-*`, recorded once applied, `keep`/`force` honoured). Only on a
  host that already routes to a `google/*` model. The lane chain lives in `setup/lanes_render.LANE_API_FALLBACKS`.
- `templates/ai-usage/credit_alert.py` is the 80% credit alert (`AI_USAGE_CREDIT_ALERT_PCT` overrides the threshold). The
  workspace `ai-usage-report.sh` / `render_compact.py` already read `SUB_CLAUDE_USD`, `CLAUDE_BILLING_DAY`, `SUB_AGY_USD`,
  `ANTHROPIC_API_CREDITS_USD`, `GOOGLE_API_CREDITS_USD` and print the Credits column; the alert is copied next to them.
- Env defaults: `SUB_CLAUDE_USD=200`, `CLAUDE_BILLING_DAY=27`, `SUB_AGY_USD=19.99`,
  `ANTHROPIC_API_CREDITS_USD=200`, `GOOGLE_API_CREDITS_USD=10` (set to 0 if the credit does not apply).

## Verification (2026-10-25)

`openclaw infer model run --model google/gemini-3.8-flash` answered (HTTP 200) both as a plain call and through the lane
agent; the usage report prints the Credits column and the 80% alert fired in a forced run (credit lowered to $40, 90% used).

## Unknowns

- Whether the Google Cloud credit applies to Gemini API usage, and whether the project is on Paid tier.
- Whether unused Anthropic credit expires.
- Which Gemini key is billed (env vs `google:default`), and whether the agy CLI sees the old key.
- Gemini tool-calling reliability through OpenClaw for the heartbeat; finops delivered but was slow and verbose.
- Per-token pricing for `gemini-3.8-flash` (not in the kit catalogue; 3.7-flash is listed at 0.75/3.75 on OpenRouter).
