# Study 4: Gap analysis and adoption plan

Status: **Waves 1-5 are implemented** on branch `openclaw/claude-gentle-ai-odd-study` (wave 1 in section 10, waves 2-5 in section 11) with the owner decisions of section 6 applied; the open items are closed in section 12 and the calibration data is in section 13. Nothing is merged or installed yet. Date: 2026-10-25. Index: [README](README.md).
Labels: **[V]** verified from code/doc, **[M]** measured, **[I]** inferred or proposed. Gentle-ai paths: `Gentleman-Programming/gentle-ai@5ee1ac7`. `kit:` paths: `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec` (2.8.4). Tests and scripts named below were not run for this study.

Inputs: [Study 1](01-odd-workflow-deep-dive.md) (ODD rules), [Study 2](02-token-context-economy.md) (measurements), [Study 3](03-size-effort-estimation.md) (rubric).

## Owner decisions (2026-10-25, override the recommendations below)

1. **Durable task document reuses `specs/`; no `odd/tasks/`.** Tasks live next to the spec they implement: `specs/features/<feature>/tasks.md` (template `templates/tasks.template.md`), or a `## Tasks` section inside a short spec. The ledger is **temporal**: created for a `light`/`full` change and removed when it closes, after `knowledge-audit` folds what is durable (accepted criteria that became `RULE-*`/`TS-*`, decisions, rejected options) into the spec or `specs/knowledge/decisions/`. Fit with the existing machinery: `specs/features/` stays the canonical truth; `specs/knowledge/` stays the working-evidence area; `tasks.md` is a third, shorter-lived artifact whose closure rule is the same SDD closure `knowledge-audit` already enforces ("data a medias" is the anti-pattern it prevents), so no new lifecycle is invented. `inline` work creates none. Repositories without `specs/` keep only the plan file under `.agent-output/`.
2. **No verbatim-quote rule.** Acceptance criteria stay in English; each carries a short `source:` reference (ticket or message link) instead of a quote. G14 and Q2 are closed this way, and the language conflict (R-LANG) disappears.
3. **Size gating is on by default, in three modes.** `inline` (zero subagents), `light`, `full`; `/kit-plan` and `/kit-implement` classify (one call) or accept `--inline`, `--light`, `--full` / `{"size": ...}`. Delegation needs a named reason, handoff is by reference (locator + `T#`/`S#` ids), and the verifier runs authorized spec examples by risk. Q3 is closed as "gated by default, `--full` restores today's behavior".

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
| G4 | **Revised by owner decision 1:** the doc is `specs/features/<feature>/tasks.md`, temporal, folded by `knowledge-audit` at closure (template in W1); no `odd/tasks/`. Original: one feature doc with fixed order Specs / Tasks / Log, `S#` quoting user strings verbatim, `L1` = original request, change rewrites only the affected spec (`routing.go:68,158`; example `odd/tasks/odd-spec-by-reference.md`) | Handoff = ephemeral workflow state, deleted at the end ("When task DONE: DELETE the handoff file"); `templates/feature-spec.template.md` = heavy REQ/RULE/TS product spec [V] | **Adapt** | New `templates/odd-task.template.md` + `workflows/odd.workflow.yaml` (generates `workflow-odd`). Keep it separate from the handoff (durable requirement ledger vs ephemeral run state); `refs.spec` in the handoff points to it | Third document type; people stop writing it. Keep to M-or-larger tasks only | M+H, ~300 lines |
| G5 | Authorize first: read-only unless the request authorizes change; one clarification on ambiguity (`routing.go:62-64`) | Plan is shown before code-writing steps ("Before the first step" item 5); outward actions need confirmation (AGENTS.md); no general read-only-by-default rule [V] | **Adapt** | One bullet at the top of "Before the first step": read-only requests (investigate, explain, compare, propose) produce no writes and no writer delegation | Over-asking; limit to one clarification | S, ~10 lines |
| G6 | Verifier runs the spec's authorized examples in a `mktemp -d` scratch copy, against a baseline, verdict per `S#`, severity rules (`odd-orchestrator-sections.md:13-24`) | `verifier.md` out of scope "running tests (Tester does that)", "Rely on Tester's report" (`kit:agents/roles/verifier.md:20,52`); **but** `kit-implement.js:232-238` tells the sign-off verifier to "run the project's tests and lint ... run the commands the plan names" [V] | **Adapt** | Align the role file with the script: verifier may run *authorized spec example commands* (not the full suite) in a scratch copy, compares with baseline, reports per criterion/`S#`, never edits. Keep independence | Duplicates the tester; bounded by "examples only" and the baseline rule | S, ~60 lines |
| G7 | Verify by risk: passive = readback, medium = writer self-verification, high = one independent verifier at feature end; one correction batch, one recheck (`odd-orchestrator-sections.md:13-24`) | `/kit-implement` always: 3 lenses + up to 16 skeptics + fix + sign-off; test fix rounds <= 2 (`kit-implement.js:22,153-232`) [V] | **Adapt** | Gate the lenses and skeptics on `class`/`H`: XS/S none; M one combined lens; L or H all three + skeptics. Sign-off stays. Bounded correction already exists (rounds <= 2); make it "one batch, one recheck" | Less review coverage on M; tell the user which lenses ran | M+H, ~250 lines JS + tests |
| G8 | RED per requested rule, PRESERVE test per touched command, differential check, scratch outside the worktree (`routing.go:168`) | Implementer "writes the tests ... test first, watch it fail" (`kit-orchestration/SKILL.md:186`); tester is a gate, reviewer audits the test diff [V] | **Adapt** | `implementer.md`/`tester.md`: add PRESERVE and the differential check as two bullets; scratch rule | Test bloat; the rule says "no other cases" | S, ~40 lines |
| G9 | `Risk: item N (reason)` / `Risk: none` on every code change; before `none`, check shared code (`routing.go:94`) | Return block has `Risks: short, or "none"` (`kit-orchestration/SKILL.md:176-182`); handoff `security_critical` bit [V] | **Adopt** | Define the six items (Study 3 4.2) in `kit-orchestration` and make the Return block's `Risks:` carry `item N` or `none` | None material | S, ~25 lines |
| G10 | Engram mirror `odd/<feature>/tasks`, project-scoped, `capture_prompt:false`, read back both writes, mark pending if unavailable, resume order `mem_context` -> `mem_search` -> `mem_get_observation` (`routing.go:165-167`; `protocol.md`) | `Engram_refs` handoff field; rules `014-engram-mcp-protocol.mdc`, `300-engram-memory.mdc`; prompt "when Engram is available: save decisions and gotchas" [V] | **Adapt** | Convention doc + one rule line: use the same topic key for the ODD doc mirror; **file is primary**, mirror best-effort; use only core `mem_*` tools | See R-ENG | S, ~40 lines |
| G11 | Core + on-demand modules (`orchestrator_modules.go`) | `kit-orchestration` 21,735 B always loaded; lines 49-148 (14,070 B) are lanes/backends for 2 of 19 workflows [M] | **Adopt (technique)** | Move "Choosing a backend" + "Lane steps" to `references/backends-and-lanes.md`; core keeps a pointer line per step type: "when a step has `backend: openclaw-agent` or `lane`, read ...". Update `test_kit_orchestration_backend_doc.py` | Models skip the pointer; the post-step checks are safety text - keep a 3-line summary of the Blocked result in the core | S+H(tests), ~60 lines moved/edited |
| G12 | Pass skill paths, not summaries; registry index; `skill_resolution` feedback (`docs/skill-registry.md`) | Prompts carry `SKILL.md` paths (`kit-orchestration/SKILL.md`, step 3); `skills-index.json` (141 entries) [V] | **Skip** (already aligned) | State "paths, never summaries" in the delegation section (one line) | - | in G2 |
| G13 | Capable vs small prompt variants (`orchestrator.go:151-185`) | One prompt per role; Haiku budget check 100k [V] | **Skip for now** | Revisit only if the audit shows Haiku roles above the budget or high cost | - | - |
| G14 | Specs and L1 quote the user verbatim "in the user's language" (`routing.go:158`) | All artifacts in English (managed AGENTS.md, IDIOMA) [V] | **Skip (owner decision 2)** | Criteria in English with a short `source:` reference (ticket or message link); no verbatim quotes | None: the English rule stays whole | done in W1 |
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

