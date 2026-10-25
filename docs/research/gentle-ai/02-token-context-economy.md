# Study 2: Token and context economy

Status: research. Nothing is implemented. Date: 2026-10-25. Index: [README](README.md).
Labels: **[V]** verified from code/doc, **[M]** measured with a command, **[I]** inferred. Gentle-ai paths are relative to `Gentleman-Programming/gentle-ai@5ee1ac7`; `kit:` paths are relative to `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec` (ai-resources 2.8.4).

Token conversion used everywhere: **4 bytes per token**, the same rule our own `validate_kit.py` Haiku budget uses (`kit:docs/decisions/0014-token-cost-round-2.md`, D1). It is a rough figure; treat tokens as order-of-magnitude.

## 1. Headline findings

1. [V][M] **Gentle-ai is not lean in absolute terms.** The instruction file it installs for OpenClaw is **55,211 bytes (~13.8k tokens)** for a capable model and **35,911 bytes (~9.0k tokens)** for a "small" one (`internal/components/agentguidance/testdata/orchestrator-module-baseline.json`, entries `openclaw/capable` and `openclaw/small`). Claude Code gets 82,908 bytes in a single file. Our whole managed AGENTS.md block is 11,087 bytes ([M] below). Importing gentle-ai's prompt would *raise* our context cost 3-5x. What is worth taking is its **rules**, not its text.
2. [V] Its real economy is **behavioral**: do not spawn when inline is cheaper, hand off by path instead of paraphrase, load skills by path only when a subagent needs them, verify only on named risk. These cut *spawns and re-reading*, which dominate cost in orchestrated work.
3. [V] Its *byte-level* context reductions are small and self-described as non-claims: the Claude-only module split moves 6,166 of 82,908 bytes (7.4%) out of the always-loaded file, and the test comment says the ceiling "is an engineering ratchet, not a token, cost or latency claim" (`orchestrator_module_budget_test.go`, comment above `claudeModuleOverheadCeiling`). No prompt-caching mechanism and no token measurement exist in the repo (`grep -rIi "prompt cach\|cache_control" internal docs skills odd` returned nothing).
4. [V][M] **We already do more infrastructure-level cost work than gentle-ai** (ADR-0013/0014: skill scoping, auto-compact window, MCP pruning, model profiles, billing awareness, `audit snapshot|compare`). **We do less behavioral cost work**: our workflows always spawn a fixed set of agents regardless of task size (section 5). That is the gap.

## 2. Gentle-ai mechanisms, one by one

### 2.1 Per-capability prompt variants (capable vs small)

[V] `internal/assets/generic/orchestrator.md` holds two sections, `model-capable` and `model-small`; `selectGenericOrchestrator` keeps one and strips the other (`internal/components/agentguidance/orchestrator.go:151-185`). The comment is explicit: it "selects instruction text, never an execution model" (`orchestrator.go:186-189`). OpenClaw uses the generic asset (`orchestratorAsset` default branch, `orchestrator.go:121-146`).

[M] Savings for OpenClaw: 55,211 -> 35,911 bytes = **-19,300 bytes (-35.0%)**. The small variant also tells the model to "load only up to 3 relevant `SKILL.md` paths and prefer numbered step instructions" (`generic/orchestrator.md`, `model-small` section) and omits the long-form delegation ladder.

[I] Relevant to us because our Haiku-tier roles (explore, tester, doc-writer, package-upgrade under `balanced`, ADR-0014 D1) get the same prompts as Sonnet roles.

### 2.2 Orchestrator core + on-demand modules

[V] `orchestrator_modules.go:13-175`. Marked fragments in the shared Markdown are moved into `orchestrator-<module>.md` files; the core keeps one pointer line per fragment: `On demand: when <reason>, read <path>, section "<title>".` Fragment specs: `writer.edit-surfaces`, `delegation.key-learnings`, `skills.discovery`, `skills.registry` (lines 44-49). Authority, safety, consent and the Delegated Verification Gate stay in the core on purpose, because they "must apply before a pointer could fire" (lines 39-43).

[V] Scope is narrow: only an explicit **user-global Claude Code opt-in**; "RenderOrchestratorWithSource and every other runtime and scope stay monolithic" (lines 18-20). Pinned bytes: `orchestrator-delegation.md` 1,171; `orchestrator-writer.md` 1,977; `orchestrator-skills.md` 3,018 (`orchestrator_module_budget_test.go`, `claudeModuleFiles`). Overhead ceiling when everything is read: 4,096 bytes.

