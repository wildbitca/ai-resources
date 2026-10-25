# Study 1: Organic Driven Development (ODD) deep dive

Status: research, read-only on gentle-ai. Nothing here is implemented in ai-resources.
Date: 2026-10-25. Part of the gentle-ai study; see [README](README.md) for the index.

## 0. Provenance and evidence labels

- Source: `Gentleman-Programming/gentle-ai`, `main` at commit `5ee1ac7` (shallow clone, 2026-10-25; docs banner says the docs track `main`, last release v4.0.0). Cloned into a scratch directory, never installed or run.
- Citation form: `path:line` is relative to the gentle-ai repo at that commit. URL base: `https://github.com/Gentleman-Programming/gentle-ai/blob/5ee1ac7/`.
- Labels used throughout the study:
  - **[V]** verified by reading the code or doc named in the citation.
  - **[M]** measured by me with a command (command given).
  - **[I]** inferred; reasoning stated. Treat as a hypothesis.
- Our side is cited as `kit:<path>`, relative to `/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec` (ai-resources 2.8.4).

## 1. What ODD is, in one paragraph

[V] ODD is **prompt guidance, not an engine**. `docs/usage.md:95` says it outright: "ODD adds shared agent guidance, not a new CLI, state engine, or mandatory planning phase. Instruction tests establish delivery, not autonomous compliance with every create/update/resume step." The canonical text lives in one Go function, `RenderRouting` (`internal/components/agentguidance/routing.go:47-194`), which renders the same block into the instruction file of every supported runtime (OpenClaw included, see `docs/agents.md:24,44`). SDD (Spec-Driven Development) was retired in v4.0.0; ODD is now the only implementation workflow (`docs/trigger-rules.md:14-16`).

Consequence [I]: every rule below is a request to the model. Gentle-ai enforces nothing at runtime except through pinning tests on the rendered text (`routing_test.go`, 1,163 lines) and, for review, a native CLI (`gentle-ai review assess`). Compliance depends on the model.

## 2. The protocol: seven steps, in order, on every request

Rendered from `routing.go:58-80` (non-Pi runtimes) and mirrored in prose at `docs/usage.md:14-24`.

| # | Step | Exact rule [V] | Source |
|---|------|----------------|--------|
| 1 | **Authorize** | Decide whether the request authorizes a change. Investigation, explanation, review, audit, comparison and proposal/planning-only requests are read-only: no file writes, no writer delegation, no implementation artifacts. Ambiguous or conditional change intent gets **one** clarification, then stop and wait. | `routing.go:62-64`; Pi variant adds "a user saying they may stop you or resume later asks for notes and separate commits, not a stop" `routing.go:24` |
| 2 | **Explore** | Explore existing code and requirements first, proportionately. Explore through a subagent only for a map the parent needs to *decide or route*, never to read files the parent will read anyway before writing inline. | `routing.go:65` |
| 3 | **Resolve uncertainty** | Optional research only for a *named* uncertainty; one focused user question only for a real unresolved product decision (then stop and wait); at most **one** read-only assumption challenge for a high-consequence unproven premise. | `routing.go:15` |
| 4 | **Classify** | Small vs large by the resume test (section 3). Counts of files, commands, tests, fixes, or a requested todo list never decide. | `routing.go:67` |
| 5 | **Track before the first write** | Large work only: create `odd/tasks/<feature>.md` plus its Engram mirror `odd/<feature>/tasks` automatically, before the first source write, without asking permission; tell the user in one line which document and how many tasks. | `routing.go:68` |
| 6 | **Implement task by task** | Hand off by reference; test-first by default; update docs/help for changed commands; check items off only with observed proof; commit work units in slices of about 400 authored changed lines. | `routing.go:73` |
| 7 | **Close** | Report the verified outcome, every failed/skipped/pending check, and the next step. End every code change with a `Risk:` line. Never end with a tracked task pending unless the user explicitly stopped; otherwise emit one **Needs your decision** result. | `routing.go:74-78` |

Resume of an interrupted feature: `mem_context`, then project- and feature-scoped `mem_search`, then `mem_get_observation` for the full document, then read the task file itself, then reconcile before continuing (`routing.go:80`, `usage.md:34`).

