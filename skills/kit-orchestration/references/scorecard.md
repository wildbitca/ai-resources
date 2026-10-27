# Scorecard (full rules)

Loaded from `SKILL.md` "Scorecard" when the orchestrator needs the dimension anchors, role triggers, tiers or escalation table. Numbers live in `../scorecard.json`.

The orchestrator scores the task **before any multi-step work**, from the request plus a bounded read of the repo (`git diff --stat`, the files the request names, one grep; about 3 calls). Six dimensions, each an integer 0-3, **higher means more ceremony**:

| Dimension | 0 | 3 |
|---|---|---|
| `resumability` | a cold session could finish from the request and `git diff` alone (the resume test) | it could not: state lives only in this session |
| `blast_radius` | one file | many files or modules |
| `risk` | none | auth, secrets, money, data migration, infra or a public API |
| `uncertainty` | cause and design are known | unknown root cause or open design choices |
| `verifiability` | existing tests cover the change | nothing would catch a regression |
| `spread` | one domain | backend + infra + mobile |

The scores are a judgement; the mapping is a rule, so the same scores always give the same team. The numbers live in [scorecard.json](scorecard.json) (single source of truth, read by `ai-resources scorecard`, embedded in `/kit-plan` and `/kit-implement`, and regression-tested against `evals/scorecard/cases.json`).

| Mode | When (total is the sum, 0-18) | Roster | Handoff |
|---|---|---|---|
| **LITE** | total <= 3, `risk` 0, `uncertainty` <= 1, `resumability` <= 1, `verifiability` <= 2, and no role trigger fires | none: work inline; focused test, then the suite once | no |
| **STANDARD** | anything between | `implementer`, `tester`, `verifier`, plus the triggered roles | only when `planner` is added |
| **FULL** | total >= 10, or `risk` 3, or 3+ triggered roles | `planner`, `software-architect`, `implementer`, `tester`, `code-reviewer`, `security-auditor`, `verifier` | yes |

Role triggers (they add to STANDARD; any firing also rules out LITE): `planner` if `resumability` >= 2; `software-architect` if `uncertainty` >= 2 or `spread` >= 2; `code-reviewer` if `blast_radius` >= 2; `security-auditor` if `risk` >= 2. `verifiability` has no role of its own: it raises the total and tells the tester how much to add. **A `verifiability` of 3 (nothing would catch a regression) is never LITE**, even when it is the only non-zero score (total 3): that task needs an independent test gate, so it is STANDARD (`max_verifiability` 2 in `scorecard.json`). A lone 2 stays LITE: the inline session writes the missing test first.

**Delegation (three layers, cheapest first).** The roster says who may run; whether a step is delegated is a separate decision that must pay for itself. (1) *Resumability*: a LITE card stays inline. (2) *Divisibility* (`divisible`): the work splits into units with no shared state and no waiting on each other. The heuristic is code: units whose files are disjoint (a directory contains its files) and that name no `depends_on` are divisible; overlapping files or a dependency are not; fewer than `delegation.min_units` units are not. Only when it is doubtful (a unit lists no files) may one small-model `explore` call decide, with strict JSON `{divisible, evidence}` and only the unit list and its files as input (`divisibility_prompt`); a missing answer, or none, means one agent. Not divisible means a single writer even when the work is large. (3) *Reason*: delegation needs one of `delegation.reasons`: `explore-without-polluting-main-context`, `parallel-writers-on-disjoint-units` (needs `divisible: yes`) or `independent-verification`. Independent verification is never skipped for security- or merge-touching work (`--security-or-merge`), including on a LITE card. The card records `layer` (the layer that decided), `evidence`, `divisible`, `reason`, `delegate`, `parallel_writers`, `verifier`, `cost_tokens` (what the divisibility call cost: 0 when none was made, null when the runtime does not report tokens) and `cost_source`, so forecast and actual can be compared. Code: `ai_resources.scorecard.decide_delegation` and its twin in `workflows/scripts/sizing-core.js.inc` (`tests/test_delegation.py` runs both over the same cases); `/kit-implement` records it on the card when the plan's Sizing block or the caller gives units, a reason or the flag; explicit size flags still win and record nothing; a card without a decision has the 2.9.0 form.

**Tests are mandatory in every mode**: LITE drops the ceremony, never the test (test first, watch it fail, then the suite once).

**Names and spellings.** LITE, STANDARD and FULL are `inline`, `light` and `full` in the workflow scripts and the plan's Sizing block. Every accepted spelling is one table, `size_flags` in `scorecard.json` (with `sizes`, the mode-to-size map): `inline` and `lite` mean LITE, `light` and `standard` mean STANDARD, `full` means FULL, as the flags `--inline|--lite|--light|--standard|--full`, as `{"size": ...}`, and as a bare legacy `mode` where the rule below allows one. `ai_resources.scorecard.mode_of_spelling` reads the table in Python; the scripts read the copy `ai-resources generate` embeds from `workflows/scripts/sizing-core.js.inc`.

