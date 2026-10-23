# Validating cost

`ai-resources audit` prices tokens with public list prices. That is an **API-equivalent estimate**: it lags billing and it
cannot see what a subscription charges you. This page says how to check the figures against the places that know.

## What the kit measures

| Command | What it shows |
|---|---|
| `ai-resources audit [--all-projects] [--subagents] [--openclaw]` | Claude Code and OpenClaw usage by model, session and agent, the poll-turn share, Haiku calls above the 100k tier, the global CLAUDE.md weight, billing mode and quota, and the **effort validation** table. |
| `ai-resources audit --json` | The same as one JSON document: `all.by_origin`, `all.by_provider`, `all.by_role`, `effort_validation`. |
| `ai-resources audit snapshot <label> [--since ISO --until ISO]` | A window of usage as a file under `~/.local/state/ai-resources/cost-baselines/`. |
| `ai-resources audit compare <a> <b>` | The delta between two snapshots, per scope, plus the breakdowns by origin, provider and role. A snapshot taken before round 3 has no breakdowns and compares as `n/a`. |
| `ai-resources orchestrate cost <run>` | The API-equivalent cost of one orchestrated run, per role (OpenClaw transcripts only). It reports; nothing is capped or paused. |

Role attribution: a Claude Code subagent's role is the `agentType` in the `.meta.json` beside its transcript (`subagent` when
there is none); an OpenClaw `kit-role-<role>` agent maps back to its role; everything else keeps its agent id. Provider comes
from the event, else from the model name.

## Checking the figures

1. **Anthropic Console (API keys).** Open Usage and Cost, filter by the key and the same window as the snapshot. The Console
   figure is what you were billed; compare it with `all.by_provider.anthropic.usd`. Expect the audit to be a little
   different: it prices the cache write tiers it can see and ignores credits and discounts.
2. **GCP billing (Vertex AI, Gemini API).** Billing > Reports, filter by service "Vertex AI" or "Generative Language API" and
   the project. Compare it with `by_provider.google` (and `google-vertex`). Vertex prices have a regional and a global rate;
   the audit uses the global one.
3. **Subscription quota (Claude Code, Antigravity, Codex).** A subscription is not billed per token. The audit's USD for those
   calls is what the same tokens would cost on the API; it is a **relative** measure (which role or provider uses the most),
   not money you owe. The real limit is the quota: `ai-resources doctor` and `audit` show the 5 hour and weekly percentages
   Claude Code reports, and the kit alerts when the weekly quota is exhausted or credit is low.
4. **Same window, same scope.** Compare one provider at a time, with `--since/--until` on the snapshot matching the console's
   date range and timezone (the kit uses UTC).

## Reading the effort table

`audit` prints, per role, the effort the profile asks for, the calls and the output tokens per call. A role set to `high`
should write more per call than one set to `low`. If the order does not follow the setting, the effort is not reaching the
model: check the subagent file (`effort:` in `~/.claude/agents/<role>.md`) or the agent's `thinkingDefault` in
`openclaw.json`. Too few calls make the average meaningless; look at a week, not an hour.

## Known gaps

- Events without a price row (a new model) are counted as unpriced, never guessed.
- Subscription lanes under-count dollars by design and over-count nothing.
- OpenClaw transcripts that cannot be decoded (no zstd decoder) are counted as "undecoded rows" and left out.
