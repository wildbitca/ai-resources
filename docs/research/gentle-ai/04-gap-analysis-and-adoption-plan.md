# Study 4: Gap analysis and adoption plan

Status: research and proposal. **Nothing is implemented**; no kit file outside `docs/research/gentle-ai/` was changed. Date: 2026-10-25. Index: [README](README.md).
Labels: **[V]** verified from code/doc, **[M]** measured, **[I]** inferred or proposed. Gentle-ai paths: `Gentleman-Programming/gentle-ai@5ee1ac7`. `kit:` paths: `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec` (2.8.4). Tests and scripts named below were not run for this study.

Inputs: [Study 1](01-odd-workflow-deep-dive.md) (ODD rules), [Study 2](02-token-context-economy.md) (measurements), [Study 3](03-size-effort-estimation.md) (rubric).

## 0. Executive summary

1. **What to take:** ODD's *decision rules* (classify before spawning, delegate only for a named reason, hand off by reference, verify by risk, verifier runs the spec's examples). They remove spawns and re-reading, which is where orchestrated work costs money.
2. **What not to take:** gentle-ai's *prompt text* (36-55 KB on OpenClaw vs our 11 KB block), its compact-skill approach (they rejected it), its RDD review system (needs their binary), its Engram protocol copy (we already get it from the MCP server instructions).
3. **Biggest measured lever:** our deterministic loop always spends **>= 15 agent calls** per change (9 in `/kit-plan`, >= 6 in `/kit-implement`), including for changes an agent could finish and verify inline. A size gate removes that for small work. [V][M]
4. **Cheapest wins:** (a) move 14,070 bytes (65%) of `kit-orchestration/SKILL.md` that only 2 of 19 workflows use into an on-demand reference; (b) add short Task Size + Delegation + Authorize sections; (c) make the verifier run authorized spec examples in a scratch copy, which the `/kit-implement` sign-off prompt already asks it to do while its role file forbids it. [V]
5. **Main risks:** the managed AGENTS.md block is overwritten by `ai-resources setup`; generated workflow skills must be edited at the YAML; the handoff front matter is a contract read by hooks; Engram project resolution in worktrees is unverified; the language rule conflicts with ODD's verbatim-quote rule (needs your decision).
6. **Order:** decisions -> docs-only prompt edits -> role/template edits -> `workflow-odd` -> script gating -> measure with `audit snapshot` (section 7).
7. **Effort (forecast, our own rubric from Study 3):** five waves; the first is class S (~150 changed lines, no spawns), the last is class M+H (JS scripts and tests). Details in section 8.

## 1. Constraints that shape every recommendation

| Constraint | Evidence | Consequence |
|---|---|---|
| The managed AGENTS.md block is regenerated | Block header: "managed by `ai-resources setup`; edits inside this block are overwritten" (`~/Development/AGENTS.md:1`; kit `AGENTS.md:1`); end marker line 186 / 39. [V] | Put procedure in skills/templates, change the block only through the kit source, never by hand in the workspace copy |
| Content outside the block is not touched, with one confirmed exception | `kit:docs/decisions/0014-token-cost-round-2.md`, D3.1; AGENTS.md "Content outside that block is never touched" (`kit:AGENTS.md:54`) [V] | Our additions can live next to the block only as operator content, which is the operator's, not the kit's |
| Workflow skills are generated | Header of `skills/workflow-*/SKILL.md`: "Generated from workflows/*.workflow.yaml by `ai-resources generate`. Edit the YAML, not this file."; `scripts/ai_resources/generate.py:219,269-274,291-310` [V] | A new `workflow-odd` is a new YAML plus a `generate` run |
| Skill exposure is policy-controlled | `skill_scope.py` `INCLUDE_IDS`, `LOADABLE_BY_PATH`, `ROLE_SKILLS`; `validate_kit.py` fails an unlinked preload or an unplaced step skill (ADR-0013 section 3) [V] | A new skill must be placed on purpose |
| Tests pin the shape of `kit-orchestration` | `tests/test_kit_orchestration_backend_doc.py:22,77,81` assert the order `## Running a step` < `## Choosing a backend` < `## Lane steps` < `## Parallel groups` [V] | Splitting the lanes text requires updating this test |
| Handoff keys are a contract | Post-step check 3: "the handoff front matter has `step: <id>`, a valid `status` and the keys of `handoff.md.template`" (`kit:skills/kit-orchestration/SKILL.md`, "Post-step check"); hooks `kit_handoff_guard.py`, `kit_subagent_return.py` [V] | New keys must be optional or the validators and tests change together |
| OpenClaw `claude-cli` children get no kit hooks | ADR-0005/0007 (cited in ADR-0014 D7: "The hook does not fire for OpenClaw `claude-cli` children") [V] | Rules in prompts and in orchestrator checks, not in hooks, for those children |
| The main OpenClaw agent delegates all project coding | Managed AGENTS.md, "Delegation": "Hand code and team work to the `claude` worker agent ... Do not do a project's coding in this workspace" [V] | "Inline" in gentle-ai terms means *inside the worker that codes*; the main agent's floor is one delegation, not zero |
| Artifacts are in English | Managed AGENTS.md, "IDIOMA" [V] | Conflicts with verbatim-quote rule, see G14 |
| No cost claim without a measured before/after | ADR-0013 Consequences [V] | Section 7 |

