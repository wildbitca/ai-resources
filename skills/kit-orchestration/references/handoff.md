# Handoff file

One handoff file per workflow run: `<repo>/.agent-output/handoff-<scope>.md`, created from `$AGENT_KIT/handoff.md.template`.

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
- **Rollback:** after each step, if `Blocked: true`, re-run `Return_to_step` (at most 3 attempts per step).
- **Cross-domain:** set `Domain` to the target domain and reference paths only — never paste code between domains.
- **Lifecycle:** delete the file when the workflow finishes. A handoff older than 24 hours is reported at session start as stale.