[M] Moved out of the always-loaded file: 1,171 + 1,977 + 3,018 = **6,166 bytes = 7.4% of 82,908**. The test header admits it "proves nothing about whether or when Claude actually reads a module."

[I] The design principle is sound (keep safety text in the core, move situational procedure behind a one-line trigger); the realized saving is modest because they moved little. We have a larger and cleaner candidate (section 4).

### 2.3 Skill registry: index and paths, not summaries

[V] `docs/skill-registry.md:8-124`.

- `gentle-ai skill-registry refresh` writes `.atl/skill-registry.md` (+ cache) with each skill's name, **full** description, scope and exact `SKILL.md` path. Project skills win over global by name. `_shared`, `skill-registry` and any `sdd-*` skill are never indexed (lines 126-132).
- The orchestrator reads the registry once per session ("cache the skill index", `generic/orchestrator.md`, Sub-Agent Launch Pattern), matches skills to the task by file extension and action, and passes **paths** under `## Skills to load before work`. Subagents read the full files.
- Explicit design decision, `docs/skill-registry.md:117-124` ("Why Not Compact Rules?"): compact rule summaries "were cheaper per delegation but could distort skills"; index+paths "spends tokens only when a subagent actually needs a skill, and it preserves the complete runtime contract."
- Feedback loop: each delegation result carries `skill_resolution` (`paths-injected` / `fallback-registry` / `fallback-path` / `none`); a fallback means the cache was lost to compaction, so re-read the registry.
- Startup hooks refresh the registry on Codex, Claude Code, OpenCode, Pi (`docs/skill-registry.md:12`).

[I] "Compact rules" is the mechanism a reader of the task expects ("compact rules / lazy loading"). Gentle-ai *tried and rejected* it. Do not build summary skills; build an index and pass paths.

### 2.4 Engram persistence

[V] `internal/assets/engram/protocol.md` is one asset with four marked sections: `full` (11,719 B), `slim` (2,566 B), `passive-capture` (495 B), `compact` (817 B) [M: Python regex over the section markers].

- **Slim vs full** (`internal/components/engram/protocol.go:76-125`): the 2,566-byte slim section is injected **only for Claude Code**, and only when the installed engram binary is >= 1.4.0 (`engramProtocolVersionFloor`), because only there is there a "redundant channel": the MCP server `instructions` string plus the SessionStart hook deliver the full protocol anyway. Every other adapter gets the full 11,719-byte text. Saving where it applies: **-9,153 bytes (-78%)**.
- **Our situation [V, observed in this very session]:** the Engram MCP server already injects its instructions into our prompts (see the "## engram" block of the MCP instructions: core tools, PROACTIVE SAVE RULE, DELIVERY GUARANTEE, CONFLICT SURFACING). So we already have the redundant channel and must **not** copy the protocol into AGENTS.md. A gentle-ai-style install for OpenClaw would duplicate it (the "full" section for non-Claude adapters).
- **Progressive recall order** (protocol.md, "WHEN TO SEARCH MEMORY"): `mem_context` ("fast, cheap") -> `mem_search` -> `mem_get_observation` for full content. Search results are previews; the full text is fetched by id only when needed.
- **Upserts instead of growth:** `topic_key` ("same topic evolving -> same key") keeps one observation per evolving topic; `mem_suggest_topic_key` for unsure keys.
- **`capture_prompt: false`** for automated artifacts such as the ODD task mirror, so the mirror does not also store prompt text.
- **Compaction survival:** "AFTER COMPACTION: IMMEDIATELY call `mem_session_summary` with the compacted summary content ... then `mem_context`". Session identity is reused across compaction.
- **Delivery guarantee:** memory bookkeeping never replaces the user-facing answer; the answer is the last message, no tool calls after. (Already present in our Engram MCP instructions.)
- **Project resolution** (`docs/engram.md:189-197`): `mem_current_project` must be called and waited on before the first `mem_context/search/review`; ambiguous or mismatched `cwd` skips reads. Relevant risk for worktrees (Study 4, R-ENG).

### 2.5 Model routing