## 2. Gap table

Legend: **Adopt** = take as is. **Adapt** = take the rule, change the form. **Skip** = do not take. Effort uses the Study 3 classes with a changed-line forecast (additions + deletions, tests included) and is a forecast, not a commitment. "Risk" names the main way it can go wrong.

| # | Gentle-ai rule (source) | Ours today (source) | Rec. | Change | Risk | Effort |
|---|---|---|---|---|---|---|
| G1 | Classify with the resume test before anything else; counts never decide (`routing.go:82-88`) | No classifier; workflow by kind; planner has no sizing (Study 3 section 3) [V] | **Adapt** | `kit-orchestration`: new "Task size" section (~1 KB). `planner.md`: Sizing block (Study 3 4.5). Handoff: optional `class`/`route` | Model judgment drifts; mitigated at the script level in G7 | S, ~120 lines |
| G2 | Delegate only for a named reason; none by default; route declared (`routing.go:116-133`, `1731-...` S1) | One sentence, "Decide by task shape, not by step or file count" (`kit:skills/kit-orchestration/SKILL.md:197-199`) [V] | **Adapt** | Replace that section with a short "Delegation triggers" table (ask, mapping, writer-by-reason, verifier-by-risk, long-session); add `route` + `reason` to the handoff notes | Collides with an explicitly invoked workflow (the user chose its steps) - keep: invoking a workflow is the authorization; the gate applies to the *default front door* and to `/kit-*` suggestions | S, ~80 lines |
| G3 | Handoff by reference, never paraphrase; verbatim request when no doc; workers report covered `S#` (`routing.go:73`) | Artifacts by path (`handoff.md.template:12`), but the requirement travels as a paraphrase: step prompt is built from `{{user_goal}}`, "the goal, constraints, and relevant spec paths" (`kit-orchestration/SKILL.md`, "Running a step" item 3) [V] | **Adopt** | Prompt rule: pass locator + task ids + spec ids; read until the Log; report covered ids; with no doc pass the user's request verbatim. Handoff front matter: **optional** `spec_ref`, `task_ids`, `spec_ids` | New keys touch the handoff contract (item 3 risk) | S+H, ~90 lines incl. tests |
| G4 | One feature doc with fixed order Specs / Tasks / Log, `S#` quoting user strings verbatim, `L1` = original request, change rewrites only the affected spec (`routing.go:68,158`; example `odd/tasks/odd-spec-by-reference.md`) | Handoff = ephemeral workflow state, deleted at the end ("When task DONE: DELETE the handoff file"); `templates/feature-spec.template.md` = heavy REQ/RULE/TS product spec [V] | **Adapt** | New `templates/odd-task.template.md` + `workflows/odd.workflow.yaml` (generates `workflow-odd`). Keep it separate from the handoff (durable requirement ledger vs ephemeral run state); `refs.spec` in the handoff points to it | Third document type; people stop writing it. Keep to M-or-larger tasks only | M+H, ~300 lines |
| G5 | Authorize first: read-only unless the request authorizes change; one clarification on ambiguity (`routing.go:62-64`) | Plan is shown before code-writing steps ("Before the first step" item 5); outward actions need confirmation (AGENTS.md); no general read-only-by-default rule [V] | **Adapt** | One bullet at the top of "Before the first step": read-only requests (investigate, explain, compare, propose) produce no writes and no writer delegation | Over-asking; limit to one clarification | S, ~10 lines |
| G6 | Verifier runs the spec's authorized examples in a `mktemp -d` scratch copy, against a baseline, verdict per `S#`, severity rules (`odd-orchestrator-sections.md:13-24`) | `verifier.md` out of scope "running tests (Tester does that)", "Rely on Tester's report" (`kit:agents/roles/verifier.md:20,52`); **but** `kit-implement.js:232-238` tells the sign-off verifier to "run the project's tests and lint ... run the commands the plan names" [V] | **Adapt** | Align the role file with the script: verifier may run *authorized spec example commands* (not the full suite) in a scratch copy, compares with baseline, reports per criterion/`S#`, never edits. Keep independence | Duplicates the tester; bounded by "examples only" and the baseline rule | S, ~60 lines |
| G7 | Verify by risk: passive = readback, medium = writer self-verification, high = one independent verifier at feature end; one correction batch, one recheck (`odd-orchestrator-sections.md:13-24`) | `/kit-implement` always: 3 lenses + up to 16 skeptics + fix + sign-off; test fix rounds <= 2 (`kit-implement.js:22,153-232`) [V] | **Adapt** | Gate the lenses and skeptics on `class`/`H`: XS/S none; M one combined lens; L or H all three + skeptics. Sign-off stays. Bounded correction already exists (rounds <= 2); make it "one batch, one recheck" | Less review coverage on M; tell the user which lenses ran | M+H, ~250 lines JS + tests |
| G8 | RED per requested rule, PRESERVE test per touched command, differential check, scratch outside the worktree (`routing.go:168`) | Implementer "writes the tests ... test first, watch it fail" (`kit-orchestration/SKILL.md:186`); tester is a gate, reviewer audits the test diff [V] | **Adapt** | `implementer.md`/`tester.md`: add PRESERVE and the differential check as two bullets; scratch rule | Test bloat; the rule says "no other cases" | S, ~40 lines |
| G9 | `Risk: item N (reason)` / `Risk: none` on every code change; before `none`, check shared code (`routing.go:94`) | Return block has `Risks: short, or "none"` (`kit-orchestration/SKILL.md:176-182`); handoff `security_critical` bit [V] | **Adopt** | Define the six items (Study 3 4.2) in `kit-orchestration` and make the Return block's `Risks:` carry `item N` or `none` | None material | S, ~25 lines |
| G10 | Engram mirror `odd/<feature>/tasks`, project-scoped, `capture_prompt:false`, read back both writes, mark pending if unavailable, resume order `mem_context` -> `mem_search` -> `mem_get_observation` (`routing.go:165-167`; `protocol.md`) | `Engram_refs` handoff field; rules `014-engram-mcp-protocol.mdc`, `300-engram-memory.mdc`; prompt "when Engram is available: save decisions and gotchas" [V] | **Adapt** | Convention doc + one rule line: use the same topic key for the ODD doc mirror; **file is primary**, mirror best-effort; use only core `mem_*` tools | See R-ENG | S, ~40 lines |
| G11 | Core + on-demand modules (`orchestrator_modules.go`) | `kit-orchestration` 21,735 B always loaded; lines 49-148 (14,070 B) are lanes/backends for 2 of 19 workflows [M] | **Adopt (technique)** | Move "Choosing a backend" + "Lane steps" to `references/backends-and-lanes.md`; core keeps a pointer line per step type: "when a step has `backend: openclaw-agent` or `lane`, read ...". Update `test_kit_orchestration_backend_doc.py` | Models skip the pointer; the post-step checks are safety text - keep a 3-line summary of the Blocked result in the core | S+H(tests), ~60 lines moved/edited |
| G12 | Pass skill paths, not summaries; registry index; `skill_resolution` feedback (`docs/skill-registry.md`) | Prompts carry `SKILL.md` paths (`kit-orchestration/SKILL.md`, step 3); `skills-index.json` (141 entries) [V] | **Skip** (already aligned) | State "paths, never summaries" in the delegation section (one line) | - | in G2 |
| G13 | Capable vs small prompt variants (`orchestrator.go:151-185`) | One prompt per role; Haiku budget check 100k [V] | **Skip for now** | Revisit only if the audit shows Haiku roles above the budget or high cost | - | - |
| G14 | Specs and L1 quote the user verbatim "in the user's language" (`routing.go:158`) | All artifacts in English (managed AGENTS.md, IDIOMA) [V] | **Needs your decision** | Proposed: spec prose in English; verbatim quotes allowed as *quoted evidence* with a language tag, e.g. `> (es, verbatim) ...` | Without a rule, either the verbatim guarantee or the English rule breaks | S, ~10 lines once decided |
| G15 | Work-unit commits ~400 lines, high-risk unit alone, record commit per task, push/PR/merge are the user's (`routing.go:73,174`) | `finishing-a-development-branch`, `common-git-collaboration`; merge to base by default in worktree workflows (`handoff.md.template` `git_integration`) [V] | **Adapt (light)** | Add "high-risk unit gets its own commit" and "record the commit id per task in the doc". Do not adopt the delivery-strategy matrix | Conflicts with `merge_to_base` default; keep ours | S, ~15 lines |
| G16 | No self-pause; hedging is not a stop; one **Needs your decision** result (`routing.go:74-78`) | Blocked/Return_to_step with 3 attempts (`kit-orchestration/SKILL.md:46`) [V] | **Adapt** | Add the "Needs your decision" phrase as the terminal state after the 3 attempts | Low | S, ~8 lines |
| G17 | Sub-agent launch dedupe (`generic/orchestrator.md`) | Not stated [V] | **Adopt** | One sentence in G2: never emit the same `(step, goal)` spawn twice in a turn | None | in G2 |
| G18 | RDD / `gentle-ai review assess` | None; review lenses in the JS loop | **Skip** | Borrow only the six risk items (G9) | Binary-specific | - |
| G19 | Model routing is runtime config, never in prompts; a weaker writer never adds a verifier (`routing.go:124`) | Profiles, scores, lanes, **independence rule** (checker family != writer family, ADR-0007 section 8) [V] | **Skip, with a note** | Keep ours. Reconcile: the verifier *decision* becomes risk-gated (G7); *who* runs it stays independent | Reducing independence would be a regression | - |
| G20 | Byte ratchets as tests (`orchestrator_module_budget_test.go`) | `HAIKU_PROMPT_BUDGET_TOKENS` check; audit snapshot/compare [V] | **Adapt (small)** | After G11, add a ceiling test for `kit-orchestration` core size (bytes), explicitly "a regression guard, not a cost claim" | Test churn | S, ~30 lines |
| G21 | (not in gentle-ai) per-spawn fixed cost on OpenClaw: workspace AGENTS.md 17.4 KB + skills listing + tool schemas reach subagents too (Study 2 section 4.1) | Unscoped; unmeasured tool-schema size [I] | **Investigate** | A measurement task via `ai-resources audit`, then decide on per-agent deny/scoping (ADR-0011/0013 section 5 limits apply) | Out of this study's remit | S investigation |

