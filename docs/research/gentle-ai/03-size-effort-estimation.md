# Study 3: Task sizing, classification and estimation

Status: research. Nothing is implemented. Date: 2026-10-25. Index: [README](README.md).
Labels: **[V]** verified from code/doc, **[M]** measured, **[I]** inferred or proposed. Gentle-ai paths are relative to `Gentleman-Programming/gentle-ai@5ee1ac7`; `kit:` paths are relative to `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec`.

## 1. Short answer

- [V] **Gentle-ai does not estimate effort.** There is no story point, T-shirt size, time estimate or token forecast anywhere in `odd/`, `skills/`, `internal/assets/` (`grep -rIi "story point\|t-shirt"` returns nothing). It *classifies* instead, and the classification exists to answer one question: **which route is the cheapest safe one?** (inline, one explorer, writers, verifier).
- [V] Its classifier has four inputs: the **resume test** (small vs large), **six high-risk items**, an **evidence budget** (3 calls / ~10k tokens / ~5 lookups), and a **changed-line forecast** that feeds *delivery slicing* only (about 400 lines), never classification.
- [V] **We have no classifier.** Neither `agents/roles/planner.md` nor `agents/roles/software-architect.md` nor `skills/workflow-explore-and-plan/SKILL.md` contains the words estimate, complexity, size or effort (`grep -n -i "estimate\|complexity\|size\|effort"` over those three returned nothing). Our workflows pick their steps by workflow name (feature, bugfix, refactor), not by task size.
- [I] Proposal: adopt the classifier (adapted), add a short **Sizing block** to the planner's output, and add a **calibration loop** (forecast vs actual) that gentle-ai lacks. Time estimates are out of scope until calibrated.

## 2. How gentle-ai sizes work

### 2.1 Resume test, three criteria (Understood, Contained risk, Resumable)

Full text and citations in Study 1, section 3. Summary [V]:

```
small  = Understood AND Contained-risk AND Resumable-from-(request + git diff)
large  = NOT Resumable   (several sessions | waiting on external | separate deliverables
                          the user wants apart | requirements too long to survive compaction)
counts of files / commands / tests / fixes / todo items never decide
```

`routing.go:82-88`; criteria origin `odd/tasks/task-size-canon.md:8` (S2).

### 2.2 What each class buys

| Class | Route [V] | Artifacts | Spawns |
|---|---|---|---|
| small | inline: read, edit, RED, focused test, suite | none unless the user asks (`routing.go:88`) | 0 |
| large, no writer reason | inline, following a logbook | feature doc + Engram mirror + work-unit commits | 0 (except a mapping explorer if the Mapping trigger fires) |
| large + parallelism reason | one bounded writer per independent unit, launched together | as above + route declaration per task | 1 per unit |
| any size + named high-risk item | as above + `Risk: item N` + one independent verifier, ideally at feature end | as above | +1 |

### 2.3 Evidence budget (how "Understood" is proven)

[V] `manifest.go:217-231`, rendered at `routing.go:84,99-108,123`.

| Quantity | Value |
|---|---|
| inline evidence batches | 1 (parallel) |
| inline evidence calls | at most 3 |
| inline evidence tokens | about 10,000 |
| sequential lookups before a mapping explorer | about 5 |
| explorer handoff size | about 2,000 tokens (`path:line`) |
| parent spot checks after an explorer | 1 |
| long-session backstop | about 150,000 parent-context tokens (advisory) |

[I] These are the only *quantified* sizing thresholds in the system. They are expressed in **evidence cost**, not in change size. That is the right unit: it measures how much you must read before you can act, which is what drives spawn decisions.

### 2.4 The 400-line number

[V] Three different uses, easy to confuse:

1. **Planning heuristic per ODD task** (`routing.go:157`): "about 400 authored changed lines (additions plus deletions)" per task; explicitly *not* an acceptance criterion, hard cap, counter-trigger, automatic stop or forced split. Never delete spaces, blank lines or comments, omit tests, or split artificially to meet it.
2. **Delivery slice** (`routing.go:73,174`): commits grouped into reviewable slices of about 400 lines; when the forecast or the running count passes it, apply the delivery strategy (`ask-on-risk`, `auto-chain`, `single-pr`, `exception-ok`).
3. **PR gate** (`skills/chained-pr/SKILL.md:16`): "Split PRs over 400 changed lines unless a maintainer explicitly accepts `size:exception`." Existing repo policy, unchanged by ODD.