**Risk items.** One named item takes a LITE card out of the inline path (STANDARD, one independent verifier, a `Risks: item N` line; nothing bigger): 1 data or irreversible effects; 2 security; 3 contracts others consume (APIs, CLI flags, config, handoff keys, hooks, workflow YAML); 4 concurrency; 5 delivery or environment (installers, CI/CD, dependencies, gateway config); 6 no test would catch a regression. Size is not priority; never infer one from the other.

**Model tiers per role**, when the runtime can choose a model per subagent: `fast` (cheap) for `explore` and `tester` (reading, running suites); `balanced` for `implementer` and `doc-writer`; `strong` for `planner`, `software-architect`, `code-reviewer`, `security-auditor`, `verifier` (judgement). A runtime without per-role models ignores the tier; the roster is unchanged.

**Escalation, never a silent downgrade.** A run may move up, never down, and says so when it does. Failed rounds are counted within the current mode; `--full` restores the whole loop (all seven roles plus handoff) whatever the scorecard says:

| From | To | When (either) |
|---|---|---|
| LITE | STANDARD | tests fail twice, or the diff reaches 150 changed lines |
| LITE | FULL | tests fail 4 times, or the diff reaches 400 lines |
| STANDARD | FULL | tests fail twice, or the diff reaches 400 lines |

**Code-enforced versus instruction.** What the code does, and what it only tells the orchestrator to do:

| Part | Enforced by code | Instruction to the orchestrator |
|---|---|---|
| Scores to mode, roster, tiers | `ai_resources.scorecard.score`, `ai-resources scorecard '<json>'`, the same rules embedded in `/kit-plan` and `/kit-implement` | the six scores themselves are a judgement |
| Invalid scores | `score_or_full`, the CLI and the scripts (one rule, below) | on the manual path, apply that rule yourself |
| Named risk item on a LITE card | the scripts and `--risk-item` move it to STANDARD and record `scored_mode`, the effective `mode` and `bump_reason` on the card | listing the risk items |
| Escalation (failed rounds, diff lines) | `ai-resources scorecard gate <mode> --failures N [--base REF]` measures the diff and prints the new mode; `/kit-implement` escalates a light run on its diff and failed rounds, recomputes the test-round limit after each escalation, and re-enters an inline run on `{"size":"inline","failures":N,"diff_lines":N}` | counting failed rounds in a LITE session and calling the gate; LITE itself spawns nothing, so only that call carries the data |
| Delegation decision | `decide_delegation`, `ai-resources scorecard '<json>' --units JSON [--delegate-reason R] [--security-or-merge] [--model-answer JSON]`, `plan-check` (reason, `divisible: yes` for writers, `resume_test` against `resumability`) | judging when the heuristic is doubtful and naming the reason |
| Recording in the handoff | `--handoff PATH` on the scorecard and `escalate`/`gate` commands writes the front matter key `scorecard` (creating the file from `handoff.md.template`) | running the command; a LITE card has no handoff file, the printed card is its record |

`ai-resources scorecard escalate <mode> <failures> <diff_lines>` prints the resulting mode (add `--handoff PATH --reason TEXT` to append `-> FULL (reason)` to the `scorecard` key). On escalation the roster becomes that of the new mode (the new roles join the run) and the card is printed again.

**Invalid scores mean FULL; no scores plus no classifier answer mean light.** `ai-resources scorecard` with scores that are not JSON, miss a dimension, add one, or hold a value outside integer 0-3 prints a FULL card (rationale names the problem, a warning goes to stderr, exit 0). The scripts do the same, also when the classifier returns a bare legacy `mode` next to invalid scores. Only a response with no scores at all may carry a legacy `mode`, and then that mode is honoured. A classifier that failed or answered nothing at all (no scores, no mode) is not an invalid card: the scripts default it to light, as `/kit-plan` always did. The Python helper has no classifier that could have failed: `score_or_full` with no scores and no valid mode gives FULL. `/kit-implement` applies the same rules to the scores recorded in the plan's Sizing block, since it never classifies.

**Print and record.** At the start of the run print the scorecard (the six scores, the mode, the roster with tiers, a one-line rationale), then record it in the handoff front matter key `scorecard` with `--handoff PATH` (LITE has no handoff file: the printed card in the reply is the record). The key holds the mode, then the six scores in the short form below, then any bump or escalation:

`STANDARD res1 blast1 risk0 unc2 ver1 spr0` where `res` = resumability, `blast` = blast_radius, `risk` = risk, `unc` = uncertainty, `ver` = verifiability, `spr` = spread, each followed by its 0-3 score. A risk-item bump adds `(scored LITE, bumped: risk items named: ...)`; each escalation adds `-> FULL (reason)`. `ai-resources scorecard '{"resumability":1,...}'` renders the block.