## 3. Concrete designs

### 3.1 `kit-orchestration` after G1, G2, G5, G9, G11 (target: core < 10 KB)

Proposed outline (names, not final text):

```
## Authorize first                  (G5)  ~0.4 KB
## Task size                        (G1)  ~1.0 KB   resume test, 6 risk items, class table pointer
## Delegation triggers              (G2,G12,G17)  ~1.2 KB
## Locate the kit                   (existing)
## Before the first step            (existing, +size gate)
## Running a step                   (existing; item 3 gains "by reference", G3)
## Backends and lanes -> pointer    (G11)  3-line summary + path to references/backends-and-lanes.md
## Parallel groups                  (existing)
## Subagent return format           (existing; Risks: item N | none, G9)
## Who writes what                  (existing)
## Finishing                        (existing, + Needs your decision, G16)
```

Estimated core: 7.7 KB (current non-lanes text) + ~3 KB new - the old 3-line "Delegating outside" = **~10.5 KB** vs 21.7 KB today. [I] Runs without lanes save ~14 KB (~3.5k tokens) per workflow invocation; the extra ~3 KB is the price of the new rules. Net on a lanes-free run: about **-11 KB (-50%)**. Not measured; Section 7 shows how to measure.

### 3.2 Delegation triggers (the table that replaces `kit-orchestration/SKILL.md:197-199`)