Forecast mechanics [V] (`routing.go:174`): "At feature-document creation, forecast authored changed lines ... from the task list, and keep a running count from work-unit commits." Generated files are excluded. Real examples record actuals next to forecasts: `odd/tasks/review-runtime-context-budget.md` ("B ... +467/-5=472. Explicit B size exception; larger than forecast 414-434, no hidden savings" and a total of 1,429 authored changed lines across four commits).

### 2.5 Writer sizing

[V] `routing.go:126`. A unit is "one independent piece of behavior with its code, tests, and docs together". A writer is justified only if the unit needs "several minutes of work (several files or roughly 100+ changed lines; a subagent spends one to two minutes reading before it writes)". Units of about 100 changed lines or less always run inline. This is the only place a *time* appears, and it is a break-even argument, not an estimate.

### 2.6 Risk as a size modifier

[V] The six high-risk items (Study 1, 3.1) do not make a task large; they add a `Risk:` line and a named verifier. "High risk takes a task out of the small path" (`docs/trigger-rules.md`) but "the feature document still comes only from a failed resume test".

### 2.7 What is missing from gentle-ai (opportunities)

1. [V] No forecast-vs-actual tracking beyond ad-hoc notes in logs. No calibration.
2. [V] No cost or time estimate; "Long or very-long work gets one cost/side-effect forecast before launch" (`odd-orchestrator-sections.md`, Native Checking Contract) is a one-line instruction without a rubric.
3. [I] The resume test is a judgment call by the same model that is about to do the work; the only guard is that it must be stated and recorded for large work (route declaration). No second opinion.
4. [V] The classifier is applied once at step 4 and re-evaluated after each mechanism resolves ("re-evaluate task size", `routing.go:121`), but there is no rule for mid-flight reclassification when actual size diverges from the forecast beyond "briefly explain why and continue" (`routing.go:157`).

## 3. What we have today [V]

- Workflows are chosen by **kind** (`workflow-feature-implementation`, `workflow-bugfix`, `workflow-refactor`, ...). Each has a fixed step list; `kit:skills/workflow-feature-implementation/SKILL.md` shows nine steps for every feature regardless of size.
- `/kit-plan` always runs 3 readers + 3 drafts + 2 judges + 1 writer = 9 calls; `/kit-implement` runs at least 6 (Study 2, 4.3).
- The planner role "decomposes work into PLAN.md steps, acceptance criteria, and explicit Tester/Verifier handoff points" (`kit:agents/roles/planner.md` frontmatter). Plan steps and acceptance criteria exist; **size, risk class and route do not**.
- The handoff front matter has `requires_tests` and `security_critical` flags (`kit:handoff.md.template:10-11`) - two coarse risk bits - and `verdicts`. There is no size or route field.
- `kit-orchestration` "Delegating outside a workflow" says "Decide by task shape, not by step or file count" (`kit:skills/kit-orchestration/SKILL.md:197-199`) but gives no shape definitions.
- Our global ClickUp rules require a **priority** on every task and say to ask if unknown (managed AGENTS.md, "Al CREAR una tarea" item 6). **Priority is not size.** ClickUp's `clickup_create_task`/`update_task` also accept `time_estimate` in minutes [V: tool schema], which is currently unused for kit work. [I]

## 4. Proposed rubric for our planner

Goals: (1) decide the route *before* any spawn; (2) be mostly mechanical so two runs agree; (3) record enough to calibrate later; (4) never use line counts as a gate.

### 4.1 Inputs (the planner fills each from a **bounded** read, max ~3 calls / ~10k tokens)