[V] Gentle-ai deliberately does **not** route models in prompts. "Model assignment is client-specific and applies to supported generic and review roles, not a formal development-phase matrix" (`docs/agents.md:50`); the orchestrator "retains the runtime's configured model choices for delegated tasks" (`generic/orchestrator.md`); the TUI has a Configure Models screen. The one routing *rule* in the text is negative: "a native tier, an unclear or failed assessment, and the writer's model or effort never add [a verifier] by themselves" (`routing.go:124`).

[V] Ours is a far more developed system: scored team composition (`kit:docs/team-composition.md`), `balanced` profile = Haiku 5.5 for explore/tester/doc-writer/package-upgrade, Sonnet 5.5 for planner/architect/implementer/reviewer/generalPurpose, Opus 5.5 for verifier/security-auditor (ADR-0014 D1); independence rule (a checker's model family must differ from the writer's, ADR-0007 section 8); Haiku static budget < 100k tokens (`validate_kit.py`, `HAIKU_PROMPT_BUDGET_TOKENS`).

[I] Nothing to adopt here. One **philosophical conflict** to resolve: gentle-ai says a weaker writer model never triggers a verifier; our kit says a checker must be an independent family *whenever a checker runs*. These are compatible if the *decision to run a checker* becomes risk-gated (gentle-ai) while *who runs it* stays independent (ours). Study 4, gap G6.

### 2.6 Sub-agent cost model

The most transferable block. All [V] from `routing.go:99-133` and `odd/tasks/1731-parity-delegate-for-reason.md`:

| Rule | Number or text | Why (as stated or inferred) |
|---|---|---|
| Inline evidence budget | one parallel batch, at most 3 calls, ~10k tokens, bounded search/line ranges, no whole large files | decide or verify without a spawn |
| Mapping trigger | spawn one read-only explorer only if more evidence is needed or more than ~5 sequential lookups | otherwise reading is cheaper in the parent |
| Explorer return | handoff of at most ~2k tokens with `path:line`, one parent spot check, do not reread the mapped evidence | cap the compaction ratio of a subagent |
| Spawn overhead | "a subagent spends one to two minutes reading before it writes" | stated reason for the next two rows |
| Writer threshold | units of ~100 changed lines or less "always run inline: writing them costs less than starting workers" | break-even |
| Several small independent changes | "run inline, one after another" | no fan-out for small items |
| Never split one behavior | no separate test, docs and code writers | each extra spawn re-pays the reading overhead |
| Never explore what you will read anyway | rule S5 | duplicate reads |
| Preparation | reading that prepares a delegated write goes *with* that write | avoid parent context fill |
| Output budget | parent bash output bounded to counts/`--stat`/tail; delegate long suites | keep parent context thin |
| Long-session backstop | at ~150k parent-context tokens pause and delegate the next bounded unit; **advisory**, not enforced | context pressure |
| Launch dedupe | session-scoped list of `(phase, task-fingerprint)`; never emit the same launch twice (generic orchestrator, "Sub-Agent Launch Deduplication") | duplicate spawns waste tokens and cause "file modified since read" conflicts |
| Verification timing | one end-of-feature verifier; one correction batch; one bounded recheck | stops loop-until-clean |
| Parent spot check | re-run one reported command; the only parent re-verification | cheap trust-but-verify |
| Spend proportionality | "validate consequential premises before building; reuse sibling investigation; focused checks while iterating, full checks at closure" (`odd-spend-proportionality.md`) | no repeated discovery or full-suite churn |
| Cost forecast | "Long or very-long work gets one cost/side-effect forecast before launch" (`odd-orchestrator-sections.md`, Native Checking Contract) | cheap guard before expensive runs |

### 2.7 Handoff by reference

[V] Section 5.1 of Study 1. Cost effect [I]: replaces paraphrase (hundreds of tokens, per hop, lossy) with a locator (tens of tokens) plus one canonical read by the worker.

### 2.8 Caching in gentle-ai

[V] Only application-level caches: the skill-registry cache, the orchestrator's once-per-session skill index, and "cache both choices" for the delivery strategy (`routing.go:174`). TUI update checks have a six-hour cooldown (`docs/usage.md:121`), which is not LLM cost. No Anthropic prompt-cache handling.

### 2.9 Review-context budget (RDD)

[V] `odd/tasks/review-runtime-context-budget.md`: a conservative 200 KiB input policy for review prompts with refusal before authority is created. Specific to `gentle-ai review`; not portable.