| Trigger | Fires when | Action | Numbers (start with gentle-ai's, calibrate later) |
|---|---|---|---|
| Ask | an open product/design decision | one question, stop | - |
| Mapping | understanding exceeds the inline evidence batch | one read-only `explore` with a `path:line` handoff | inline = 3 calls / ~10k tokens; explorer after ~5 lookups; handoff <= ~2k tokens |
| Writer | >= 2 **independent** units with disjoint edit surfaces, each > ~100 changed lines, launched together; or the parent is near its context limit | one bounded writer per unit | units <= ~100 lines run inline |
| Verifier | the change names a risk item | one independent verifier at the end | independent family stays |
| Never | size, file count, "large task alone", model/effort of the writer | - | one behavior is never split into test/docs/code writers |

Plus: record `route:` and `reason:` per task; never explore files you will read anyway; do not repeat a spawn.

The main OpenClaw agent keeps routing code work to the `claude` worker (managed AGENTS.md); these triggers apply **inside that worker** and in `/kit-implement`.

### 3.3 Handoff by reference (G3), concrete diff shape

`handoff.md.template` front matter gains three **optional** keys under `refs:`:

```yaml
refs:
  plan: ""
  ...
  spec: ""            # path to odd/tasks/<feature>.md (or specs/...), when one exists
task_ids: []          # e.g. [T2]
spec_ids: []          # e.g. [S3, S4]
```

Step prompt rule (replaces "the goal, constraints, and relevant spec paths"): `Spec: <path>, T2, S3-S4. Read until "## Log". Report which S# you covered.` When no doc exists: the user's request verbatim, quoted, and the plan path.

Post-step check 3 stays (keys of the template must exist); because the new keys are optional the check and the hooks must be taught to accept their absence. That is the H part.

### 3.4 `workflow-odd` (G4)

Not a copy of gentle-ai's protocol. A short YAML workflow whose steps are the *decisions*, executed by the main (or `claude` worker) agent, not by seven subagents:

| Step | Who | Output |
|---|---|---|
| 1 authorize | agent | read-only or authorized |
| 2 explore | agent (<= 3 calls) or one `explore` | map |
| 3 classify | agent | Sizing block (Study 3 4.5) |
| 4 track | agent | `odd/tasks/<feature>.md` + best-effort Engram mirror (M or larger only) |
| 5 implement | agent inline, or writers by trigger | commits with Risk lines |
| 6 verify | self, or one verifier (H) | verdict per `S#` |
| 7 close | agent | outcome, failed/pending checks, next step |

Template skeleton (`templates/odd-task.template.md`): exactly the shape in Study 1 section 4.1, with the language note from G14 and a "Sizing" block.

Location of the document is a decision (section 6, Q1): repository-tracked `odd/tasks/` (gentle-ai's choice, discoverable, reviewed in PRs) vs `specs/tasks/` (fits our `specs/` convention) vs `.agent-output/` (ephemeral).

### 3.5 Verifier change (G6), proposed text delta for `agents/roles/verifier.md`

- Replace out-of-scope "running tests (Tester does that)" with: "Do not run the full suite (the Tester does). You may run commands the plan or spec **names as examples**, in a scratch copy under the system temp dir created with `mktemp -d`, never in the workspace, and you leave no new file in it."
- Replace rule "Rely on Tester's report" with: "Re-run one reported command as a spot check; for each criterion or `S#` report pass/fail with the command and observed result."
- Add severity: blocker = reproduces with realistic input and did not reproduce at the baseline; baseline defects and out-of-domain values = advisories; a silently ignored explicit option or an unrequested output change = blocker.
- Keep: no edits, criteria-bound, no-go zones, traceability chain.
- Result is a verdict per `S#`/criterion, written into the Result block and the handoff `verdicts.verification`.

### 3.6 Script gating (G7), sketch

`/kit-plan` grows a first phase "Classify" (the parent computes the Sizing block from three bounded reads). Then:

| class | phases run |
|---|---|
| XS/S | none: print "inline recommended" and the Sizing block; stop |
| M | one reader set (code only), **one** draft, no judges, write plan |
| L/H | current behavior (3 readers, 3 drafts, 2 judges, write plan) |

`/kit-implement` reads the plan's `class` and `risk_items`: XS/S/M without risk items skip the three lenses and the skeptic pass (keep test gate and sign-off); M with a risk item and all L run lenses + skeptics. Deterministic, testable with the existing `orchestrator_fakes.py`.

## 4. Risks and conflicts

### R-BLOCK: managed AGENTS.md block

- [V] The block in `~/Development/AGENTS.md` (lines 1-186, 11,087 bytes) and in the kit `AGENTS.md` (lines 1-39) is overwritten by `ai-resources setup`. After `brew upgrade ai-resources` the user must re-run it (block line 4).
- [I] Any rule added by hand in the block disappears. Rules must live in (a) skills loaded by path or by workflow (`kit-orchestration`, `workflow-odd`), (b) templates, (c) kit source feeding the block (kit `AGENTS.md` and `scripts/ai_resources/setup/...`). I did not locate the single generator for the OpenClaw workspace block (`cockpits/_shared.py:238-361,443` implements the managed-block mechanics; `test_openclaw_workspace_blocks.py` covers it). **Open item**: trace the exact source of the 186-line block before editing anything.
- Plan: keep the block change to at most **three lines** (pointer to `workflow-odd` / `kit-orchestration` "Task size"), and put everything else in skills. Net block growth under 300 bytes.
- The operator-rules section (Clusters, ClickUp, Questions, Language, Commits) is operator content written by `setup`'s step 8 (`operator_rules.py`); this study does not propose touching it.
- Do **not** import gentle-ai's own installer into this host: it also writes managed `AGENTS.md`/`SOUL.md` into the OpenClaw workspace (`docs/agents.md:44`) and would collide with ours. [V] The task constraints also forbid running it.

### R-ENG: Engram MCP

1. [V] **Duplicate protocol.** Our sessions already receive the Engram protocol through MCP server instructions (visible in this session). Gentle-ai injects the *full* 11.7 KB protocol for every adapter that is not Claude Code (`protocol.go:84-125`). Do not copy `protocol.md` into AGENTS.md.
2. [V] **Tool-surface drift.** `docs/engram.md:152-177` lists `mem_stats`, `mem_delete`, `mem_timeline`, `mem_merge_projects`; our Engram server exposes a different set (`mem_compare`, `mem_consolidate`, `mem_doctor`, `mem_judge`, `mem_judge_many`, `mem_review`, `mem_pin`, `mem_session_finalize`, ... and no `mem_stats`/`mem_delete`/`mem_timeline` in this session's tool list). Kit text must name only tools present in our server; confirm per version.
3. [I] **Worktree project resolution.** Gentle-ai documents project detection from the git remote, falling back to the directory name, and says `mem_current_project` must be called before the first read (`docs/engram.md:185-197`). A managed worktree under `~/.openclaw/worktrees/<fingerprint>/<name>` shares the remote with its repo, so detection probably matches; if it falls back to the directory name, the mirror lands in a different project than the repo's. **Verify** with `mem_current_project` from inside a managed worktree before relying on mirrors. The ODD rule "never fall back to all projects" is worth copying.
4. [V] **Availability in children.** Lane agents and `claude-cli` children do not necessarily have Engram or hooks (ADR-0005/0007). Therefore the **file is primary**, the mirror best-effort, with the gentle-ai marker "mirror pending".
5. [V] **Write cost and conflicts.** `mem_save` can return `judgment_required` (conflict surfacing in the MCP instructions); `topic_key` upserts on one key keep that rare. Save the mirror with `capture_prompt: false`.
6. [V] **Plugin patch.** ADR-0013 section 4 already edits the Engram plugin's first-message directive; a plugin update can restore it. Our additions must not depend on the SessionStart payload, which is "not kit-controllable" (ADR-0014).
7. [I] Subagents saving "Key Learnings" (gentle-ai `mem_capture_passive`): we already ask for saves when Engram is available; adopting a closing-block convention is optional and low value.

### R-GEN: generated artifacts and policy

- Edit `workflows/odd.workflow.yaml`, then `ai-resources generate`; never edit `skills/workflow-odd/SKILL.md` (it is regenerated). [V]
- A new skill must be placed in `skill_scope.py` (`INCLUDE_IDS` or `LOADABLE_BY_PATH`) and, if a role preloads it, in `ROLE_SKILLS`; `validate_kit.py` fails otherwise. `workflow-*` already carry `disable-model-invocation: true`, so they do not enlarge the native listing. [V]
- `skills-index.json` is generated (141 entries, `generated_at 2026-10-22`); regenerate after adding. [V]
- Haiku budget: a role prompt that grows above 100k estimated tokens fails validation; our roles are 0.6-5.9 KB, far below. [V]

### R-HOOK: handoff contract

New handoff keys must be optional in `kit_handoff_guard.py`, `kit_subagent_return.py`, the post-step check text, and `test_*handoff*`/`test_workflow_*` tests, or all must change together. Treat this as risk item 3 and give it its own commit.

### R-OPENCLAW: dispatch limits

`openclaw-agent` and `lane` steps need `sessions_spawn` (kit-orchestration, "Availability"); a Claude Code session, a kit role subagent or the `/kit-*` scripts do not have it. The classifier therefore cannot rely on spawning a `claude` worker from inside a script; it must run in whichever process holds the plan. [V]

### R-COMPLIANCE: nobody measures model adherence

Gentle-ai ships no adherence data (Study 1 section 8). Prompt-only adoption (levels 1 in Study 3 section 6) may be ignored. Mitigations: gate in the scripts (level 2) and add validator tests (level 3). [I]

### R-INDEP: independence rule

ADR-0007 section 8 requires a checker's family to differ from the writer's. Gentle-ai says the writer's model never adds a verifier. Both can hold only if risk gates *whether* a checker runs and the independence rule governs *who*. Any change that lets the writer's family verify itself would reverse ADR-0007 and needs a new ADR. [V][I]

### R-SCOPE

ODD bundles authorization, tracking, delivery slicing, RDD, skill registry, persona and Engram protocol. Adopting all of it is a platform migration. This plan takes seven rules and one technique (lazy modules). [I]

### R-LANG

Verbatim quoting vs English-only (G14). Needs a decision before `workflow-odd` is written.

## 5. What this plan deliberately does not do

- No new CLI, no state engine, no enforcement hook for ODD (same position as gentle-ai, `docs/usage.md:95`), except the deterministic gating inside `/kit-plan` and `/kit-implement`, which we already own.
- No change to model profiles, lanes, the audit tool, or ADR-0007/0011/0013/0014 decisions.
- No change to ClickUp conventions. Size and priority stay separate; priority is still asked when unknown.
- No rewrite of the 141 skills, no summary skills.

## 6. Decisions I need from you

| # | Question | Options | My recommendation |
|---|---|---|---|
| Q1 | Where does the durable task document live? | `odd/tasks/<feature>.md` in each repo; `specs/tasks/<feature>.md`; `.agent-output/` (ephemeral) | `odd/tasks/` for tracked repos (compatible with gentle-ai tooling and PR review); `.agent-output/` for non-repo or scratch work |
| Q2 | Language of verbatim quotes | English-only (translate and keep the original in a quote); spec prose English + verbatim quotes tagged with their language | Second option |
| Q3 | Should `/kit-plan` and `/kit-implement` become size-gated by default? | gated by default; opt-in flag `--lean`; keep as is and add a separate lean path | Gated by default, with `--full` to force today's behavior (reversible, one flag) |

(Per the global question rule these should be asked with buttons; I have no question tool in this subagent session, so they are listed for the requester to relay.)

## 7. Measurement plan (ADR-0013 rule: no claim without before/after)

1. **Before any change**: `ai-resources audit snapshot before-odd --since <ISO> --until <ISO>` for a window containing recent `/kit-plan` and `/kit-implement` runs; record spawn counts per run from the run ledger. (Not run in this study.)
2. **Fixed benchmark set**: 6 representative tasks, two per class (S, M, L), replayable on a scratch repository: for each, the Sizing forecast and the actuals (lines, spawns, API-equivalent cost).
3. **After each wave** take a snapshot and run `ai-resources audit compare`. Report medians with sample size; no percentage without n.
4. **Byte guard**: after G11, add the `kit-orchestration` core size ceiling test (G20) as a regression guard only.
5. **Quality guard**: keep a list of known defects the lenses caught (`kit-implement` findings from past runs) and check that class M without lenses would not have missed them; if it would, keep one lens for M.

## 8. Ordering and effort

Effort uses Study 3 classes; forecast lines include tests. "H" = touches a contract (risk item 3). All waves are independent commits on feature branches, merged per `finishing-a-development-branch`; none requires a gateway restart (ADR-0003).

| Wave | Contents | Class | Forecast lines | Spawns | Verification |
|---|---|---|---|---|---|
| **W0** | Answer Q1-Q3; trace the generator of the OpenClaw block; verify `mem_current_project` in a managed worktree; take the baseline snapshot | XS | 0 | 0 | the answers recorded in an ADR stub |
| **W1** (docs/prompts only) | G1, G2, G5, G9, G12, G16, G17 into `kit-orchestration`; G11 split of lanes to `references/backends-and-lanes.md`; update `test_kit_orchestration_backend_doc.py`; G20 ceiling | S+H (test contract) | ~250 | 0 | `scripts/run-tests.sh`; `python3 scripts/validate_kit.py` |
| **W2** (roles, templates) | G6 verifier, G8 implementer/tester, G3 handoff optional keys (+hook/test updates), G15 commit rule | M+H (G3 is item 3) | ~250 | 0-1 (one verifier for G3) | role-template tests; hook tests; validator |
| **W3** (`workflow-odd`) | G4 YAML + template + `generate` + `skill_scope` placement + `skills-index.json`; G10 Engram convention + rule line | M+H | ~300 | 0 | `generate` idempotence; `validate_kit`; new tests |
| **W4** (scripts) | G7 + Classify phase in `kit-plan.js`/`kit-implement.js`, `--full` flag, tests with `orchestrator_fakes.py` | M+H | ~350 | 0-1 | script tests; one live dry run on a scratch repo |
| **W5** (measure) | after-snapshots, compare, calibration table (Study 3 4.6); decide G13, G21 | S | doc only | 0 | audit output attached |

W1 is the highest return for the lowest risk: it needs no new keys, no scripts, and it removes ~14 KB from every non-lane workflow run. W2 and W3 can swap if the user prefers `workflow-odd` first. W4 must come after W1-W3 because it consumes the Sizing block.

Total forecast: ~1,150 changed lines, six commit groups, no required parallel writers (each wave is one understood area; a single worker agent is enough; the parallelism trigger does not fire). [I]

## 9. Acceptance criteria for "adoption done" (draft)

- A small task (class XS/S) in an OpenClaw `claude` worker completes with 0 subagent calls and no durable artifacts, and the Sizing block is stated in the reply.
- A class M task creates one task doc whose `## Specs` quote the request, whose `## Tasks` carry commit ids, and whose `## Log` starts with the original request.
- A handoff from the planner to the implementer contains a locator + `T#` + `S#` and no paraphrase of the requirement.
- The verifier's report contains one verdict per `S#`, produced by running at least one authorized example in a `mktemp -d` scratch copy; the workspace has no new file.
- `kit-orchestration/SKILL.md` core < 11 KB; lanes reference present; all existing tests pass; `validate_kit.py` passes.
- `audit compare` shows spawns per small change reduced vs the baseline, with n >= 5 per class.
- No edit to the managed block beyond the agreed three-line pointer, and a fresh `ai-resources setup` run keeps it.