Public progress vocabulary is only four states: **Working**, **Checking**, **Ready**, **Needs your decision** (`docs/trigger-rules.md`, "Native progress and authority").

## 3. Classification: the resumability test

[V] `routing.go:82-88`, `docs/usage.md:21`, spec S2 in `odd/tasks/task-size-canon.md:8`.

A task is **small** only when all three hold:

1. **Understood.** The outcome is specified, what and where to change is known, no product or design decision is open, and this is provable "within one bounded read batch" (at most 3 calls and about 10k tokens; manifest `MaxEvidenceCalls: 3`, `ApproxEvidenceTokens: 10000`, `internal/agents/capabilitymanifest/manifest.go:217-219`).
2. **Contained risk.** It touches none of the six high-risk items (section 4).
3. **Resumable from the diff.** The resume test: "if the session stopped now, someone could finish the work from the original request and `git diff` alone."

A task is **large** *only* when the resume test fails. The text gives four failure shapes: several sessions, waiting on something external, separate deliverables the user wants apart (never several features asked at once), or requirements too long to survive compaction (`routing.go:87`).

What the classification explicitly does **not** use: number of files, commands or tests, fixes, a requested todo list, or changed lines (`routing.go:87`, `usage.md:21,28`). A one-line cross-cutting rename and a 30-file mechanical edit can both be small if the three tests hold.

**Small path** (`routing.go:88`): work inline; read and edit, several files of one understood change included; observe RED inline before the fix; run the focused test and the suite inline, once each. No explorer, worker or verifier; no feature document, mirror or commits unless the user asks; a todo list is optional.

**Large path:** tracking (feature doc + mirror + work-unit commits). A large task gets a *writer subagent* only by the Writer trigger (section 6); otherwise the parent works inline following its logbook (`routing.go:87`).

Re-evaluation: "after [a mechanism] resolves, re-evaluate task size and stay small while the three criteria hold" (`routing.go:121`).

### 3.1 The six high-risk items

[V] `routing.go:89`, refined in `odd/tasks/task-size-canon.md:10` and `1731-parity-delegate-for-reason.md:11` (S3).

1. Data or irreversible effects that cannot be rebuilt: migrations, *changing or deleting* existing stored data, persisted-format changes. Not "saving new records", not a dev/test database a seed or backup rebuilds.
2. Security: auth, permissions, credentials, secrets, guards, sandbox.
3. Contracts others consume: changing or *removing* public API, CLI flags, config formats, published exports, mirrored prompts. Not adding a flag/command/optional field. Requested changes are not high-risk by themselves; *unrequested breaks in shared code* are.
4. Concurrency: locks, races, async ordering.
5. Delivery or environment: installers, release, CI, deploy, dependencies.
6. No test would catch a regression in what changes.

"Unclear" counts as high only when a bounded look cannot tell whether items 1-5 apply.

**Risk line:** every code change, small path or delegated, closes with `Risk: item N (reason)` or `Risk: none`. Before writing `Risk: none`, check whether the diff changes code that behavior the request did not mention also uses (shared options, parsers, helpers); if so it is item 3 (`routing.go:94`).

Escalation (RDD runtimes only): `gentle-ai review assess --escalate-item <1-6> --escalate-reason "<one line>"` raises a native tier to high and never lowers it (`task-size-canon.md:11`, `routing.go:178`). Not portable to us (needs the gentle-ai binary).

## 4. The feature document

### 4.1 Format (fixed order)

[V] `routing.go:68`, `routing.go:158`, `docs/usage.md:26`. Stable content first, growing log last, so a worker can stop reading at `## Log`.

```
# <Feature title>                       <- header: 2-3 lines
Branch · Delivery strategy · Runner     <-    objective, branch, delivery, test runner
Engram mirror: odd/<feature>/tasks · Route: inline|delegated (+ reason)

## Specs
S1. <requirement; user's exact strings, error messages, commands, examples verbatim>
S2. ...

## Tasks
- [x] T1 (S2-S5) inline · <scope> · RED→GREEN · <commit sha>
- [ ] T3 (S1) pending merge · <what is outstanding>

## Log
L1 <date> user (verbatim): > <original request, untouched>
L2 <date> user (verbatim): > <later correction>
L3 <date> T1 evidence (risk: medium): RED <test> failed ...; GREEN <command> ok ...
```