| Id | Question | Answer form | Source of the rule |
|---|---|---|---|
| U | Is the outcome specified, and do I know what and where to change, with no open product/design decision? | yes / no (+ the open decision) | gentle-ai criterion 1 |
| R | Which risk items apply? | list from the table in 4.2, or `none` | gentle-ai six items, adapted |
| S | Resume test: if the session stopped now, could someone finish from the request + `git diff`? | pass / fail (+ which failure shape) | gentle-ai criterion 3 |
| F | Forecast authored changed lines (additions + deletions, generated excluded) | integer, rounded to 10; also per unit | gentle-ai `routing.go:174` |
| N | Number of **independent units** (disjoint edit surfaces; each a behavior with its code, tests, docs) | integer + the surfaces | gentle-ai Writer trigger |
| X | Is there an external wait (CI, review, human, deploy)? | yes / no | gentle-ai large shapes |
| D | Does the user want separate deliverables apart? | yes / no | gentle-ai large shape |

### 4.2 Risk items (adapted to our domains)

Keep gentle-ai's six, with our concrete triggers [I]:

| # | Item | Examples in our world |
|---|---|---|
| 1 | Data or irreversible effects | DB migration, deleting stored data or cloud resources, `terraform apply`/`destroy`, `kubectl delete`, `flux` writes, force pushes |
| 2 | Security | auth, permissions, secrets, IAM, sandbox/tool policy, `tools.deny` |
| 3 | Contracts others consume | public API, CLI flags, config formats, **handoff front-matter keys and hook contracts**, workflow YAML schema, ADR-bound interfaces |
| 4 | Concurrency | locks, races, async ordering, parallel-group joins |
| 5 | Delivery or environment | installers, setup writers, CI/CD, deploy, dependencies, gateway config |
| 6 | No test would catch a regression | no runnable check exists for the change |

Our clusters rule (writes only against `default` or `gke-iac`, explicit `--context`) is an *authorization* constraint, not a risk item; it applies to any size.

### 4.3 Classification (decision table)

Evaluate top to bottom; first match wins. `H` is an orthogonal flag, not a size.

| Class | Condition | Route | Artifacts | Expected spawns |
|---|---|---|---|---|
| **XS** | U and S and F <= ~30 and N = 1 | inline; focused test; no plan file | none | 0 |
| **S** | U and S (any F) | inline; RED -> focused test -> suite once | none (commit optional) | 0 |
| **M** | not S (resume test fails), and N < 2 or per-unit F <= ~100 | inline with a logbook; mapping explorer only if >~5 lookups or evidence over budget | feature doc + mirror + work-unit commits | 0-1 |
| **L** | not S, N >= 2 independent units each > ~100 lines, disjoint surfaces | parallel bounded writers launched together | M's artifacts + route declaration | N (+1 if H) |
| **E** | D (user wants separate deliverables apart) | split into separate tasks, classify each | one task/epic entry per deliverable | per child |
| **H** | any risk item in R | add `Risk: item N`; one independent verifier at feature end | as per the class | +1 |

Rules around the table [I, derived from gentle-ai rules cited in Study 1]:

1. Counts of files, tests, commands, fixes, todo items never classify. F only chooses between XS/S (cosmetic) and feeds delivery slicing.
2. If F > ~400, **do not reclassify**; apply the delivery strategy (slice into reviewable commits/PRs). Reclassifying for line count is the "size-only rework loop" gentle-ai forbids.
3. U = no -> the class is "not decided yet": ask the one product question, or run the mapping explorer, then re-evaluate. Never start writing.
4. `H` never creates a plan-and-fan-out ceremony by itself: it adds one verifier and a Risk line.
5. Re-evaluate once after each mechanism resolves (explorer returned, question answered).

### 4.4 Mapping to our existing machinery

| Class | Today | Proposed |
|---|---|---|
| XS, S | goes through a workflow if the user invokes one | **no workflow**; main agent works inline; no `/kit-plan` |
| M | `/kit-plan` + `/kit-implement` | `/kit-plan` with one draft (skip angles/judges), or a hand-written `odd` feature doc; implement inline; tester gate once |
| L | `/kit-plan` + `/kit-implement` | current 3-angle plan is justified; writers per independent unit via `feature-lanes` |
| H | review lenses always | lenses/skeptics/Opus verifier only when H |

### 4.5 The Sizing block (planner output, also copied into the handoff / feature doc)