Location of the document was decided (section 6, Q1): `specs/features/<feature>/tasks.md`, temporal. `workflow-odd` (W3) writes and closes that file; it does not introduce a new directory.

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
- [I] Any rule added by hand in the block disappears. Rules must live in (a) skills loaded by path or by workflow (`kit-orchestration`, `workflow-odd`), (b) templates, (c) kit source feeding the block (kit `AGENTS.md` and `scripts/ai_resources/setup/...`). **Closed (wave 3):** the block body is generated by `openclaw_agent_kit_md()` in `scripts/ai_resources/setup/cockpits/_shared.py` (mechanics at `:238-361,443`; `test_openclaw_workspace_blocks.py` covers it); the operator-rules part is appended separately by `operator_rules.py`. The only edit made is one bullet (209 bytes) in that function.
- Plan: keep the block change to at most **three lines** (pointer to `workflow-odd` / `kit-orchestration` "Task size"), and put everything else in skills. Net block growth under 300 bytes.
- The operator-rules section (Clusters, ClickUp, Questions, Language, Commits) is operator content written by `setup`'s step 8 (`operator_rules.py`); this study does not propose touching it.
- Do **not** import gentle-ai's own installer into this host: it also writes managed `AGENTS.md`/`SOUL.md` into the OpenClaw workspace (`docs/agents.md:44`) and would collide with ours. [V] The task constraints also forbid running it.

