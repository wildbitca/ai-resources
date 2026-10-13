---
name: kit-orchestration
description: "How to run an ai-resources workflow: resolve each step's agent, persona and skills, dispatch subagents with a complete prompt, keep the handoff file, run parallel groups and join their results, retry blocked steps. Use when a workflow-* skill is active or when delegating to kit role subagents (planner, implementer, tester, code-reviewer, security-auditor, verifier, doc-writer)."
---

# Kit orchestration

## Prefer the deterministic loop for code changes

For a feature, bugfix or refactor, run the kit's workflow scripts instead of driving the steps by hand:

1. `/kit-plan <goal>` — parallel readers map the code, specs and tests; three planners draft from different angles; judges pick one; a plan file is written for the user to approve.
2. `/kit-implement` with `{"plan_path": "…", "goal": "…", "kind": "feature|bugfix|refactor"}` — one writer implements with tests, a tester gate runs the suite, three review lenses (correctness, security, test integrity) run in parallel, each finding is verified by a skeptic before it is fixed, and a verifier signs off against the acceptance criteria.

A workflow script cannot ask the user anything mid-run, which is why approval sits between the two. Use the YAML workflows below when a run needs judgement in the middle, when the task is not a code change, or when workflow scripts are unavailable.

## Locate the kit (`$AGENT_KIT`)

Workflows and prompts refer to `$AGENT_KIT`, the kit root (`workflows/`, `agents/`, `skills/`, `templates/`, `handoff.md.template`). Resolve it in this order:

1. `$AGENT_KIT`, if set.
2. The parent directory of `$AGENT_SKILLS_ROOT`, if set.
3. `dirname "$(dirname "$(readlink -f ~/.claude/skills/kit-orchestration)")"` — use `~/.agents/skills/kit-orchestration` for tools other than Claude Code.

## Before the first step

1. Read `specs/PROJECT.md` when it exists (Intent, Technologies, spec index).
2. Detect the domain with [references/domains.md](references/domains.md). If it is ambiguous, ask the user once.
3. For workflows that change code (feature, bugfix, refactor, cross-domain), work in a git worktree on `feature/<name>` or `fix/<name>` (skill `using-git-worktrees`).
4. Create the handoff file from `$AGENT_KIT/handoff.md.template` — naming, front matter, fields and rollback are in [references/handoff.md](references/handoff.md).
5. For a code-changing workflow, show the plan to the user before the first step that writes code.

## Running a step

Run the steps of the workflow YAML in order. For each step:

1. **Agent:** `subagent_type` names the kit role subagent. If `$AGENT_KIT/agents/personas/<role>-<domain>.md` exists, the subagent reads it for domain conventions.
2. **Skills:** each id under `skills:` is `$AGENT_KIT/skills/<id>/SKILL.md`.
3. **Prompt:** fill the step's `prompt_template` (`{{workspace}}`, `{{domain}}`, `{{user_goal}}`, `{{handoff_file}}`). The subagent has no chat context, so the prompt must contain:
   - the absolute workspace path and the handoff file path;
   - the `SKILL.md` and persona paths to read before acting;
   - the goal, constraints, and relevant spec paths;
   - the domain's commands from `$AGENT_KIT/workflows/_domain-commands.yaml` when the step tests, lints or builds;
   - when Engram is available: save decisions and gotchas that are not in code or specs;
   - the return format below.
4. **After it returns:** read the handoff file and the return block, not the subagent's full output. If `Blocked: true`, re-run `Return_to_step` — at most 3 attempts per step, then stop and report to the user. Otherwise continue with `handoff_to`.
5. A step whose prompt says it does not apply (e.g. `Requires_tests: no`) records that in the handoff and the workflow moves on.

## Choosing a backend

