# Cockpit recommendations: the answers that give a performant and low-cost setup

Setup asks each ticked tool its own questions (ADR-0015). This page says which answer to give so the setup is fast where it
matters and cheap everywhere else. It follows the cost analysis behind ADR-0013 and ADR-0014: the expensive things are a large
fixed prefix, polling turns, models above the tier a role needs and thinking that nobody asked for. Numbers here are
API-equivalent estimates; on a subscription you spend quota, not dollars ([validating cost](validating-cost.md)).

## The short version

| Question | Answer | Why |
|---|---|---|
| Starting team | **Balanced** | Haiku for explore, tester, doc-writer and package-upgrade; Sonnet for planner, architect, implementer and reviewer; Opus only for the verifier and the security auditor. The checkers are independent of the writers. |
| Claude Code sign-in | **Your subscription** when you have one; an API key only when you want per-token billing and its limits | A key is read from your environment and never stored. Vertex AI keeps only the project and region. |
| Skills | Leave `AI_RESOURCES_SKILLS` unset; add a domain (`flutter`, `symfony`, ...) only for the stacks you use | Every listed skill costs prompt tokens in every session; the lean list is the same for every cockpit. |
| Effort | Keep the profile's `role_effort` | planner, architect, verifier and security-auditor high; implementer and reviewer medium; explore, tester and docs low. |
| Parallel runs | Off unless you need them | They add Claude Code lanes and, with Antigravity, a terms acknowledgement. |
| OpenClaw providers | Tick only the providers you really use | Routing, fallbacks and the 15 role agents are limited to the ticked providers. A role whose provider you did not tick runs on the cheapest ticked one and the review says so. |
| OpenClaw sign-in per provider | **CLI backend** for a provider you have a subscription for (Claude Code, Codex); **API key** otherwise | The API key is saved by OpenClaw in its own store through `openclaw models auth paste-api-key`; the kit never sees it. |

## Per cockpit

**Claude Code.** Subscription sign-in, `balanced`, lean skills. Setup also sets the auto-compact window, removes the agent-teams
flag it once wrote, registers the sleep guard (a `sleep` over 30 s inside one tool call is refused: use `run_in_background`
and one read) and writes subagents with the right model and effort. The hygiene step (default No) offers to disable MCP
servers and plugins nobody called in 30 days.

**OpenClaw.** Tick the providers you have. Setup then writes one `kit-role-<role>` agent per role with the profile's model,
`thinkingDefault` from the effort table and a scoped skills list; it fills `agents.defaults.subagents` limits (3 concurrent,
3 children, depth 2, 2 h per run) only where you set none, and scopes skills through a managed directory. Everything is
fill-only and hot: nothing restarts the gateway. A new kit plugin version is recorded as one pending entry; run
`ai-resources openclaw apply-pending` from a terminal when the gateway is idle.

**Antigravity (agy).** Signs in with your Google account. Setup adds `engram` to its MCP file only when absent (with a backup)
and a lean role list to `GEMINI.md`. Its lanes stay behind the terms acknowledgement and the proven gates of ADR-0007. A
`kit-lane-agy-*` agent that runs a Claude model on the API is reported, and setup offers (per agent, with your confirmation)
to point it back at the Antigravity catalog or remove it; it never changes it silently.

**Cursor.** Sign-in is managed in Cursor; setup asks nothing and writes its instruction file and MCP entry as before.

## The native orchestrator

`/equipo <goal>` on OpenClaw runs the `feature-implementation` workflow as a team: research, plan, architect, implement, test,
review and security in parallel, verify, document. Defaults that keep it cheap and safe: 2 teams at once (the rest queue), each
team in its own worktree, one writer per run, the verifier on its own agent and model family, the plan approved by you before
the first writer, timeouts per role and at most 3 returns to a step. Watch it with `/workflow status` and read what it cost
with `/workflow cost <run>`.

## What not to do

- Do not give every role the strongest model: only checkers need it.
- Do not tick providers you do not use: each one widens routing and the fallback chain.
- Do not poll: wait with one blocking call or `run_in_background`. The audit alerts when poll turns exceed 5% of the cost.
- Do not set a high effort on the cheap roles to "be safe": the effort validation in `ai-resources audit` shows output per
  call by role, so you can see whether a setting changes anything.