Rules [V]:

- `## Specs`: numbered `S#`, "quoting the user's exact strings, error messages, commands, and examples verbatim ... in the user's language ... never summarize or reword those fragments"; "do not add requirements the user never asked for, and mark assumptions as assumptions" (`routing.go:158`).
- `## Tasks`: one line per task with a stable ID, linked `S#`, route (inline or delegated), status, and the commit identity or RED/GREEN evidence.
- `## Log`: `L1` is the user's original request verbatim; later user corrections are logged verbatim with a date; problem analysis, rationale for meaningful changes, verification evidence, progress and next step are appended. "Routine corrections stay brief; no exhaustive decision journal."
- **Change handling:** a requirement change appends its verbatim Log entry, rewrites only the affected `S#`, and reopens only its linked task. The origin of this rule is the user's remark quoted at `odd/tasks/odd-spec-by-reference.md:8`: "unlike SDD, a change does not redo everything, only that spec and its associated task" (translated from the Spanish original). Findings alone never authorize scope expansion; business scope changes need the user.
- **Checkboxes** are not approvals: "Check off only observed outcomes with applicable proof" (`routing.go:159`).

### 4.2 Real examples (all in the repo, 3,443 lines across 53 files in `odd/tasks/`, [M] `wc -l`, `ls | wc -l`)

- `odd/tasks/odd-spec-by-reference.md` (35 lines): the cleanest instance. Six specs, five tasks with commit shas, eight log entries including a review finding that changed the design (L7-L8).
- `odd/tasks/task-size-canon.md` (23 lines): same shape, includes a "limit" note in L3 (what the change does *not* do).
- `odd/tasks/1731-parity-delegate-for-reason.md`: shows the *reason* field for delegation on every task ("route: delegated writer; reason: 2+ non-trivial files").
- `odd/tasks/review-runtime-context-budget.md` (295 lines): the failure mode; a long-lived doc becomes a narrative. The format rule "no exhaustive decision journal" is not always followed. [I] A size cap on Log would help.
- Older docs (`odd-mandatory-delegation.md`, `odd-spend-proportionality.md`) predate the S#/L# format and use Objective/Problem/Scope prose. The fixed format arrived with the spec-by-reference work on 2026-10-03 (`odd-spec-by-reference.md:21`).

### 4.3 Engram mirror

[V] `routing.go:158,165-167`.

- Topic `odd/<feature-name>/tasks`, project-scoped; holds "the full current document and repository-relative file locator, not just a summary".
- Saved with `capture_prompt: false` (automated artifact; `internal/assets/engram/protocol.md`, the `mem_save` format section).
- **Read back both writes**: file and mirror are not atomic. If Engram is unavailable, keep local progress, mark the mirror *pending*, never claim success, resync later. If versions conflict irreconcilably, preserve both and ask only about the real conflict.
- Do not infer active work from the newest global memory; resume scoped to project and feature.
- The doc file is authoritative for working; the mirror exists for cross-session recovery when the file is missing or the worktree is gone. [I] This is the only reason both exist.

## 5. Handoff and delegation

### 5.1 Handoff by reference

[V] `routing.go:73`, spec S4 in `odd/tasks/odd-spec-by-reference.md:9`.

"Hand off by reference, never by paraphrase: name the document, task, and specs (for example `Spec: odd/tasks/<feature>.md, T2, S3-S4`), tell workers to read until `## Log`, and ask which `S#` were covered. Without a feature document, include the user's request verbatim."

- The parent passes: locator + task IDs + linked `S#`. It does not rewrite the requirement.
- Workers read the document up to `## Log` before edits and report which `S#` they covered.
- Why it matters [I]: a paraphrase drifts at every hop and costs tokens at every hop; a path costs ~20 tokens and the worker reads the canonical text once.

### 5.2 Delegation triggers

