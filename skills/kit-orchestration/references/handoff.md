# Handoff file

One handoff file per workflow run: `<repo>/.agent-output/handoff-<scope>.md`, created from `$AGENT_KIT/handoff.md.template`.

The file starts with YAML front matter holding the state a step needs to read (`workflow`, `step`, `status`, `blocked`, `return_to_step`, `block_reason`, `domain`, `requires_tests`, `security_critical`, `refs`, `verdicts`, `next`, `docs_updated`, `git_integration`), plus the **optional** by-reference keys `refs.spec`, `task_ids`, `spec_ids`, `class`, `scorecard` and `route` (absent or empty is valid; hooks and the post-step check must never require them), followed by the human-readable notes. Update both: the front matter is what the next step and the join of a parallel group read.

## The `scorecard` key

Written by code, not by hand: `ai-resources scorecard '<json scores>' --handoff <file>` records the task scorecard (creating the file from the template when it is missing), and `ai-resources scorecard escalate|gate ... --handoff <file>` appends each escalation. Value: the mode (`LITE` | `STANDARD` | `FULL`), then the six 0-3 scores in a short form, then any bump or escalation, e.g. `STANDARD res1 blast1 risk0 unc2 ver1 spr0 -> FULL (tests failed 2 times)`.

| Abbreviation | Dimension |
|---|---|
| `res` | resumability |
| `blast` | blast_radius |
| `risk` | risk |
| `unc` | uncertainty |
| `ver` | verifiability |
| `spr` | spread |

A LITE card has no handoff file (nothing is delegated), so the printed card in the reply is its record; an escalation out of LITE does create the file. Which parts are enforced by code: `references/scorecard.md`, "Code-enforced versus instruction".

## Scope (file name)

- With a worktree: the branch name with `/` replaced by `-` — `handoff-feature-auth.md`.
- Without a worktree: the workflow name — `handoff-release-dart-flutter.md`.
- Fallback: `.agent-output/handoff.md`.

## Core fields

| Field | Meaning |
|-------|---------|
| **Status** | `success` \| `partial` \| `blocked` |
| **Executive summary** | 1–3 sentences |
| **Goal reached** | 1–2 sentences |
| **Changes made** | File paths only, no code |
| **Commands run** | Build/test/analyze commands, one per line, or `none` |
| **Unresolved / risks** | Open items or follow-ups |
| **Next recommended** | Next step id, or `none` |
| **Next assigned role** | planner \| implementer \| tester \| verifier \| software-architect \| code-reviewer \| security-auditor \| explore \| doc-writer \| generalPurpose |
| **Domain** | dart-flutter \| angular \| symfony \| api-platform \| devops \| security |
| **Blocked** | `true` when a previous step must run again |
| **Return_to_step** | Step id to re-run when blocked |
| **Block_reason** | What that step must correct |
| **Git_integration** | `merge_to_base` \| `none` \| `pending_human` |

Workflow-specific fields (`Plan_ref`, `Architect_approval`, `Test_report`, `Code_review_verdict`, `Security_audit`, `Spec_refs`, `Requirements_preserved`, `Requires_tests`, `Security_critical_feature`, …) are listed in the template and set by the steps that own them.

## Rules

- **Who writes:** the step that is running. Steps in a parallel group never write it; the orchestrator joins their report fields afterwards.
- **`openclaw-agent` children:** they follow the same rules (read the handoff on start, report on completion), but nothing enforces them: only a sequential step writes the handoff, a parallel member writes `.agent-output/<role>/` only, and the orchestrator's post-step check (skill `kit-orchestration`, "Choosing a backend") validates the front matter before advancing. Dispatch evidence goes in the notes, never in new front-matter keys.
- **Rollback:** after each step, if `Blocked: true`, re-run `Return_to_step` (at most 3 attempts per step).
- **Cross-domain:** set `Domain` to the target domain and reference paths only — never paste code between domains.
- **Lifecycle:** delete the file when the workflow finishes. A handoff older than 24 hours is reported at session start as stale.