## 3. Our current mechanisms (verified)

From ADR-0013 and ADR-0014 (`kit:docs/decisions/`):

- Skill scoping: only the skills in `skill_scope.py` are linked natively; `workflow-*` skills and four step-loaded skills carry `disable-model-invocation: true`; role subagents preload `skills:`; the rest are read by path (ADR-0013 section 3).
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` (fill-only).
- Engram plugin's first-message ToolSearch directive removed by a hash-guarded patch (ADR-0013 section 4).
- OpenClaw host profile lean keys: session reset, `contextPruning.mode`, server compaction params, `subagents.{model,maxConcurrent,maxChildrenPerAgent}`, `utilityModel`, heartbeat isolation (ADR-0013 section 5, facts table).
- Unused MCP servers/plugins pruning offered one by one (ADR-0014 D3.2); persona subagents rendered only for active domains (D3.2, D6).
- `balanced` retune and `HAIKU_PROMPT_BUDGET_TOKENS` check (D1).
- Billing awareness (D2); polling guard hook `kit_sleep_guard.py` (D7) and the AGENTS.md "Long-running commands never block" and "Waiting without polling" rules.
- `ai-resources audit snapshot|compare` with message-id de-duplication and subagent transcripts (ADR-0013 section 2). Cost claims are API-equivalent estimates and "no saving is claimed without an `audit compare` between two snapshots" (ADR-0013, Consequences). This is **stricter** than anything in gentle-ai.
- Return format cap: every subagent ends with a Result block of at most 5 summary bullets; "read the handoff file and the return block, not the subagent's full output" (`kit:skills/kit-orchestration/SKILL.md:46,160-182`).
- Guidance-only skill `common-context-optimization` (2,028 B, `disable-model-invocation: true`): observation masking, compaction every ~10 turns or 8k tokens, and "KV-cache awareness" (static prefix first, append-only history). Prior art for prompt-cache hygiene that gentle-ai lacks; it is advice to the model, not a mechanism, and the only reference I found is the `explore` step of `workflows/explore-and-plan.workflow.yaml` (not an exhaustive search). [V]
- Handoff template already says `refs:` hold "paths written by steps, not the content" (`kit:handoff.md.template:12`), i.e. reference semantics for artifacts exist; they are not applied to the *requirement*.

## 4. What we inject, measured

[M] Commands: `wc -c` on the files named; a Python 3 `-I` pass over `skills/*/SKILL.md` frontmatter; `awk 'NR>=a&&NR<=b'` for section sizes. Tokens = bytes/4.

### 4.1 Always-on (every OpenClaw turn, including subagents)

| Item | Bytes | ~Tokens | Evidence |
|---|---:|---:|---|
| Workspace `~/Development/AGENTS.md` | 17,394 | 4.3k | `wc -c` |
|   of which the managed ai-resources block (lines 1-186) | 11,087 | 2.8k | `awk` between BEGIN/END markers |
| Kit repo `AGENTS.md` (source of the block's kit part, 39 lines) | 7,315 | 1.8k | `wc -c` |
| `~/.claude/CLAUDE.md` (Claude Code global) | 9,344 | 2.3k | `wc -c` |
| OpenClaw skills listing | about 18 KB for 100 entries | about 4.5k | [I] entry format observed in this session's prompt: name + absolute path, ~180 bytes each; the prompt states "Skills truncated: included 100 of 126" |
| Tool schemas (ClickUp, Supabase, Engram, OpenClaw built-ins; ~180 tools) | not measured | likely the largest single item | [I] "tool-schema fan-out" is a named cost driver in ADR-0013 Context [V]. I cannot measure the serialized schema block from inside the session. |

Observation [V, from this session]: **a subagent such as this one receives the full workspace AGENTS.md (including the 11 KB managed block and the workspace conventions), the skill listing and the entire tool list.** Per-spawn fixed cost on OpenClaw is therefore at least ~4.3k + ~4.5k tokens before the kit role file is even read. [I] This is a direct, if indirect, argument for fewer spawns.

### 4.2 Loaded when a workflow runs

| Item | Bytes | ~Tokens | Notes |
|---|---:|---:|---|
| `skills/kit-orchestration/SKILL.md` | 21,735 | 5.4k | 199 lines; always loaded by every `workflow-*` skill (their step 1) |
|   lines 49-148 ("Choosing a backend" + "Lane steps") | 14,070 | 3.5k | 64.7% of the file; used only by 2 of 19 workflows (`feature-lanes`, `feature-cross-provider`: `grep -l "backend: lane\|backend: openclaw-agent"`) |
|   everything else (lines 1-48 and 149-199) | 7,665 | about 1.9k | [M] 21,735 - 14,070 |
| `kit-orchestration/references/{domains,handoff}.md` | 4,232 | 1.1k | read on demand |
| `handoff.md.template` | 3,565 | 0.9k | |
| 19 `workflow-*` skills | 28,097 total (avg ~1.5k) | 7.0k total | `wc -c` 49,855 minus kit-orchestration 21,758 |
| Workflow YAMLs (19 + templates) and `WORKFLOW_CONTRACT.md` | 162,093 | 40.5k total | `wc -c`; only the chosen YAML is read |
| Role files: planner 5,481; implementer 4,683; tester 5,052; code-reviewer 4,257; verifier 5,379; software-architect 5,901 | 26,753 for those six | 6.7k | `wc -c agents/roles/*.md` (all 15 roles = 48,028) |
| Domain personas (25 files) | 42,710 (avg 1,708) | 10.7k total | one persona per step, ~0.4k |
| Skill descriptions, all 141 `SKILL.md` | 31,944 (avg 226) | 8.0k | frontmatter `description:` lines; upper bound of a complete native listing |
| All 141 skill bodies | 512,611 | 128k | never loaded together; largest: kit-orchestration 21.7 KB, terraform-maintainer 21.3 KB, flutter-icons 18.7 KB |

### 4.3 Spawn counts of the deterministic loop

[V] `kit:workflows/scripts/kit-plan.js` and `kit-implement.js`.

- `/kit-plan`: 3 parallel readers (`LENSES`, lines 70-83) + 3 drafts (`ANGLES`, 85-89, 119-126) + 2 judges (`judgeCriteria`, 140-150) + 1 plan writer (164-180) = **9 agent calls, always**.
- `/kit-implement`: 1 implement (103) + 1 test (122) + up to 2 fix rounds of 2 calls each (`MAX_TEST_ROUNDS = 2`, line 22, 132-146) + 3 review lenses (153-175) + up to 16 skeptics (`VERIFY_CAP = 16`, 191-193) + 1 fix (219) + 1 sign-off (232). Minimum with no findings: 1+1+3+1 = **6**. Maximum: 1+1+4+3+16+1+1 = **27**.
- Combined, for any change run through the loop: **at least 15 agent calls**, up to 36.
- Under gentle-ai's rules, a small understood change costs **0 subagent calls**; a large change with no writer reason also costs 0 (parent inline with a logbook); a large change with a parallelism reason costs one writer per independent unit plus at most one verifier when a high-risk item is named.

[I] Fixed per-spawn input: kit role (4-6 KB) + persona (~1.7 KB) + preloaded skills + the OpenClaw/Claude base prompt. Taking only role+persona (~1.5-2k tokens) and a Claude Code base prompt, 15 spawns repay several tens of thousands of input tokens in fixed prefixes alone. A real figure requires `ai-resources audit snapshot` around a run (Study 4, measurement plan). **I did not run the loop**, so no measured token total is claimed.

The loop was designed for correctness on non-trivial changes and is defensible for those (parallel lenses, skeptic pass). The gap is that **there is no cheaper path** for a change an agent could finish and verify inline.

### 4.4 Redundancy inside our own injected text [I]

- The managed block lines 1-39 (kit part) and the operator rules both state long-running-command rules; ADR-0013 §3 says the kit CLAUDE.md block "holds the polling guidance once". Worth a dedupe check across `~/.claude/CLAUDE.md` and the AGENTS.md block (not done here).
- The Clusters (WILDBIT), ClickUp, and Questions sections are domain-specific and loaded for every task. They could be on-demand rules (`rules/*.mdc` or skills), but they are *operator rules*; changing them is outside this study's remit and belongs to the operator-rules step (`kit:scripts/ai_resources/setup/operator_rules.py`).

## 5. Comparison

| Concern | Gentle-ai | ai-resources today | Verdict |
|---|---|---|---|
| Base prompt size | 35.9-55.2 KB (OpenClaw), 82.9 KB (Claude) | 11.1 KB managed block (+6.3 KB workspace conventions) | **Ours smaller.** Do not import theirs |
| Lazy loading of procedure | Claude-only pilot, 6.2 KB moved (7.4%) | `references/` pattern, but 14.1 KB of lane machinery inline in kit-orchestration | **Ours has a bigger opportunity, same technique** |
| Skill selection | index + path passing; full descriptions; compact rules rejected | scoped native links, `skills-index.json`, role-preloaded skills, by-path loading | Equivalent; OpenClaw listing not scoped (126 listed, 100 shown) |
| Memory protocol | full or slim; slim only with a verified redundant channel | MCP instructions inject it; kit rules 014/300; plugin directive trimmed | **Ours already on the "slim" side.** Keep it |
| Model routing | none in prompts (runtime config) | profiles, scores, lanes, independence rule | **Ours far ahead** |
| Whether to spawn | decision rules with numbers; zero spawns on small paths | fixed workflow steps; "Decide by task shape" one-liner (`kit-orchestration/SKILL.md:197-199`) | **Main gap** |
| Handoff content | locator + task + spec ids; verbatim request | paths for artifacts; `{{user_goal}}` paraphrase for the requirement | Gap (small, cheap to close) |
| Verification cost | risk-gated; 1 end-of-feature verifier; bounded correction | 3 lenses + up to 16 skeptics + sign-off every run | Gap |
| Measurement | byte ratchets, explicit non-claims | audit snapshot/compare, API-equivalent estimates, honest caveats | **Ours ahead** |

## 6. Candidate savings, with confidence

All estimates are arithmetic on the measurements above; none is a measured saving. Ordering is by effort-to-benefit.

| Candidate | Estimated effect | Basis | Confidence |
|---|---|---|---|
| Split `kit-orchestration` into a core and an on-demand lanes/backends reference | -14,070 B (~3.5k tokens) on 17 of 19 workflow runs | [M] section size; [V] only 2 workflows use it | High on size; zero risk to behavior if the pointer is explicit |
| Size-gated path: inline for small tasks, skipping `/kit-plan` and `/kit-implement` | removes >=15 spawns on small changes | [V] spawn counts; [I] fixed prefix per spawn | High on count; unknown on dollars until audited |
| Risk-gated review (lenses/skeptics only for named high-risk items) | removes up to 3+16+1 calls per run on medium changes | [V] script; gentle-ai rule R10 | Medium: loses the cheap breadth that lenses give; keep lenses for high risk |
| Handoff by reference for the requirement | tens to hundreds of tokens per hop, plus less drift | [I] | Medium on tokens, high on correctness |
| Haiku-tier "small" prompt variants for roles | up to -35% on those role prompts if the same ratio holds | [M] gentle-ai 55.2 -> 35.9 KB | Low: our roles are 0.6-5.9 KB, there is little to cut |
| Dedupe AGENTS.md block vs CLAUDE.md | unknown | [I] | Low until measured |
| Scope the OpenClaw skill listing per agent | up to ~18 KB x every session if listing were cut to the kit's few front-door skills | [I] listing size | Low-medium: depends on an OpenClaw knob I did not verify |
| Do **not** copy Engram protocol into AGENTS.md | avoids +11.7 KB | [V] full section size | High |

## 7. Anti-recommendations (things gentle-ai does that we should not copy)

1. A 36-82 KB always-on orchestrator prompt. [M]
2. Compact skill summaries. They tried; `docs/skill-registry.md:117-124` explains the failure.
3. Byte-pinning tests for prose as a substitute for measuring tokens. Their ratchets are fine as regression guards, but they say nothing about cost. We have a better tool (`audit`).
4. Fan-out by default. They removed their own file-count delegation triggers (1731) after seeing the overhead.

## 8. Open questions

1. How large is the serialized tool-schema block per OpenClaw session, and can it be denied per agent without violating ADR-0011 (global `tools.deny` is never written by the profile) ? ADR-0013 section 5 mentions per-agent `<server>__*` deny in opt-in MCP mirror mode. Needs a measurement with the audit tool, not a guess.
2. What fraction of our real spend is subagent fixed prefix vs reading vs output? `ai-resources audit snapshot` over a window containing `/kit-plan` and `/kit-implement` runs would answer it. Not run here (read-only constraint on spend and time).
3. Does OpenClaw expose a per-agent skill allow-list for the `<available_skills>` listing? Not verified.