[V] `routing.go:116-133`. Text rule: "Each trigger holds only when its condition truly holds, and only then is it mandatory, not advisory; no trigger holds by default, and when in doubt work inline ... executing past a fired trigger inline is a routing defect even if the work succeeds."

| Trigger | Fires when | Numbers [V: manifest.go:217-231] |
|---|---|---|
| **Ask** | an open product/design decision | one focused question, stop and wait |
| **Mapping** | understanding exceeds the inline batch or needs more than ~5 sequential lookups | one read-only explorer; handoff at most ~2k tokens with `path:line`; one parent spot check |
| **Verification** | the change's Risk line names a high-risk item | one independent verifier, "as rarely as possible"; a native tier, a failed assessment, or *the writer's model/effort* never add one |
| **Tracking** | a large task | feature doc + mirror + work-unit commits |
| **Writer** | a *named reason* only: (a) **parallelism**: two or more independent units of behavior with disjoint edit surfaces, each needing several minutes (several files or ~100+ changed lines), launched together; (b) **context**: the long-session backstop | units of ~100 changed lines or less always run inline: "writing them costs less than starting workers"; a subagent "spends one to two minutes reading before it writes" |
| **Parallel writers** | see Writer (a) | other repository -> fresh worktree on that repo's main; same repo -> same tree with declared disjoint edit surfaces, parent owns git, no repo-wide formatters/generators/installs, one seam check (full suite once) at the end; same file -> isolated worktrees |
| **Invariants** | any writer prompt | name the existing helpers for shared state and the invariants they keep; parallel units cannot see each other |
| **Preparation** | a write is delegated | reading that prepares it is delegated with it, not done in the parent |
| **Output budget** | large task | bound parent bash output to counts/`--stat`/tail; delegate long suites |
| **Long-session backstop** | parent context at about 150k tokens (`ApproxParentContextTokens: 150000`, `ContextBackstopIsAdvisory: true`) | "advisory context guidance, not mechanically observed or enforced" |
| **Route declaration** | large work | record route per task and the trigger evidence in the feature doc so skipped delegation is observable |

Never delegate a writer for: size, "a large task alone", file count, or a price ratio (`routing.go:126`). A unit is one independent behavior with its code, tests and docs *together*: never split one behavior into test, docs and code writers.

**Evolution (useful to us):**