### R-ENG: Engram MCP

1. [V] **Duplicate protocol.** Our sessions already receive the Engram protocol through MCP server instructions (visible in this session). Gentle-ai injects the *full* 11.7 KB protocol for every adapter that is not Claude Code (`protocol.go:84-125`). Do not copy `protocol.md` into AGENTS.md.
2. [V] **Tool-surface drift.** `docs/engram.md:152-177` lists `mem_stats`, `mem_delete`, `mem_timeline`, `mem_merge_projects`; our Engram server exposes a different set (`mem_compare`, `mem_consolidate`, `mem_doctor`, `mem_judge`, `mem_judge_many`, `mem_review`, `mem_pin`, `mem_session_finalize`, ... and no `mem_stats`/`mem_delete`/`mem_timeline` in this session's tool list). Kit text must name only tools present in our server; confirm per version.
3. [I] **Worktree project resolution.** Gentle-ai documents project detection from the git remote, falling back to the directory name, and says `mem_current_project` must be called before the first read (`docs/engram.md:185-197`). A managed worktree under `~/.openclaw/worktrees/<fingerprint>/<name>` shares the remote with its repo, so detection probably matches; if it falls back to the directory name, the mirror lands in a different project than the repo's. **Verified (wave 3):** `mem_current_project` run from inside the managed worktree `~/.openclaw/worktrees/.../claude-gentle-ai-odd-study` returned `project: ai-resources`, `project_source: git_remote`, `project_path: ~/Development/wildbit/ai-resources`, so a mirror lands in the repository's own project. The convention still says to pass the project explicitly if a worktree ever reports something else. The ODD rule "never fall back to all projects" is worth copying.
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

Closed by owner decision 2: no verbatim quotes, English criteria with a `source:` reference.

## 5. What this plan deliberately does not do

- No new CLI, no state engine, no enforcement hook for ODD (same position as gentle-ai, `docs/usage.md:95`), except the deterministic gating inside `/kit-plan` and `/kit-implement`, which we already own.
- No change to model profiles, lanes, the audit tool, or ADR-0007/0011/0013/0014 decisions.
- No change to ClickUp conventions. Size and priority stay separate; priority is still asked when unknown.
- No rewrite of the 141 skills, no summary skills.

## 6. Decisions (answered 2026-10-25)

| # | Question | Decision |
|---|---|---|
| Q1 | Where does the durable task document live? | `specs/features/<feature>/tasks.md` (or a `## Tasks` section of a short spec), **temporal**: created for the change, folded into the spec by `knowledge-audit` and deleted at closure. No `odd/tasks/`. |
| Q2 | Language of verbatim quotes | No verbatim-quote rule. English criteria with a short `source:` reference. |
| Q3 | Size-gate `/kit-plan` and `/kit-implement` by default? | Yes, three modes (`inline`, `light`, `full`) with `--inline`, `--light`, `--full` overrides; `--full` restores the old loop. |

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
| **W1** (docs/prompts only, **done**, section 10) | G1, G2, G5, G9, G12, G16, G17 into `kit-orchestration`; G11 split of lanes to `references/backends-and-lanes.md`; update `test_kit_orchestration_backend_doc.py`; G20 ceiling | S+H (test contract) | ~250 | 0 | `scripts/run-tests.sh`; `python3 scripts/validate_kit.py` |
| **W2** (roles, templates) | G6 verifier, G8 implementer/tester, G3 handoff optional keys (+hook/test updates), G15 commit rule | M+H (G3 is item 3) | ~250 | 0-1 (one verifier for G3) | role-template tests; hook tests; validator |
| **W3** (`workflow-odd`) | G4 YAML + template + `generate` + `skill_scope` placement + `skills-index.json`; G10 Engram convention + rule line | M+H | ~300 | 0 | `generate` idempotence; `validate_kit`; new tests |
| **W4** (scripts) | G7 + Classify phase in `kit-plan.js`/`kit-implement.js`, `--full` flag, tests with `orchestrator_fakes.py` | M+H | ~350 | 0-1 | script tests; one live dry run on a scratch repo |
| **W5** (measure) | after-snapshots, compare, calibration table (Study 3 4.6); decide G13, G21 | S | doc only | 0 | audit output attached |

W1 is the highest return for the lowest risk: it needs no new keys, no scripts, and it removes ~14 KB from every non-lane workflow run. W2 and W3 can swap if the user prefers `workflow-odd` first. W4 must come after W1-W3 because it consumes the Sizing block.

Total forecast: ~1,150 changed lines, six commit groups, no required parallel writers (each wave is one understood area; a single worker agent is enough; the parallelism trigger does not fire). [I]

## 9. Acceptance criteria for "adoption done" (draft; status after wave 5 in brackets)

- A small task (class XS/S) in an OpenClaw `claude` worker completes with 0 subagent calls and no durable artifacts, and the Sizing block is stated in the reply. [Scripts: 0 calls proven with `--inline`; the worker's adherence needs real runs after install]
- [Met by the dry run for creation and ids; the fold-and-delete at close is the `close` step of `workflow-odd` and was not exercised] A `light` or `full` task creates one temporal `specs/features/<feature>/tasks.md` whose `## Specs` carry a `source:` link, whose `## Tasks` carry commit ids and whose `## Log` starts with the original request link; `knowledge-audit` folds and removes it at closure.
- [Met in the scripts and the engine prompt, tested] A handoff from the planner to the implementer contains a locator + `T#` + `S#` and no paraphrase of the requirement.
- [Prompt and schema in place; behavior needs a real run] The verifier's report contains one verdict per `S#`, produced by running authorized examples (all of them when a risk item is named) in a `mktemp -d` scratch copy; the workspace has no new file.
- [Partly met: core 12,454 B against the 11 KB target, ceiling test at 12,700 B; the rest is met] `kit-orchestration/SKILL.md` core < 11 KB; lanes reference present; all existing tests pass; `validate_kit.py` passes.
- [Not measurable until the branch is installed; see section 11] `audit compare` shows spawns per small change reduced vs the baseline, with n >= 5 per class.
- [Met: one 209-byte bullet; a real `setup` run was deliberately not done on this host] No edit to the managed block beyond the agreed three-line pointer, and a fresh `ai-resources setup` run keeps it.

## 10. Wave 1 result (implemented 2026-10-25)

Branch `openclaw/claude-gentle-ai-odd-study`. [M] = measured.

| Item | Before | After |
|---|---|---|
| `skills/kit-orchestration/SKILL.md` (always loaded) [M] | 21,758 B | 12,110 B (-9,648 B, -44%); target was < 11 KB, missed by about 1.1 KB because the Task size, Delegation and Tasks sections add about 4.4 KB |
| `references/backends-and-lanes.md` (on demand) [M] | n/a | 14,266 B, read only for `openclaw-agent` and `lane` steps |
| `planner.md` / `verifier.md` [M] | 5,481 / 5,379 B | 7,425 / 6,091 B |
| `/kit-plan` agent calls [M, test harness] | 9 | `inline` 0 (explicit) or 1 (classified); `light` 3 (+1 classify); `full` 9 (+1 classify) |
| `/kit-implement` agent calls [M, test harness, clean run] | 6 | `inline` 0; `light` 3 (4 with a risk item); `full` 6 |

Closed gaps: G1, G2, G3 (prompt level), G5, G6, G9, G11, G12, G14 (skipped by decision), G16, G17, G20, and G7 in its minimal form. `audit snapshot before-wave1` is stored; a before/after cost claim needs real runs on the new kit, so none is made yet.

## 11. Waves 2-5 result (implemented 2026-10-25)

Same branch. [M] = measured on this branch against `ac89fe9` (the pre-wave-1 base) with `wc -c` or the node harness; [V] = verified by a command run in this session.

| Wave | What changed | Commits |
|---|---|---|
| W2 | G8: implementer gains RED per requested rule, PRESERVE, differential check (scratch copy via `mktemp -d`, outside the workspace) and a commit rule (G15); tester becomes a gate check (confirm RED and PRESERVE exist, separate new from pre-existing failures on the base revision) | `docs(roles)` |
| W2 | G3 contract: optional `refs.spec`, `task_ids`, `spec_ids`, `class`, `route` in `handoff.md.template` and `references/handoff.md`; `engine.py` (`HANDOFF_KEYS`, merge, one "Work by reference" prompt line, only when set), `reply.py` (new reply keys), SubagentStop hint (`Covered:`); `tests/test_handoff_optional_keys.py` pins that a plain run renders none of them and that both hooks ignore the new keys | `feat(handoff)` |
| W3 | G10: `references/task-ledger.md` (temporal lifecycle, Engram mirror: key `kit/<feature>/tasks`, two writes, ids and paths only, core `mem_*` tools only), one line in `rules/300-engram-memory.mdc`, `execution_hints.inline` documented in `WORKFLOW_CONTRACT.md` and `kit-orchestration` | `docs(kit-orchestration)` |
| W3 | One bullet in the managed block (`_shared.py`) | `feat(openclaw)` |
| W3 | G4: `workflows/odd.workflow.yaml` (4 steps), generated `skills/workflow-odd/`, `skills-index.json` (142 skills), `tests/test_workflow_odd.py` | `feat(workflows)` |
| W4 | G7 rest: `scripts/ai_resources/plan_sizing.py`, `ai-resources plan-check`, `validate_kit.py` pass over the shipped examples; `kit-plan.js` returns `task_ids`/`spec_ids` and asks for `plan-check`; `kit-implement.js` takes `tasks_path`/`task_ids`/`spec_ids`, by-reference prompt, RED/PRESERVE/differential/commit rules, sign-off runs authorized examples in a scratch copy with a verdict per `S#`, `light` gets one correction round | `feat(validate)`, `feat(workflows)` |
| W5 | Snapshot, calibration, decisions (sections 12-13); conftest exemption narrowed | `test(guard)`, `docs` |

### Measured before and after

| Item | Before (`ac89fe9`) | After |
|---|---|---|
| `kit-orchestration/SKILL.md` (always loaded) [M] | 21,758 B | 12,454 B (-9,304 B, -43%); lanes text 14,266 B on demand; ledger text 2,526 B on demand |
| Managed block, kit part [M] | 3,611 B | 3,820 B (+209 B; the plan allowed under 300) |
| `planner.md` / `implementer.md` / `tester.md` / `verifier.md` [M] | 5,481 / 4,683 / 5,052 / 5,379 B | 7,425 / 5,619 / 5,418 / 6,091 B (+3,938 B across the four, loaded only by the role that runs) |
| `handoff.md.template` [M] | 3,565 B | 4,161 B |
| `/kit-plan` agent calls [M, harness] | 9 | `inline` 0 (1 when classified), `light` 3 (+1), `full` 9 (+1) |
| `/kit-implement` agent calls, clean run [M, harness] | 6 | `inline` 0, `light` 3 (4 with a risk item), `full` 6 |
| `/kit-implement` correction rounds on a red suite [M, harness] | 2 | `light` 1, `full` 2 |
| Live dry run, scratch repo, `light` [M] | n/a | plan 3 calls, implement 3 calls, 0 findings, ledger checked by `plan-check`, RED failed with the right error before the fix, 1 commit recorded on `T1` |

The dry run used the real scripts and a real scratch git repository (`~/.openclaw/scratch/odd/dryrun`) with a deterministic stand-in for every `agent()` call. It proves the script flow, the by-reference prompts and the checker on real files; it does not call a model, so it says nothing about model adherence or cost.

### What the audit snapshots can and cannot say

`audit snapshot after-wave5 --days 7` is stored next to `before-wave1` and `after-wave1`. Between `after-wave1` and `after-wave5` the 7-day window slid, and the host's installed kit (`/home/linuxbrew/.linuxbrew/opt/ai-resources`, 2.8.4) is unchanged, so the small differences (cost -1%, first-call context -7%) come from older sessions leaving the window, not from this branch. No cost claim is made. The comparison becomes meaningful after the owner installs the branch and runs at least 5 changes per mode (section 9, last criteria).

### G21 measurement: fixed context per spawn

From `after-wave1.json` (window 2026-10-18 to 2026-10-25, `first_call_ctx_median`, tokens, n = sessions): kit Claude Code subagents 21,488 (n=203); OpenClaw `claude-cli` children 46,492 (n=52); OpenClaw main sessions 96,483 (n=268); Claude Code main 56,940 (n=7). An OpenClaw child therefore starts with about 2.2 times the context of a kit subagent before it reads a line of the task, which is the strongest evidence in this study for the size gate: each avoided OpenClaw spawn avoids roughly 46k tokens of fixed prefix on top of the work. Scoping the OpenClaw children's tool and skill listings would cut that prefix, but it is a live gateway configuration change under ADR-0011/0013 limits and out of this change set (decision in section 12).

## 12. Open decisions, closed

| Item | Decision | Rationale |
|---|---|---|
| G13 capable vs small prompt variants | **Skip** | No role prompt is near a budget (largest role 7.4 KB; `check_haiku_budget` passes against 100k tokens); the Haiku calls above 100k context in the audit (22%) come from session history, not from the role prompt, so a smaller variant would not move them. Revisit only if a Haiku role file passes 20 KB |
| G21 per-spawn fixed cost | **Investigated, measured (section 11), no scoping change here** | The number justifies the gate already shipped; changing which tools and skills OpenClaw children list needs a live config change and its own before/after, so it is a follow-up for the owner |
| Correction rounds on a red suite | `light` 1, `full` 2 | "One batch, one recheck" (G7) for the cheap path; `--full` keeps the previous behavior |
| Where the plan-sizing check lives | A pure module, a `plan-check` command and a validator pass, plus a prompt line asking the plan writer to run it | The scripts run in a sandboxed runtime that cannot import Python or read files; an agent can run a command and the validator pins the shipped examples |
| `workflow-odd` step count | 4 steps (`classify`, `implement`, `verify`, `close`), not the 7 of section 3.4 | Authorize, map and classify happen in one read-bounded step; every step is `inline: true`, so fewer steps means fewer places to spawn. The orchestrator CLI ignores the hint and dispatches every step, which is documented in `WORKFLOW_CONTRACT.md` |
| New skill placement for `workflow-odd` | No `skill_scope.py` entry | The `workflow-` prefix already links it, and generated workflow skills carry `disable-model-invocation`, so the native listing does not grow. The planner and verifier steps name `common-system-design` and `common-protocol-enforcement` only so the role preloads (an intersection over all steps of the role, checked by the validator) stay whole; the prompts say when to actually read them |
| Engram mirror | Key `kit/<feature>/tasks`, project scope, `capture_prompt: false`, two writes (create, close), ids and paths only, core tools only, `judgment_required` resolved as `related` | Least consumption that still lets a later session resume; the file stays primary |
| Core size ceiling | Raised from 12,500 to 12,700 B | Wave 2 and 3 pointers added about 340 B to a core already 390 B under the old ceiling; the ceiling is a regression guard, not a cost claim |
| `node_scripts` conftest exemption | Narrowed (section 14) | The first version let every `node` call through in a marked test |

## 13. Calibration (Study 3, section 4.6)

Forecast is the line count in section 8; actual is authored changed lines (additions plus deletions, tests included; generated files, the digest fixture and research docs excluded), from `git diff --numstat` per wave. Sample size is the number of waves, so these numbers are a record, not an estimate; no median or percentile is published below n = 10 per class, and no `time_estimate` is derived from them.

| Wave | Class (forecast) | Forecast lines | Actual lines | Spawns | Reclassified |
|---|---|---|---|---|---|
| W1 | S+H | ~250 | 671 (about 14 KB of that is the lanes text moved, counted once as added and once as deleted) | 0 | no |
| W2 | M+H | ~250 | 165 | 0 | no |
| W3 | M+H | ~300 | 188 (plus 1,509 B generated skill and the index entry) | 0 | no |
| W4 | M+H | ~350 | 293 | 0 | no |
| W5 | S | doc only | 61 (conftest narrowing and its test; the rest is documentation) | 0 | no |

Observation (n = 4): code and test waves came in 16-35% under forecast; the one overshoot was a move of existing text, which a line-count gate would have misread. That supports the rule in Study 3 that line counts never gate a mode.

## 14. Review of the `node_scripts` conftest exception

The wave 1 exemption removed `node` from `HOST_BINARIES` for every test carrying the marker, so a marked test could run any node program. It was scoped (marker only, one test at a time through `monkeypatch`), but wider than needed. It now allows exactly `node tests/kit_script_harness.mjs <file under workflows/scripts> <config>`; `node -e`, a script outside `workflows/scripts`, a different argument count and a shell wrapper stay refused, and an unmarked test cannot run node at all (`tests/test_node_harness_guard.py`).

## 15. Remaining risks

- **Prompt-level adoption is unmeasured.** The scripts gate spawns deterministically; the role and skill rules rely on model adherence. The plan-check validator and the harness tests catch the shape, not the behavior. Measure after install (section 9).
- **`inline` is honored by an agent, not by the orchestrator CLI.** `ai-resources orchestrate` dispatches every step of `workflow-odd`, so running that workflow through the CLI spawns four subagents; the intended route is an agent running it in-session.
- **Managed block growth** is 209 B per OpenClaw workspace and reaches hosts only through `ai-resources setup`; until then the pointer line is absent and everything else still works.
- **The sign-off prompt now asks the verifier to run examples**, which adds shell work to that role; its independence rule (a family different from the writer's) is unchanged.
- **Engram mirror conflicts** surface as `judgment_required`; the convention says to resolve silently, which depends on the model following it.
- **52 pre-existing test failures** on the base also fail here, the identical set; they are not related to this branch (section 16).

## 16. Test results

Full suite, `python -m pytest -m "not timing" tests/` plus the `timing` pass, in the `kit-test` virtualenv, base tree `ac89fe9` (a detached worktree under `~/.openclaw/scratch/odd/base-tree`) against this branch at its final commit. [M]

| | Base `ac89fe9` | This branch |
|---|---|---|
| Main pass | 52 failed, 10,033 passed, 19 skipped | 52 failed, 10,080 passed, 19 skipped |
| Timing pass | 221 passed | 221 passed |
| Failing tests only in one side | - | none (`comm` of the two sorted lists is empty) |

The 52 failures are the same on both sides: `test_openclaw_doctor_drain` (25), `test_openclaw_config_watch` (12), `test_openclaw_restart_gate` (7), `test_openclaw_host_wizard` (3) and one each in `test_state_merge`, `test_setup_idempotence_round4`, `test_openclaw_team_resilience`, `test_openclaw_journal_hostile` and `test_openclaw_cost_migrations`; they concern the host's OpenClaw setup and watch paths, not this work. The owner's brief counted 51; in this environment the base fails 52, so one of them is probably environment- or timing-sensitive. The branch adds 47 passing tests and `validate_kit.py` passes (142 skills, 15 roles, 25 personas, 20 workflows). A first full run of this branch failed two tests the branch had introduced (the workflow count in `test_orchestrator_workflows.py` and the committed repo `AGENTS.md` block, which must match the renderer after the new bullet); both were fixed in a later commit and the run above is the second one.


## 17. Adaptive sizing: the run ledger and one measured experiment (2026-10-26)

[V] = verified by a command; [M] = measured; [I] = inferred.

### What telemetry exists today, and what does not

| Source | Gives | Gap |
|---|---|---|
| `audit_sources` transcripts (Claude Code JSONL, OpenClaw events) [V] | per call: time, session, model, prompt/cache/output tokens, so tokens and API-equivalent USD for any window or session id | no notion of "a workflow run", of its size, or of a verifier sending work back; a window also catches concurrent sessions (an upper bound) |
| `orchestrate` run store (`state.json`, `orchestrate cost`) [V] | steps, agents, `returns` (send-backs), start and end, cost by role | exists only for runs the Python engine drives; no run has ever been stored on this host (`~/.local/state/ai-resources/runs` is absent), and inline `workflow-odd` runs never reach it |
| `subagents list` (OpenClaw tool) [V] | runtime and a `totalTokens` per child | `totalTokens` is the last prompt size, not the sum of the child's calls (a child with 5 calls printed 94,591), so it understates cost |
| Engram [V] | free-text memories | MCP only, no Python reader, nothing structured |
| ai-usage report | host-level usage and credit alerts | not per run |

Decision: a small append-only ledger (`ai-resources runs`, `scripts/ai_resources/run_ledger.py`) that the closing step of `workflow-odd` fills, with tokens and USD joined from the transcripts by time window and session ids, and structure joined from `state.json` when a run came from the engine (`--from-run`). Nothing in OpenClaw core changes. Not measurable: tokens per step of an inline run (one session), and a child that reports no session id.

### Experiment: same task, `/equipo`-style full workflow against `workflow-odd` inline

Task (both arms, scratch clones of this tree): add `--mode` and `--outcome` filters to `ai-resources runs list` with one test. Both arms produced a passing 11-test file (15 and 17 changed lines in two files).

Arm A emulates `/equipo` (`feature-implementation`): I acted as the runtime and spawned its nine steps one at a time as isolated child sessions (`research, plan, architect, implement, test, review, security, verify, document`), each given its real prompt template. The chat command and the orchestrator CLI could not be driven from this session, so this is an emulation of the dispatch with the real step prompts, not a run of the plugin. A mistaken extra spawn (step 2 started twice) was cancelled after one call and is excluded from the figures below. Arm B is the `odd` route done inline by the session that already held the context.

| | A: full, 9 spawned steps | B: odd, inline |
|---|---|---|
| Wall time [M] | 254 s | 10 s |
| Model calls [M] | 59 | 2 |
| Prompt tokens (fresh + cache) [M] | 5,551,725 | 396,532 |
| Output tokens [M] | 18,742 | 1,416 |
| API-equivalent USD [M] | 2.09 | 0.06 |
| Rework / outcome | 0 / pass | 0 / pass |

Ratios: 35x USD, 14x prompt tokens, 25x wall time. Caveats, all real: n = 1 per arm, so these are anecdotes, not a calibration; the full arm wall time includes the load of a concurrently running test suite; B is the marginal cost of a session that already held context (a cold `odd` would add one child spawn, about the 0.2 USD of a single full-arm step); the parent's own orchestration calls in arm A are not attributed; the nine steps were run with a "be economical" instruction, so a real `/equipo` run on this task may cost more, not less.

### G21 follow-up: the fixed context per spawn, measured

Every child's first call carried 91,226-91,242 prompt tokens (nine children, a spread of 16 tokens) before reading its task [M]. That is twice the 46,492 median recorded for OpenClaw `claude-cli` children in section 11, so the fixed prefix of a child spawned from this agent is larger than that cohort's median [M]; the cohorts differ (this agent's workspace instructions and tool listing), so the two numbers are not a trend.

Each child made 5-9 calls and re-read that prefix from cache on every one. Computed from the figures above, 59 calls x about 91.2k = 5.38M of the 5.55M prompt tokens (97%) are the repeated fixed prefix; the task-specific part is about 3% [I, arithmetic on measured values]. Cached reads are cheap per token, which is why the full arm costs 35x rather than 100x, but the prefix is what a spawn costs before it does any work. Scoping which tools and skills OpenClaw children list is still the lever, still a live configuration change under ADR-0011/0013, and still not done here; the ledger will show its effect, because `first_call_ctx` per child is in the transcripts.

### Decision

The numbers support the existing direction (do not spawn for a one-unit, two-file change) and do not support a lower floor for risky work; they are one task. Thresholds stay as shipped. The ledger and the calibration report decide any change after at least 5 runs per mode, and the owner approves it.

### Status: study closed (2026-10-26)

No further paid experiments. Calibration now comes from the run ledger fed by ordinary `workflow-odd` runs; the first report is due after at least 5 runs per mode, and any threshold change needs owner approval. The one open item is G21 (scope what OpenClaw children list), which needs a live config change and its own before/after.
