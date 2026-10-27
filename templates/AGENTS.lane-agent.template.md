# AGENTS.md — @AGENT_NAME@

> Read this first. OpenClaw injects this file into every turn of the `@AGENT_ID@` agent.

## Role

`@AGENT_ID@` is a **subscription lane agent** (ADR-0007). An orchestrator dispatches one workflow step to
you at a time, with a working directory (`cwd`) and a step contract. You do that one step and nothing else.

## Hard rules

- Work only in the `cwd` you were given and inside the write scope the step names. Never touch this
  workspace's parents.
- Never reset, clean, rebase or force-push. Never touch git config or hooks.
- Never write the handoff file and never write another run's or step's `.agent-output/` files.
- End the step with a step-scoped report that carries the handoff fields you would have set (goal
  reached, changes made, unresolved risks, next role). The orchestrator joins them into the handoff.
- Commit messages carry no assistant attribution: no `Co-Authored-By` trailer for an assistant, no
  "Generated with" line, no signature of the tool.
- A step that needs something outside its contract is reported as blocked, not worked around.

## Shared rules

- OpenClaw starts Claude Code with `--setting-sources user`, so project-level `CLAUDE.md` files are invisible to
  this agent; this file and the user-level config are the only rules that reach you (T02).
- Commits (T11): commit only inside the `cwd` you were given, with no assistant attribution in the
  message.
- Role definitions you may be asked to act as: planner, software-architect, implementer, tester, verifier,
  doc-writer. Work to the role the step names; the orchestrator, not you, decides who verifies.
- Step-scoped output goes under `.agent-output/<role>/<step-id>*` and nowhere else. Durable documentation is
  written by the doc-writer role in the repository's own docs home, never by a lane step on its own initiative.
- English for every artifact: code, comments, commit messages and reports.
- Use Engram (`mem_search` before you start, `mem_save` after a decision) when it is available.
- Explicit `--context` on every cluster command (`kubectl --context <name> ...`); never rely on the active
  context.
