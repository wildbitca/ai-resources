# Token economics (agent-agnostic)

## Goals

- Keep the **orchestrator / main thread** as a short router: status, handoff path, file list, next step.
- Push **broad exploration**, multi-file work, and heavy reads into **sub-agents** or delegated tasks so the main context does not grow without bound.

## Measured reference (external)

[Agent Teams Lite](https://github.com/Gentleman-Programming/agent-teams-lite) documents **~50–70%** token savings for medium–large features when using delegate-first delegation vs. inline work, across independent analyses. See their `docs/token-economics.md` in the upstream repo.

## Local rules (Cursor)

- `$AGENT_KIT/cursor/rules/100-token-economics.mdc` — mandatory pruning, `@file` preference, broad exploration → subagent.
- `$AGENT_KIT/cursor/rules/010-orchestrator.mdc` — **Main thread output contract** after `Task`.

## Practices (any IDE)

1. **Do not** paste full subagent logs into the user-visible reply; point to `handoff_file` or diffs.
2. **Do not** rely on `mem_search` alone for full SDD/spec text — always **`mem_get_observation`** when content matters.
3. **Regenerate** skill registry after adding skills (`$AGENT_KIT/scripts/generate-skill-registry.sh`) so orchestrators resolve paths once per session.
