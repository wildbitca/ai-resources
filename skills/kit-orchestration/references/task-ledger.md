# Task ledger and Engram mirror

Read this only for `light` and `full` work that creates `specs/features/<feature>/tasks.md` (template `$AGENT_KIT/templates/tasks.template.md`). `inline` work creates no ledger and no mirror.

## Lifecycle (temporal)

1. **Create** when the size is `light` or `full`: Sizing block, `## Specs` (`S#`, English, each with a short `source:` link), `## Tasks` (`T#` naming the `S#` it covers), `## Log` (`L1` = link to the original request). A repository without `specs/` keeps only the plan under `.agent-output/`.
2. **Work by reference:** pass the file path, the `T#` and the `S#`; the worker reads until `## Log` and reports the ids it covered. Record the commit id on each `T#` when it lands.
3. **Change of scope:** append one `L#` line and rewrite only the affected `S#`.
4. **Close:** run `knowledge-audit`: accepted criteria that became `RULE-*`/`TS-*`, decisions and rejected options go to the spec or `specs/knowledge/decisions/`; fill `## Sizing (actual)`; then delete `tasks.md`. A ledger that survives its change is an orphan.

## Engram mirror (best effort, file first)

The file is the source of truth. The mirror only helps a later session resume after a reset; skip it for `inline` work and when Engram is unavailable (say `mirror pending` in the handoff notes, do not retry in a loop).

- **Project:** call `mem_current_project` once; it resolves from the git remote, also inside a managed worktree (`~/.openclaw/worktrees/...`). If it reports an ambiguous or unexpected project, pass the repository's project explicitly. Never search across all projects to resume.
- **Topic key:** `kit/<feature>/tasks`, `type: "learning"`, `scope: "project"`, `capture_prompt: false`. One key, so every write upserts one observation.
- **Writes (two, no more):** at creation, the Sizing block, the `S#` list and the `T#` list with the path of the file; at close, the same key updated with `closed` and the path of the spec or decision that absorbed it.
- **Content:** ids, status and paths only; never the requirement text, code, logs or secrets.
- **Resume order:** `mem_context`, then `mem_search` on `kit/<feature>/tasks`, then `mem_get_observation` for the hit. If the file exists, trust the file.
- **Tools:** only the core `mem_*` tools above (`mem_save`, `mem_search`, `mem_get_observation`, `mem_context`, `mem_current_project`); other tools vary by Engram version.
- **Conflicts:** a `judgment_required` result on this key is resolved as `related`; never ask the user about a mirror.