1. `odd-mandatory-delegation.md`: found the orchestrator ran everything inline; added mandatory file-count triggers (the "4-file rule", "multi-file write rule") plus a route declaration.
2. `odd-spend-proportionality.md` (#4894): added "validate premises before building, reuse sibling investigation, focused checks while iterating, full checks at closure"; kept "two or more non-trivial files" as the writer threshold.
3. `1731-parity-delegate-for-reason.md` (S1, S5): *removed* file-count triggers. Writer only for a named reason; explore only for a map the parent needs. The file-count rule produced a cost problem (subagent reading overhead) and was replaced.

[I] The lesson is that gentle-ai tried size-based delegation, measured the pain, and moved to reason-based. We should start where they ended.

Residual inconsistency [V]: `capabilitymanifest` still carries `WriterMinNonTrivialFiles: 2` (`manifest.go:233`) and `odd/tasks/task-size-canon.md:30` notes the JSON contract keeps it "but routing no longer renders it". Dead config, not behavior.

### 5.3 Workers

`odd/tasks/1731-...:S6-S8` and the OpenCode agents `gentle-ai-worker`, `gentle-ai-verify`, `gentle-ai-explore` (`internal/assets/opencode/agents/`) carry the worker-side rules. Only OpenCode ships named agents; other runtimes use generic subagents driven entirely by the handoff text (`1731-...:L6`). OpenClaw therefore gets the *routing text only*, with no ODD-specific subagent definitions. [V: docs/agents.md:44 and manifest `FileSubAgents` false for OpenClaw, `manifest.go:346-348`]

## 6. Verification

[V] `internal/assets/skills/_shared/odd-orchestrator-sections.md:13-57`, `routing.go:124`.

Risk-proportionate gate:

| Tier | Meaning | Verification |
|---|---|---|
| passive | docs, images, comments with no executable effect | structural readback only |
| medium | ordinary behavior change covered by focused tests | writer self-verification: it runs the parent-authorized `## Verification` commands in the foreground and reports `<command>: <observed result>`. No separate verifier |
| high (only through a *named* item) | any of the six items | self-verification **plus one independent read-only verifier** that re-runs the commands and inspects the diff without the writer's context |

Fixed in every tier: the **parent spot check** (re-run one reported command before delivery).

**Verifier behavior (the part we most want):**

- **Verify handoff:** give the verifier the *whole* feature document (every `S#`, never one task), the baseline commit, and the probe command forms.
- **Runs the spec's examples**: "runs the spec's examples the parent authorized, against isolated state when they mutate data, and returns a verdict per `S#`" (`routing.go:73`; widened in `odd-spec-by-reference.md` L8: command scope is "test, build, lint, or spec example commands").
- **Isolation:** on first launch, probes run in a fresh scratch copy made with `mktemp -d` under the system temp dir, never inside the workspace, leaving no new file in the workspace.
- **Probes derive from the specs**, plus invariants: the data hash is unchanged after a rejected command; prior commands' output equals the baseline.
- **Severity:** a *blocker* is a change-caused defect or unmet spec item that reproduces with realistic input and did **not** reproduce at the baseline. Defects already at the baseline and out-of-domain values are *advisories*. Silently ignoring an explicit option with success, or changing existing output nobody asked to change, is always a blocker.
- The writer commits the probes as regression tests.
- **When the user reports a failure, reproduce it before deciding it already works** (`routing.go:73`; `odd-spec-by-reference.md:10`, S5).

**Timing and bounds** (`1731-...:S8`):

- One end-of-feature verify when the same model wrote several deliveries inline; per-unit verify only on a named high-risk item (plus "always on smaller-model code" in the original S8; the shipped text in the shared section dropped that clause: "never because of the model or effort that wrote the code").
- One correction batch fixes every reported blocker; one recheck limited to those blockers; a second correction only if the *same* blocker still fails; a new finding never earns one; leftovers become one **Needs your decision**. Worker self-review has the same bound, then reports `partial`.
- An unavailable verifier is reported as unavailable, never retried or escalated (`routing.go:30`).

Note: spec S8 in `1731-...` still says "per-unit verify on real high risk and always on smaller-model code", while the shipped shared text says "never because of the model or effort that wrote the code" (`odd-orchestrator-sections.md:13-24`). The shipped text wins; the task file records the earlier intent. [V]

## 7. Tests, commits, delivery

### 7.1 Test discipline

[V] `routing.go:168` (long form), `docs/usage.md:30`.

- Test-first by default **when** a relevant runnable deterministic test and a clear expected outcome exist: observed RED -> GREEN -> REFACTOR. Otherwise explain the exception and run proportionate functional checks. Never invent RED/GREEN evidence or a runner; record the runner in the feature doc and forward it to workers.
- **One RED test per requested rule**, covering the cases that rule names, asserting observable effects (output, exit code, persisted data) through the public interface.
- **PRESERVE:** for every existing command/option the change touches (it "shares the changed code"), add one test proving its previous behavior still holds; add no other cases (no padding).
- **Differential check before finishing:** run each existing command that shares the changed code with the new/changed input against a copy of the base commit (in a `mktemp -d`, never committed) and against the change; compare stdout, stderr, exit code. Any unrequested difference is a regression to fix and pin.
- Shared state goes through the existing helper, keeping its invariants.
- Scratch (tmp dirs, `HOME`, `TMPDIR`, caches) lives outside the worktree in `mktemp -d`; subagents report scratch paths and the parent removes them.
- Update help text and docs for any changed command/option/message.

### 7.2 Commits and delivery

[V] `routing.go:73,174`, `docs/usage.md:23,32`.

- Work-unit commits on the feature branch (branch first when on default), about **400 authored changed lines** per slice (additions + deletions, generated files excluded), tests and docs alongside behavior, Conventional Commit message. A unit whose Risk line names a high-risk item gets its **own commit**. The doc records the commit that holds each task as evidence.
- The 400 is a planning heuristic only: "not a task acceptance criterion, hard cap, counter-trigger, automatic stop, forced split"; if the correct solution is bigger, explain briefly and continue; never delete blank lines/comments/tests or split artificially to fit (`routing.go:157`).
- Delivery strategy chosen once per feature: `ask-on-risk` (default), `auto-chain`, `single-pr`, `exception-ok`. When the forecast or running count passes ~400, apply it before the next commit; cached; slice boundaries recorded in the doc. Uses skills `work-unit-commits` and `chained-pr` resolved by registry name.
- **Push, PR creation and merge remain the user's decisions** (`routing.go:73`).

## 8. What ODD does not do (so we do not over-read it)

- [V] No time or story-point estimate anywhere (`grep -rIi "story point\|t-shirt"` over `odd/`, `skills/`, `internal/assets/` returns nothing; "estimate" appears only in the `chained-pr` skill, for changed-line forecasts). Sizing = resume test + risk items + the forecast of changed lines for delivery slicing. Covered in Study 3.
- [V] No prompt-caching mechanism (`grep -ri "prompt cach\|cache_control"` over `internal docs skills odd` returned nothing).
- [V] No token measurement claims. The module-split test file says its byte ceiling "is an engineering ratchet, not a token, cost or latency claim" (`internal/components/agentguidance/orchestrator_module_budget_test.go`, comment above `claudeModuleOverheadCeiling`).
- [V] RDD (Receipt-Driven Development) is a *separate* opt-out review system tied to the `gentle-ai review` CLI (`docs/usage.md:131-135`, `docs/trigger-rules.md`). ODD never enables it. We have no equivalent binary; the portable idea is only the risk gating.
- [I] Compliance is unmeasured. The repo has a `bench/` corpus of journeys, but `skills/gentle-ai-bench/SKILL.md` states `go test ./bench` "does NOT execute journeys" and says nothing about ODD-adherence rates. No evidence found that models follow the 7 steps reliably.

## 9. Quirks that affect adoption

1. **Language rule conflict (decision needed).** ODD requires `## Specs` and `L1` to quote the user's words verbatim "in the user's language" (`routing.go:158`; real examples are quoted in Spanish, `odd-spec-by-reference.md:21-22`). Our managed AGENTS.md says every work artifact is English (kit AGENTS.md, section "IDIOMA"). Proposal in Study 4: keep spec prose in English, allow *verbatim quotes* as quoted evidence marked with their language.
2. **Wording is dense.** The step-6 item at `routing.go:73` is one ~1,770-character line ([M] `sed -n 73p | wc -c`); the routing block is part of a ~36-55 KB injected file (Study 2). Copying it verbatim is the wrong move; copy the rules, not the prose.
3. **Dead or drifting config:** `WriterMinNonTrivialFiles` (above). Also `docs/usage.md` mentions `gentle-ai review assess` flags that exist only with the binary.
4. **Self-reported evidence.** ODD relies on the same model to write "RED observed" in the log. The mitigations are the parent spot check and the independent verifier on high risk. [I] Good enough for a trusted-agent setup; weak against a lazy model.

## 10. Rule checklist (for Study 4 to map against)

- R1 Authorize first; read-only stays read-only.
- R2 Explore only for a map; never pre-read what you will read anyway.
- R3 One question / one challenge caps.
- R4 Resume test + three criteria; counts never decide.
- R5 Six high-risk items; `Risk:` line on every change.
- R6 Large => feature doc (S#, Tasks, Log) + mirror, before first write.
- R7 Handoff by reference; workers report covered `S#`.
- R8 Delegate only for a named reason; none by default; route declared.
- R9 Writer units: independent, >100 lines, launched together; same-tree disjoint surfaces.
- R10 Verifier only for a named high-risk item; runs spec examples in scratch; verdict per `S#`; baseline-aware severity.
- R11 One correction batch, one recheck, then Needs your decision.
- R12 RED per rule + PRESERVE + differential check.
- R13 Slices ~400 lines; high-risk alone; push/PR/merge are the user's.
- R14 Change => rewrite only the affected S# and reopen only its task.
- R15 No self-pause; partial -> one Needs your decision.
