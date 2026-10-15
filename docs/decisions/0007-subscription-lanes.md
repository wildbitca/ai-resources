# ADR-0007: Subscription lanes (a separate lanes pass, kit-only landing, and the `subscription-claude-55` profile)

**Status**: Accepted by the operator on 2026-10-15, without the "Antigravity lane" section, which stays Proposed-deferred until P3-S0. Phase 0 (this ADR and the isolated probes) and phase 1 (the `subscription-claude-55` profile) are implemented; phase 2 (the claude-cli lanes machinery, the kit commands and the setup writer) is implemented, with the isolated-gateway merge gate still to run. The evidence section states what each isolated-gateway probe showed and how strong that evidence is.
**Date**: 2026-10-14
**Decision makers**: planner, implementer, operator (decisions of 2026-10-14 recorded in `.agent-output/planner/subscription-lanes-plan.md`, section 0)
**Amends**: [ADR-0001](0001-model-pins-and-auto-update.md) (second validated writer), [ADR-0002](0002-provider-agnostic-model-selection.md) §8 (second validated writer; rendered `lanes.yaml`), [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (watch record, teardown, classify-before-add, drained path), [ADR-0005](0005-cross-provider-role-dispatch.md) §1, §2, §3, §4 and the writers matrix, §6, §7 and "Security limits", [ADR-0006](0006-scored-team-composition.md) §4, §8, "Output limits", "Rollback" (scoped to composition; the revisit trigger at "What made this Accepted" is not met)

## Context

The operator uses subscriptions only (Claude Code, Antigravity), with no paid APIs. Three facts shape the design, each verified in code:

1. **Compose forbids Antigravity, and keeps forbidding it.** ADR-0006 §4, rule 017 "Composed teams", the `direct-only` usage in `scores/model-scores.yaml` and the pinned tests govern compose's model choice. The lanes pass is a separate output under this ADR.
2. **A claude-cli lane does not spread quota.** It draws the same `claude-subscription` pool as Claude Code; it adds isolation and parallelism and costs memory. The agy pools (`agy-claude-gpt`, `agy-gemini`) are a second quota.
3. **The shipped engine cannot produce the operator's role table.** A curated profile supplies the models (WHAT, ADR-0002); a lanes pass decides where each role runs.

## Decision

### 1. Lanes are a separate pass after the model choice

A pure module (`scoring/lanes.py`) runs after the model choice. `compose.py`, `gate.py`, `db.py`, the dataset and the golden fixture are not changed. Compose never emits a lane. For each role the composition still picks a model (`by_role`); the lanes pass places the role on `claude-code` or on `openclaw-agent` + `agentId` (an OpenClaw agent on `claude-cli`, or in phase 3 an agy backend). A lane never moves the model or the band.

### 2. Data model

- **Setup state (WHAT, ADR-0002).** `ProfileState.lanes` (absent on a fresh install): `agents` (`{<id>: {backend, model, pool, stale?}}`, ids `kit-lane-<lane>-<model>`), `roles` (candidates per role, order is the tie-break and not a rank), `second` (candidates for an optional parallel second member of a report-contract role), `orchestrators` (the only agents whose `allowAgents` receive lane ids; `agents.defaults` is never touched), `verify_capability` (static record per orchestrator: `exec`, `plugin-tool` or `none`, from the orchestrator's config or by reference to isolated V8 evidence; setup never spawns a session and never calls the live gateway), `pool_caps` (optional, never applied to checkers), `max_remote_concurrency` (default 2) and `workspace_root`. `ProfileState.lane_acks` holds `{anthropic?, google?}` acknowledgements `{date, text_sha256, text, roles}`; a lane on an account is admitted only for listed roles. Old state files load unchanged and round-trip byte-identically.
- **Rendered `lanes.yaml`.** `~/.config/ai-resources/lanes.yaml` (`agents`, `roles`, `second`, `mode`, `digest`) is a setup-owned rendered artifact (not HOW) that exists in both single-model and multi-model mode. Written only when lanes are non-empty; removed by `--no-lanes` and by teardown; the `models` commands never create it from scratch, but the `models update` fan-out re-renders it as a registered artifact (needed for the stale marking and restore of AC2.16). `executors.yaml` is **not changed**: no `lanes:` block, byte-identical with and without lanes, and still not written in single-model mode. `lanes.yaml` is tracked in `tracking.config_files_written` like `executors.yaml` and, on the re-render path, is a registered artifact with its own backup before write and its own restore on failure (C3). The gateway never reads it, so no reload and no watch record follow.
- **Model drift.** When a `models update` or a family alias change makes a laned role's model differ from its lane agent's `model.primary` (by family for alias roles, by exact ref otherwise), the re-render marks the agent `stale: true`; the lanes pass refuses it, and `status`, `verify` and `models update` warn "lane agent <id> model <old> differs from role model <new>; re-run `ai-resources setup --lanes`". The digest covers agent model refs.
- `executors apply`, the re-render and the `lanes.yaml` render never open `openclaw.json`.

### 3. Workflow steps and the precedence table

`backend:` accepts `claude-code`, `openclaw-agent` and `lane`. New fields: workflow-level `integrating_writer` (default `implementer`), step `write_scope` (path prefixes), step `integrates` (scope step ids the kit merges), and `execution_hints.lane_member: second`. Lane children never write the handoff file; every lane step ends its step-scoped report with the handoff fields it would have set and the orchestrator joins them (WORKFLOW_CONTRACT rule 4).

| Step declares | Lanes table | Orchestrator | Result |
|---|---|---|---|
| `claude-code` | any | any | claude-code |
| `openclaw-agent` + agentId | ignored | has `sessions_spawn` and agent allowed | that agent (ADR-0005 rules) |
| `openclaw-agent` + agentId | ignored | no `sessions_spawn` or agent not allowed | Blocked (ADR-0005 §3) |
| `lane` | no entry for role | any | claude-code (the off switch) |
| `lane` | resolved to `claude-code` | any | claude-code (still under the kit flow when the run has lane writers) |
| `lane` | resolved to `openclaw-agent:<id>` | `sessions_spawn`, agent in `allowAgents`, `verify_capability.mode != none` | that agent, under the step's contract |
| `lane` | resolved to `openclaw-agent:<id>` | no `sessions_spawn` | claude-code, "lane unavailable here" (soft fallback for `lane` only; amends ADR-0005 §3) |
| `lane` | resolved to `openclaw-agent:<id>` | agent missing from `allowAgents` | Blocked (configuration error) |
| `lane` | resolved to `openclaw-agent:<id>` | `verify_capability.mode == none` | Blocked ("kit commands cannot run here") |
| `lane` | writer pool cap reached (if set) | any | next candidate, "cap reached" (never applies to checker steps) |
| `lane` (checker) | candidate model equals a writer model recorded in the run ledger | any | next candidate; none left: Blocked with the models named |

### 4. Pool mapping and peer lanes

| Lane | Pool | Headroom source |
|---|---|---|
| claude-code | claude-subscription | live (statusline sidecar) or static |
| openclaw-agent on claude-cli | claude-subscription | same pool: no quota gain |
| openclaw-agent on agy, Claude model | agy-claude-gpt | static (no live signal today) |
| openclaw-agent on agy, Gemini model | agy-gemini | static (no live signal today) |

The agy pools have `routes: []`; `lanes.py` maps them directly and the dataset is not changed. Lanes are peers: no permanent role ban on any lane; a role is ineligible only where a mechanism is missing. Headroom and evidence decide placement (least projected pressure, ties by candidate order then id); the profile's table is the default. The claude-cli and agy lanes get the same handoff contract, the same `ai-resources lanes` commands, the same worktree and scope rules and the same gates. The off-by-default state on a fresh install is part of the decision: lanes are disabled on a fresh install of the kit, `subscription-claude-55` declares a lane table, and setup enables it for the operator with the one-time account acknowledgement.

### 5. Eligibility, contracts and the forbidden list

A role may take any lane when its contract can be enforced by the kit commands from the dispatching orchestrator (`verify_capability.mode` is `exec` or `plugin-tool`) and the validator rules hold. An unknown role is ineligible (there is no contract to enforce).

| Contract | Roles | Where it runs | What `lanes verify` allows |
|---|---|---|---|
| report | explore, planner, software-architect, code-reviewer, security-auditor, verifier; any writer role on a step without `write_scope` that is not integrating | the run tree, read-only except its report | only `.agent-output/<role>/<step-id>*` and the tolerance list |
| scope | tester, doc-writer, generalPurpose, package-upgrade, fixer/IaC personas, with `write_scope` | own per-attempt worktree from a committed base | `write_scope` plus the step-scoped report plus the tolerance list; commits only on its branch, descending from the base |
| integrating | the `integrating_writer` role | own per-attempt integration worktree from the run tree's HEAD | anything except the forbidden list; commits only on the integration branch, descending from the base |

**Forbidden for every contract:** `.git` internals; other refs; git common `config`, `hooks/`, `info/`, `core.hooksPath`; `.gitmodules`, `.gitattributes`; `.githooks/`, `.claude/`, `.cursor/`, `.openclaw/`; the handoff file; every other step's `.agent-output/` files; the run state directory `$XDG_STATE_HOME/ai-resources/lanes/`; the tripwire host files (`~/.openclaw/openclaw.json`, `~/.claude/settings.json`, `~/.claude/hooks/`, `~/.config/ai-resources/`, shell rc files). Scope and report contracts also reject new symlinks, submodules and exec-bit changes. The commit-message check rejects any commit in `base..tip` that carries a `Co-Authored-By:` trailer or a "Generated with" line naming an assistant or tool. Tolerance list (exact): `IDENTITY.md`, `SOUL.md`, `USER.md`, `.pytest_cache/`, `__pycache__/`.

### 6. The kit-only landing path (amends ADR-0005 §4 "one writer per run")

"Each run has one landing path, the kit command `lanes land`. At most one integrating writer per run produces the integration branches it lands. Every lane writer, the integrating writer included, works in its own per-attempt worktree. Only the kit merges scope branches (`lanes integrate`) and only the kit moves the run tree (`lanes land`). No agent merges or lands." This holds for every role and every lane. Runs without lane writers keep today's behaviour.

Flow for every lane step: `prepare` (iteration counter, model and family recorded, checker independence, committed base, worktree via `openclaw worktrees create`, snapshots, pool count); `integrate` (`git merge --no-ff` of a verified scope branch into the integration worktree; a conflict aborts in the integration worktree and blocks the step, the run tree is untouched); dispatch (`sessions_spawn` with `cwd` set to the worktree); `verify` (hard verdict; a FAIL is a failed attempt, at most 3, and the attempt's worktree is removed with `openclaw worktrees remove`, never reset); a `verifier` step on the subject `<step-id>@<tip sha>`; `land` (`git merge --ff-only` in the run tree); cleanup. `land` refuses unless the integrating verify is PASS for that tip in this iteration, a Verifier PASS is bound to that exact commit, the integration branch's recorded base equals the run tree's current HEAD ("base <sha> is not run HEAD <sha>"), the run tree's index and tracked content are unchanged since the integrating prepare, and the ledger shows no other land for this **(step, iteration)**. A loop-back through `on_concern_return_to` is a new iteration and may land again. Lands within a run are serialised. Concurrent scope writers get their own worktrees with disjoint scopes; integration runs one scope branch at a time; `max_remote_concurrency` (2) holds for all remote children.

### 7. Verdict integrity (threat model)

- `lanes prepare` creates an append-only, hash-chained run ledger under `$XDG_STATE_HOME/ai-resources/lanes/<run-id>/ledger.jsonl`. Every command appends an entry and prints `{nonce, snapshot_digest, ledger_head}`.
- The orchestrator passes `--expect <nonce>:<digest>:<ledger_head>` to `verify`, `integrate` and `land`. The head must be an ancestor of or equal to the current chain head; a rewritten but self-consistent ledger no longer contains the head the orchestrator saw, so the command fails.
- Records are write-once files keyed by run, step, attempt and nonce; the ledger stores their sha256. A Verifier verdict counts only through the Verifier step's own verify record, which binds the report sha256, the parsed verdict and the subject.
- **Fail-closed chain:** `integrate` and `land` require prepare then verify PASS for every step they depend on.
- **Witnessed authority (C2).** A host-user process (any lane child) can read the ledger, snapshots and nonces and can run the kit commands. Authority therefore comes from what the orchestrator witnessed, not from file secrecy: an entry carries authority only if it lies at or before the `ledger_head` passed in `--expect`, and `integrate` and `land` ignore every entry after it; `verify` FAILS with "ledger written during the child's run" if the ledger gained entries between that step's prepare and verify that do not belong to steps declared `--in-flight`; the orchestrator always passes the head printed by the command it just ran, never one read from the file.
- **Amendment (verifier re-verification).** A shared report directory cannot attribute a write made before `verify`, so a `verifier` or `security-auditor` lane step is never a parallel member with another lane step (validator rule) and `lanes verify` FAILS a checker given `--in-flight` steps; the checker writes its report with nothing else running. Ignored-file coverage (G4) is limited to files under the forbidden roots, so AC2.17 reads "ignored files under forbidden roots".

### 8. Checker independence on the models that can actually run

- **Hard rule:** the verifier's model is not the implementer's model (Q7), extended to every checker role (`verifier`, `security-auditor`) against every writer role (integrating writer and scope-capable roles). Independence is by model, not by lane or provider (OQ-3 decided: a verifier may share the provider with a different model).
- **Static (resolve time).** The set of models a role can run is its primary, plus, only in multi-model mode, the LiteLLM union of fallbacks keyed by that primary and `default_fallbacks`. In single-model mode it is the primary only (fallbacks are inert, rule 017). A violation raises `LaneConflict` naming roles, models and the fallback edges to remove. This is not in `compose._solve`, because the golden output would shift.
- **Dynamic (dispatch time).** `lanes prepare` refuses a checker whose model family equals any writer family recorded in the run ledger, or whose family is unknown (fail closed).
- **One normaliser (C1).** Both checks compare families through `roles_policy.model_family(ref)` (aliases `haiku`, `sonnet`, `opus`; `anthropic/...`, `claude-cli/...`, `claude-kit/...`; agy slugs); `None` for an unknown family counts as conflicting for checkers.
- **Single-model alias target and alias drift.** `subscription-claude-55` is a single-model alias profile (`mode: single-model`, `haiku`/`sonnet`/`opus`), because no gateway resolves `anthropic/...` ids in that mode (V11 decides whether pinned ids may replace aliases). An alias floats to the newest model of its family; lane agents pin a concrete ref. Version drift inside a family is the same family: not stale, not a conflict, and the stale-agent warning reports a mismatch only when the family or the exact ref differs. A multi-model twin, if ever added, must drop the planner's Opus fallback and the architect's Sonnet fallback, because the LiteLLM per-model union would otherwise cross checker and writer models.
- **Same-model lane as the availability fallback.** When a checker's preferred lane is unavailable, the same model on another lane is the fallback; a different model is never substituted silently. Pool caps never apply to checkers: a checker with no admissible candidate is Blocked with the reason and never waits.
- Q3: security-auditor stays on Opus 5.5; the CyberBench caveat (74.6 tagged CVP vs 55.4 untagged; Sonnet 59.6) is recorded here and a lane never moves the model.

### 9. Accounts, acknowledgements and process facts

- **Per-account acknowledgements** (one per account, with a role list; adding a role means re-signing): Anthropic, covering the shared-login grey zone for claude-cli lanes and the `-p`/SDK billing changes; Google, covering ToS and data egress for the acknowledged roles (explore, code-reviewer, tester, doc-writer, security-auditor, extended by the operator to every role listed in the acknowledgement). `--allow-unverified` opens neither.
- **Measured process flags (2026-10-14), recorded as facts and not a ranking:** `claude-cli` agents run with `--permission-mode default`, `--permission-prompt-tool stdio`, `--strict-mcp-config`, `--allowedTools mcp__openclaw__*` and `--disallowedTools ...,Bash,...`; `claude-kit` and `agy-cli` run with `--dangerously-skip-permissions`.
- No Claude Code hooks fire for any OpenClaw child (ADR-0005 §6), so the kit commands are mandatory on both lanes.
- **Compose's ban is kept.** Antigravity stays direct-only in composition; the FAQ reading is unchanged and the opt-in is an operator risk acceptance.

### 10. How setup writes OpenClaw config

Operator-run only, with an explicit confirm. A lanes patch builder sits beside the antigravity patch builder (fill-only; `assert_channels_safe` holds). `openclaw agents add/delete` through `openclaw_host.agent_new` is a **second validated writer**, outside `apply_patch` (amends ADR-0001 and ADR-0002 §8). Setup classifies `agents.entries.<id>` with `openclaw_reload_rules` before the add (ADR-0003); restart-required keys, the plugin backend and the plugin tool go through the drained path. Lane agents get a `lane-agent` workspace template under `~/.openclaw/kit-lanes/<agent id>/` (never `/tmp`), recorded in `created_workspaces`; if V9 shows a `cwd` outside the workspace is refused, `workspace_root` covers the managed worktree root and this ADR records that choice. `allowAgents` is scoped to `lanes.orchestrators`. Lane leaves enter the per-run `config_watch` record (ADR-0003 §6); the watch revert deletes only recorded agent ids, or they are removed by teardown only (per V4). Teardown removes only kit ids: scalars via `restore_patch`; `allowAgents` by set difference; `agents delete` only for `created_agent_ids` with confirmation; only `created_workspaces`; teardown does not touch plugin entries (phase 2 writes no plugin backend or tool) and does not remove worktrees: lane worktrees are removed only by `lanes land`, by a failed `lanes verify` and by a `--reenter` prepare, through `openclaw worktrees remove <id>` for ids recorded in the run ledger. **The lanes writer never spawns a session and never probes the live gateway** (setup as a whole still runs its pre-existing `openclaw health` baseline before its first write).

**MCP scoping for claude-cli lanes (OQ-5, claude-cli part; the agy half of V7 stays phase 3).** Bundled `claude-cli` starts with `--strict-mcp-config`, but every server in OpenClaw's `mcp.servers` (including the ones `_openclaw_mcp` mirrors from the operator's Claude Code) is loaded per run, so each lane child spawned its own copy of every server (measured on the live gateway: ~89 supabase/engram processes at ~12.2 GB of a 12.9 GB `MemoryHigh`). The lever is OpenClaw's own per-agent tool policy: a whole-server denial in `agents.entries.<id>.tools.deny` (`bundle-mcp`, or `<server>__*`) excludes the server before it is spawned (docs `plugins/bundles.md`, `cli/mcp/registry.md`; `createMcpServerToolDenyMatcher`). Setup therefore writes `tools.deny: ["bundle-mcp"]` on every `kit-lane-*` entry by default (fill-only: operator tokens are kept, stale kit tokens are dropped, recorded in `lanes_applied.mcp_deny_added` and removed by set difference on teardown). The opt-in is the profile key `lanes.mcp: {<role>: [<server>, ...]}`: the named servers are kept and the other configured ones are denied by name. Known limit: in opt-in mode `bundle-mcp` is not denied (it would also exclude the kept servers), so bundle-provided servers that are not in `mcp.servers` stay loaded, and an empty `mcp.servers` yields no deny at all; sanitized-name collisions between a kept and a denied server are not checked. `explain` shows `[mcp: ...]` in the Lane column, and `verify --cockpit openclaw` warns on a lane agent without the tokens. Static evidence only: the effect on the live process count is a phase 2 merge-gate check (run a lane turn, count `supabase`/`engram` children). The Antigravity section is unchanged.

### 11. Antigravity lane (Proposed-deferred until P3-S0)

This section is Proposed and deferred. Phase 3 does not start before P3-S0, the operator's approval after the phase 2 merge gate, V5-V7 and V9 (agy), OQ-5 and OQ-6, and the signed Google acknowledgement. Contents to be settled then:

1. ToS clause quoted (ADR-0006 §4, `usage-google.md`), suspension scope, unchanged FAQ, signed account acknowledgement, compose's ban kept.
2. Process flags compared across `claude-cli`, `claude-kit`, `agy-cli`.
3. No Claude Code hooks for any OpenClaw child; the kit commands are the control.
4. Egress for every acknowledged role, including MCP data (V7).
5. V6 result, or "writes outside the repo and the tripwire list are undetected".
6. Tool surface (V7).
7. About 50k tokens per step (ADR-0005), static headroom, optional caps.
8. Evidence V5-V10 and isolated P6 per role shape.
9. Status: Proposed-deferred. An agy lane needs `--lanes-antigravity`, the Google acknowledgement and this section Accepted.

## Evidence

Measured on 2026-10-14 (UTC) on OpenClaw 2026.10.3 against an **isolated** gateway (`kite2e`, loopback 19789, own
cgroup) driven by the `bin-lanes` harness (claude-cli agents only; seal `c996908d1161510a3710e94772210e282706b2e23a39ff72475e786e51991932`),
never against the live gateway. Each run was closed by the harness operator with the ADR-0005 live-untouched proof
(86 L1 sha256 entries identical, live MainPID and NRestarts unchanged, no `message` call, no live message delivery in
any probe window); that proof was reported in the operator's session and its raw artifacts were lost (incident note
below), so it is a report, not an independently re-verifiable artifact.
Run directories: `20261014T210239Z` (V1, V2, V8, V9), `20261014T211507Z` (V10, V11), `20261014T220817Z` (V4),
`20261014T221645Z` (V3 in hybrid reload mode).

| V | Question | Status | Observation | Dependent step |
|---|---|---|---|---|
| V1 | Model ref form for a claude-cli OpenClaw agent | VERIFIED | The runtime-pinned canonical ref, the legacy `claude-cli/` ref and the full id `anthropic/claude-sonnet-5-5` each ran the agent through claude-cli and answered | P2-S3 |
| V2 | How an OpenClaw-run orchestrator dispatches a `claude-code` step | PARTIAL | Dispatch of a child by `sessions_spawn` from a claude-cli orchestrator works (V9). The tool list captured by the harness was empty in the collected table, so the content of the orchestrator's tool list (`sessions_spawn` is present in one manual read; two manual reads disagree on whether `exec` and `process` are listed) rests on raw run output that was not preserved (incident note below) and is UNVERIFIED in the table. How a `claude-code` step is expressed in an orchestrator is documentation (P2-S6) | P2-S6 |
| V3 | Hot vs restart-required (hybrid reload mode) | VERIFIED for four of five keys; `plugins.entries` UNVERIFIED | Re-run on 2026-10-15 (run `20261015T044718Z`, regenerated hybrid config, gateway pid unchanged in every step, artifacts and `proof-after` saved in `~/.cache/ai-resources-evidence-backup-p0rerun-20261015/`): `agents.entries.<id>.subagents.allowAgents` real change: HOT (`config hot reload applied`); `agents add` of a new entry: HOT; `models.providers.<id>` new entry: HOT; `agents.entries.<id>.model` and `.model.primary` change on existing agents: HOT; `plugins.entries.anthropic.enabled`: accepted, no reload event logged, so it is NOT classified and stays restart-required by default. This supersedes the 2026-10-14 readings, where the harness regex had mislabelled `models.providers` and `plugins.entries` as restart-required. With `gateway.reload.mode=off` every change needs a restart. | P2-S5: lane agents, `allowAgents`, catalog entries and model changes are hot in hybrid mode; plugin entries and the plugin backend stay on the ADR-0003 drained path |
| V4 | `openclaw agents delete <id>` prunes only that agent's workspace and state | VERIFIED (effect), harness verdict UNVERIFIED because an empty state directory remains | Run `20261015T044718Z` repeated the 2026-10-14 result: rc 0, the target workspace removed, the other agent's workspace and state byte-identical, `agents list` consistent, trash entries under the sandbox data directory, `agents/<id>/` left as an empty directory. Teardown must tolerate that leftover | P2-S5/S6 |
| V5 | `agy models` slugs | UNVERIFIED (operator, phase 3) | | P3-S1 |
| V6 | `agy --sandbox` containment, argv, separate backend id | UNVERIFIED (phase 3) | | P3-S1 |
| V7 | MCP servers an agy child receives via `bundleMcp`; per-agent scoping | UNVERIFIED (phase 3) | | P3-S0 (OQ-5) |
| V8 | Whether the orchestrator can run kit commands through OpenClaw exec | VERIFIED (capability) | `python3 -c print(1)` and `ai-resources version` ran through `mcp__openclaw__exec` with a nonce proof, with the orchestrator started with `--disallowedTools ...,Bash,...`. Both an agent with and one without an explicit `tools.exec.mode` could do it, so no static key separates them: exec is available to claude-cli orchestrators by default | P2-S5b not needed for claude-cli |
| V9 | A spawn with `cwd` in a managed worktree outside the lane agent's workspace (claude-cli) | VERIFIED | The child's `pwd` and toplevel equalled the worktree under the managed worktrees root and the nonce matched | P2-S4/S5 |
| V10 | `openclaw worktrees create/remove` | VERIFIED | Branch `openclaw/<name>`, under the managed root, listed, `remove` succeeded and the branch was kept (the table shows `snapshot_paths=[]`, so snapshot-on-remove is NOT evidenced). The first `create` after a gateway start failed once with `OpenClawStateLeaseError` and succeeded on retry about a minute later; that observation came from a gateway log that was lost, so it is recorded here without a surviving artifact: the kit should retry, treat as a precaution | P2-S4 |
| V11 | Pinned full Claude id in generated subagent frontmatter, no gateway | VERIFIED | `claude -p --agent` with the full id and with the alias `sonnet` both answered and used `claude-sonnet-5-5` | P1-S1/S2 (the profile may use aliases or pinned ids) |

Stop rules (unchanged): V1, V2, V3, V4, V9 (claude-cli) or V10 UNVERIFIED stops phase 2 after P2-S4. Current state:
V3 was re-run on 2026-10-15 (four of five keys VERIFIED; `plugins.entries` stays restart-required by default). V2 is
still PARTIAL (the orchestrator tool list is UNVERIFIED); the operator accepted that partial evidence with the ADR
on 2026-10-15, which is the recorded waiver of the P2-S4 stop for V2. V5, V6, V7 gate phase 3 only.

Known limitations (final verification): (G8) the C2 taint skips ledger entries of the verified step itself, so a child can pre-seed same-step prepare entries; by analysis the orchestrator's verify wins and a seeded entry can only cause a refusal (denial of service, not authority), and this was not probed. (G9) Dispatch-time independence trusts the `--model` the orchestrator declares; the kit cannot see the model that actually ran, and the validator does not force a `claude-code` writer step in a lane workflow to be a `lane` step (the `land` tracked-digest check and the documentation cover it).

Evidence-loss incident (2026-10-15 ~02:27 UTC): after these runs a harness-builder agent deleted
`~/.cache/ai-resources-live-e2e/proof-lanes/` (the six run directories, their raw logs and per-probe outputs) and
`state-lanes/` with an `rm -rf`. The raw artifacts are not recoverable. What survives is the deterministic collect table of
each run (`p0-s2.md` content, saved from stdout in `~/.cache/ai-resources-evidence-backup-20261015T0228Z/lanes-collect-tables/`:
`lanes-collect.out`=20261014T210239Z, `lanes-collect2.out`=211507Z, `c3.out`=220817Z, `c4.out`=221024Z, `c5.out`=221645Z) and
the proof-after summaries as quoted in the operator's session (not preserved on disk). Every row above is therefore supported by its collected table, not by raw logs; the
`proof-agy` evidence of ADR-0005 is intact. Consequences recorded by the independent Verifier: the "live host untouched" proof of the lanes runs (86 L1 entries, MainPID 962, NRestarts 0, no `message` call) and the seal `c996908d...` have no surviving artifact and are not re-verifiable; V1, V4 and V8-V11 are "verified (collect table only)"; V2 and V3 stay PARTIAL. The V3 and V4 re-run required before any setup-writer step (P2-S5) was done on 2026-10-15 (run `20261015T044718Z`) with the corrected harness and a fresh untouched-host proof (`proof-after` Overall PASS: 86 L1 entries identical, MainPID 962, NRestarts 0, no `message` call); the live-untouched claim of the 2026-10-14 runs must still not be used as a gate.

Two operational findings from the runs: (1) the shared `openclaw` package of the live host was rewritten in place
twice and its `bin/openclaw` link was absent for a few minutes (attributed by the operator to another agent testing the
same thing); a harness that shells out to `openclaw` must tolerate that window. (2) OpenClaw's config journal restores a
regenerated `openclaw.json` ("clobbered") unless its fingerprint key and last-good copy are removed together.

## Alternatives Considered

1. **Lift the antigravity ban inside compose.** Rejected: compose's golden output, the usage gate and the tests are the contract; the lanes pass is separate and opt-in.
2. **Carry the lanes table in `executors.yaml`.** Rejected: it is not written in single-model mode, so it cannot hold the table for the operator's profile; `lanes.yaml` exists in both modes.
3. **Let an agent land its own work.** Rejected: hooks do not fire for OpenClaw children, so the kit command is the only control point.
4. **A permanent role ban on the agy lane.** Rejected by the operator: lanes are peers; ineligibility comes only from a missing mechanism.

## Consequences

- Fresh installs and existing workflows are unchanged: no `lane` steps and no lanes table mean claude-code.
- A claude-cli lane costs memory and gives no quota gain; the value is isolation and parallelism.
- The kit commands, the ledger and verify records become part of the trust boundary for lane runs; the ledger does not protect against a host-user process that can also act as the orchestrator, which is why authority is bound to the witnessed head.
- Alias drift is accepted for the single-model profile and reported, not hidden.

## Rollback

- Phase 1: delete the profile, its test and doc row.
- Phase 2 state: `ai-resources setup --no-lanes` clears `ProfileState.lanes`, removes `lanes.yaml`, tears down kit ids only (scalars restored; `allowAgents` by set difference; recorded agents and workspaces removed; plugin entries and worktrees are not touched by teardown). Landed commits stay as normal commits; un-landed lane branches remain; ledgers stay under `$XDG_STATE_HOME` as evidence.
- Workflows: `backend: lane` steps resolve to claude-code once the table is empty.
- Phase 3: drop `--lanes-antigravity` and the Google acknowledgement; teardown removes the catalog stub, agy agents and the recorded plugin backend id (drained path).
- Code: each phase its own local commit; `git revert <sha>`.

## Follow-ups (not blocking)

- **FU-1** lazy MCP startup for lane children.
- **FU-2** Codex lane.
- **FU-3** per-role evidence rows and a `dataset_version` bump.
- **FU-4** an `effort` field.

## Related

- [ADR-0001](0001-model-pins-and-auto-update.md), [ADR-0002](0002-provider-agnostic-model-selection.md), [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md), [ADR-0005](0005-cross-provider-role-dispatch.md), [ADR-0006](0006-scored-team-composition.md)
- Plan: `.agent-output/planner/subscription-lanes-plan.md`
