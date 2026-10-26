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
| **LITE** | total <= 3, `risk` 0, `uncertainty` <= 1, `resumability` <= 1, and no role trigger fires | none: work inline; focused test, then the suite once | no |
| **STANDARD** | anything between | `implementer`, `tester`, `verifier`, plus the triggered roles | only when `planner` is added |
| **FULL** | total >= 10, or `risk` 3, or 3+ triggered roles | `planner`, `software-architect`, `implementer`, `tester`, `code-reviewer`, `security-auditor`, `verifier` | yes |

Role triggers (they add to STANDARD; any firing also rules out LITE): `planner` if `resumability` >= 2; `software-architect` if `uncertainty` >= 2 or `spread` >= 2; `code-reviewer` if `blast_radius` >= 2; `security-auditor` if `risk` >= 2. `verifiability` has no role of its own: it raises the total and tells the tester how much to add.

**Tests are mandatory in every mode**: LITE drops the ceremony, never the test (test first, watch it fail, then the suite once). In the workflow scripts LITE, STANDARD and FULL are `inline`, `light` and `full`.

**Model tiers per role**, when the runtime can choose a model per subagent: `fast` (cheap) for `explore` and `tester` (reading, running suites); `balanced` for `implementer` and `doc-writer`; `strong` for `planner`, `software-architect`, `code-reviewer`, `security-auditor`, `verifier` (judgement). A runtime without per-role models ignores the tier; the roster is unchanged.

**Escalation, never a silent downgrade.** A run may move up, never down, and says so when it does:

| From | To | When (either) |
|---|---|---|
| LITE | STANDARD | tests fail twice, or the diff reaches 150 changed lines |
| LITE | FULL | tests fail 4 times, or the diff reaches 400 lines |
| STANDARD | FULL | tests fail twice, or the diff reaches 400 lines |

`ai-resources scorecard escalate <mode> <failures> <diff_lines>` prints the resulting mode. On escalation add the new roles, append `-> FULL (reason)` to the handoff `scorecard` field and print it.

**Print and record.** At the start of the run print the scorecard (the six scores, the mode, the roster with tiers, a one-line rationale), then copy it into the handoff front matter key `scorecard` (LITE has no handoff file: the printed card in the reply is the record). `ai-resources scorecard '{"resumability":1,...}'` renders the block.

