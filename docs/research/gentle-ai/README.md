# gentle-ai study (ODD, context economy, sizing, adoption plan)

Research only. Nothing in the kit was changed except these documents. Date: 2026-10-25.

| Doc | What it answers |
|---|---|
| [01 ODD deep dive](01-odd-workflow-deep-dive.md) | Exact ODD rules: the 7-step protocol, resume-test classification, six risk items, feature-doc format (`S#`, Tasks, `L#` Log), handoff by reference, delegation triggers and their numbers, verification, tests, commits |
| [02 Token and context economy](02-token-context-economy.md) | Every cost mechanism in gentle-ai vs ours, with measured prompt sizes and spawn counts |
| [03 Size and effort estimation](03-size-effort-estimation.md) | How gentle-ai classifies (it does not estimate), our gap, and a proposed planner rubric with a Sizing block and calibration loop |
| [04 Gap analysis and adoption plan](04-gap-analysis-and-adoption-plan.md) | 21 gaps with adopt/adapt/skip, concrete kit changes, risks (managed AGENTS.md block, Engram, generated skills, handoff contract), effort and ordering, decisions needed |

## Source and method

- Upstream: `Gentleman-Programming/gentle-ai` at `5ee1ac7` (main, 2026-10-25), shallow-cloned into a scratch directory outside any repo. Not installed, installer not run. A temporary Go test file written into the scratch clone to measure rendered size could not build offline and was removed (clone left clean).
- Ours: ai-resources 2.8.4 at `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec`, and the working tree this branch belongs to (`origin/main` = `ac89fe9`).
- Evidence labels: **[V]** read in code/docs with a citation, **[M]** measured with a command, **[I]** inferred. Estimates are arithmetic on measurements, never measured savings.
- Gentle-ai path shorthands: `routing.go` = `internal/components/agentguidance/routing.go`; `odd-orchestrator-sections.md` = `internal/assets/skills/_shared/odd-orchestrator-sections.md`; `manifest.go` = `internal/agents/capabilitymanifest/manifest.go`; `protocol.md` = `internal/assets/engram/protocol.md`.

## Executive summary

1. **Take the rules, not the prompt.** Gentle-ai's OpenClaw instruction file is 55,211 B (capable) / 35,911 B (small); our whole managed block is 11,087 B.
2. **Its savings are behavioral:** classify with the resume test first; zero subagents for small work; delegate only for a named reason (parallel independent units > ~100 lines, or context pressure); hand off a locator + task + spec ids, never a paraphrase; verify by risk with one independent verifier at the end; the verifier runs the spec's authorized examples in a scratch copy and returns a verdict per `S#`.
3. **Our gap is the missing cheap path.** `/kit-plan` always spends 9 agent calls and `/kit-implement` at least 6 (up to 27): >= 15 per change, even when an agent could finish and verify it inline. No sizing exists in planner/architect/explore-and-plan.
4. **Cheapest first move:** split the 14,070 B (65%) of `kit-orchestration` used by only 2 of 19 workflows into an on-demand reference, and add short Task size / Delegation / Authorize sections. Docs-only, no new keys.
5. **Inconsistency found in our own kit:** `agents/roles/verifier.md` forbids running tests, while `kit-implement.js` tells the sign-off verifier to run them.
6. **Gentle-ai does not estimate effort or measure tokens or ODD adherence.** We have better measurement (`audit snapshot|compare`); the plan uses it for a before/after, and adds a forecast-vs-actual Sizing block that gentle-ai lacks.
7. **Risks:** managed AGENTS.md block is overwritten by `ai-resources setup`; workflow skills are generated from YAML; handoff keys are a hook-checked contract; Engram project resolution in worktrees and tool-set drift are unverified; ODD's verbatim-quote rule conflicts with our English-only rule.
8. **Decisions needed:** where the durable task doc lives, language policy for verbatim quotes, and whether `/kit-*` become size-gated by default (doc 04, section 6).
9. **Effort (forecast):** about 1,150 changed lines in five waves, W1 (docs/prompts) first; no parallel writers needed.