```markdown
## Sizing
- class: S            # XS | S | M | L | E   (+ risk flag below)
- resume_test: pass   # pass | fail (shape: sessions | external | separate-deliverables | compaction)
- risk_items: none    # none | [3: changes handoff front-matter keys read by hooks]
- forecast: lines~120 units=1 surfaces=[scripts/ai_resources/verify.py, tests/test_verify.py]
- route: inline       # inline | explorer(reason) | writers(N, reason)
- verification: self  # self | verifier(item N)
- spawn_budget: 0
- evidence_read: 3 calls, ~7k tokens
## Sizing (actual, filled at close)
- lines: +96/-14=110 | spawns: 0 | commits: 1 | reclassified: no
```

The *actual* block is the calibration data. Source of each actual: `git diff --numstat <base>..HEAD` (generated files excluded), subagent count from the run log, tokens from `ai-resources audit snapshot` for the window.

### 4.6 Estimation, kept honest

- **Do not publish time estimates yet.** There is no calibration data and no basis in gentle-ai to borrow. [V]
- After ~10 closed tasks per class, report the **median and 80th-percentile of actual lines, spawns and API-equivalent cost** per class, with the sample size next to every number (ADR-0013 rule c: no saving or cost claim without a measured before/after).
- Only then fill ClickUp's `time_estimate` (minutes) from the class median, and say it is an estimate. [I]
- Keep priority (business) and size (effort) as separate fields. Never infer priority from size (managed AGENTS.md: ask the user when priority is unknown).

### 4.7 Anti-gaming and failure modes

| Risk | Mitigation |
|---|---|
| Planner inflates class to justify a bigger workflow | The Sizing block must state the failing resume-test *shape*; a verifier or the user can challenge it |
| Planner deflates to skip review | Risk items are independent of class; a named item always adds the verifier |
| Forecast drifts | record actuals; reclassify only the **route** when actuals exceed forecast by >2x, never the acceptance criteria |
| Line-count worship | rule 2 above; gentle-ai's own wording forbids size-only rework |
| Judge fatigue | the rubric is 7 questions and a table; no scoring weights to tune |

## 5. Worked examples (using this repository)

| Task | U | R | S | F / N | Class | Why |
|---|---|---|---|---|---|---|
| Fix a typo in one skill description | yes | none | pass | 2 / 1 | **XS** | nothing to resume |
| Add "run spec examples in scratch" paragraph to `agents/roles/verifier.md` | yes | none (prose contract only the verifier reads) | pass | 25 / 1 | **XS/S** | single file, understood |
| Add a new `workflow-odd` skill: YAML + generated SKILL.md + validate_kit + tests + docs | mostly | 3 (workflow YAML schema consumed by `generate`/`validate_kit`) | **fail** (YAML, generated file, tests and docs would not be finishable from a diff alone) | ~300 / 1 | **M + H** | not independent units; one verifier at end |
| Change `handoff.md.template` front-matter keys | yes | **3** (consumed by `kit_handoff_guard`, `kit_subagent_return` hooks and the post-step check) | pass | 40 / 1 | **S + H** | small but a contract |
| Roll out lane changes across `lanes_cmd.py`, `kit-orchestration`, docs and tests as two independent halves | mostly | 3, 4 | fail | ~900 / 2 disjoint surfaces | **L + H** | writers justified by parallelism |
| This study (four documents, read-only research, one repo clone, one worktree) | yes | none | fail (several deliverables, findings live in context until written) | n/a / 4 documents, loosely coupled | **E -> 4x M**, executed inline | 4 deliverables the user wants as separate docs; no writer reason because each doc depends on the others' measurements |

## 6. Where the classifier gets enforced (design note)

Gentle-ai enforces nothing; it relies on prompt text and pinning tests (Study 1, section 1). For us, three levels are available [I]:

1. **Prompt level** (cheapest): short "Task size" and "Delegation" sections in `kit-orchestration` and the AGENTS.md block source. Relies on the model.
2. **Artifact level**: `/kit-plan` writes the Sizing block into the plan file; `/kit-implement` refuses to run without it and uses `class` to choose phases (skip lenses/skeptics when class is XS/S/M and no `H`). This is deterministic, in the JS scripts we already own.
3. **Validator level**: `validate_kit.py` checks that planner/handoff templates contain the Sizing keys; a test asserts that `class` and `risk_items` agree with the route table. Cheap to add once (2) exists.

Order of adoption is in Study 4.