A step runs on `claude-code` unless its YAML says `backend: openclaw-agent` with an `agentId`. Use `openclaw-agent` when the role should run on another OpenClaw agent, and through it on another provider. Coordination stays the handoff file. The model is a property of whatever runs the role (the role's `by_role` executor, or the model configured on that agent); a step cannot pin one.

**Availability.** An `openclaw-agent` step can be dispatched only by an orchestrator whose own tool list contains `sessions_spawn`, that is, an OpenClaw-run agent turn. An `openclaw` binary on PATH does not count. A Claude Code session, a kit role subagent, a plain shell and the `/kit-plan` / `/kit-implement` scripts do not have it. When it is missing, do not fall back to `claude-code` and do not spawn anything; the step result is exactly:

```text
Blocked: true
Block_reason: openclaw-agent step <id> (agentId=<x>) needs sessions_spawn in an OpenClaw-run orchestrator; missing: sessions_spawn tool in this session
Return_to_step: <id>
```

**Dispatch.** Whether the agent exists, runs the CLI backend you expect and is admitted by `subagents.allowAgents` is checked now, never by the validator. Spawn with `sessions_spawn agentId=<agentId> cwd=<the orchestrator's worktree>`. The `cwd` is mandatory and explicit. The parameters, the return value and the completion signal of `sessions_spawn` are recorded in ADR 0005 (`docs/decisions/0005-cross-provider-role-dispatch.md`); where that record is empty they are unverified, and the child's own startup check (below) is the only proof of the working directory.

**Child prompt.** The child has no kit subagent definitions and no native skills, so the prompt carries everything:

- the absolute worktree path, which must be its working directory, and the absolute handoff path;
- `$AGENT_KIT/agents/roles/<role>.md` and, if it exists, `$AGENT_KIT/agents/personas/<role>-<domain>.md`, to be read first;
- each skill as `$AGENT_KIT/skills/<id>/SKILL.md`;
- the goal, the constraints and the domain commands;
- the write scope: only `.agent-output/<role>/`, plus the handoff for a sequential step only;
- the attempt number, the return format above verbatim (some role files have no return block of their own), and the sentinel spec below;
- a startup self-check: run `git rev-parse --show-toplevel`, and end as `Blocked` if it differs from the given worktree.

The prompt carries no secrets, no environment dumps and no code.

**Waiting (T39).** Every call that waits or polls lasts at most 60 s; never put a wait loop inside one call, and never restart or stop the gateway. The budget per attempt is 20 minutes. Completion is the sentinel `.agent-output/<role>/<step-id>.done` (JSON `{step, attempt, status, finished_at}`, written by the child last), or a terminal session state when a status tool exists. A timed-out child is ended with a session tool if there is one; otherwise the attempt is recorded as lost.

**Post-step check.** The Claude Code hooks (`kit_subagent_return`, `kit_handoff_guard`) do not fire for these children, so the orchestrator runs the check itself. The step is complete only if all of these hold:

1. The sentinel (or a terminal state) is present.
2. `.agent-output/<role>/<step-id>.md` exists and contains `## Result` and `## Routing`.
3. For a sequential step, the handoff front matter has `step: <id>`, a valid `status` and the keys of `handoff.md.template`. For a parallel member, the handoff sha256 is the same before and after.
4. For a read-only role, `git -C <worktree> status --porcelain` shows nothing outside `.agent-output/`.

Any failure is one failed attempt. On a failure of check 4, restore and clean only the offending paths. The check detects a violation; it cannot prevent one, because the child runs with its own agent's permissions.

**Retries.** At most 3 attempts per step, each a new spawn with the same `cwd`. Before a retry, rename the previous report to `<step-id>.attempt-<n>.md`. After the third failure, stop and report the last `Block_reason`.

**Evidence.** Per dispatch, record in the report and the handoff notes (never in new front-matter keys): backend, agentId, handle or sessionKey, cwd, attempt, start and end times, the result of checks 1-4, the report path and, for a parallel member, the handoff hash before and after.

**Example team.** Models are properties of the agent or role and informational here; `agy-flash` and `agy-pro` are placeholder ids for agents that must exist and be on `subagents.allowAgents`.

| Role | Backend | agentId | Model configured on that agent or role |
|------|---------|---------|-----------------------------------------|
| planner | openclaw-agent | agy-flash | gemini-flash (agy label) |
| explore (the "researcher") | openclaw-agent | agy-pro | gemini-pro (agy label) |
| software-architect | claude-code | | opus via `by_role` |
| implementer | claude-code | | sonnet via `by_role` |
| code-reviewer | openclaw-agent | agy-flash | gemini-flash (agy label) |

The example team is illustrative. `rules/017-multimodel-routing.mdc` recommends a strong model for review, so a real team would keep `code-reviewer` on a strong model. An agent backed by agy is an external prerequisite that this kit does not provide (see ADR 0005).

## Parallel groups

Steps that share `execution_hints.parallel_group` run concurrently — several Agent calls in one turn — once their shared entry criteria hold.

1. Before spawning them, write `.agent-output/parallel-group.json`:
   `{"group": "post-test", "steps": ["review", "security"], "handoff_file": ".agent-output/handoff-<scope>.md"}`
   While this file exists, a hook stops subagents from writing that handoff file.
2. Each parallel step writes its own report under `.agent-output/<role>/`, ending with the handoff fields it would have set.
3. A member with `backend: openclaw-agent` is spawned in the same turn as the others, polled and checked as in "Choosing a backend", and joined like any other member. Only the six read-only roles may be such members, and none of them writes the handoff.
4. When every step has returned: delete `parallel-group.json`, copy each report's fields into the handoff, and if any verdict blocks (`REQUIRES_CHANGES`, `FAIL`, `block`), set `Blocked: true`, `Block_reason` from that report, and `Return_to_step` from that step's `on_concern_return_to`.

## Subagent return format

Every kit role subagent working on a workflow ends its final message with this block. A hook sends the subagent back if the block is missing.

```text
## Result
- Status: success | partial | blocked
- Executive summary: 1–3 sentences
- Summary: at most 5 bullets
- Handoff: path to the updated handoff file

## Artifacts
- Files touched: paths, one per line
- Commands run: one per line, or "none"
- Specs/docs read: paths, or "none"

## Routing
- Next recommended: step id, or "none"
- Blocked: yes | no (if yes: reason and suggested Return_to_step)
- Risks: short, or "none"
```

Code, logs and stack traces go in the handoff or under `.agent-output/`, never in the block.

## Who writes what

- **One writer per run.** The implementer makes the code changes and writes the tests for what it changes (test first, watch it fail). Reviewers, testers and verifiers only read and run commands.
- **The tester is a gate**, not the test author: it runs the full suite, separates new failures from pre-existing ones, and fills coverage gaps the plan requires.
- **The reviewer audits the test diff** as well as the code: assertions deleted or weakened, tests skipped, tests that cannot fail.
- **The verifier checks acceptance criteria against evidence** it produces itself, and reports gaps. Spec, ADR and knowledge updates belong to a separate `document` step (skill `knowledge-audit`), not to the verifier.

## Finishing

1. When the verify step passes, merge the branch or open a PR if the user prefers (skill `finishing-a-development-branch`).
2. Delete the handoff file and any leftover `parallel-group.json`.
3. Tell the user the outcome, the changed paths, and anything left open — briefly.

## Delegating outside a workflow

Decide by task shape, not by step or file count. Delegate work that would flood the context (broad searches, long logs, test output), independent review, and parallelizable fan-out. Do targeted reads and small edits directly. Delegated prompts use the same contents and return format as workflow steps.
