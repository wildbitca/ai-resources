---
name: setup-project
description: "Scaffold a repository for the ai-resources agent kit: specs/PROJECT.md, specs/ tree, specs/knowledge/ working area, .gitignore entries, optional MCP config. Use when the user says /setup-project, 'set up this project for agents' or 'initialize agent config'."
disable-model-invocation: true
---

# /setup-project

Templates live in `$AGENT_KIT/templates/` (see `kit-orchestration` to locate `$AGENT_KIT`).

## Ask first

1. **Project name** — display name (e.g. "MyApp").
2. **Package name** — for code (e.g. `myapp` / `my_app`).
3. **Domains used** — dart-flutter, angular, symfony, api-platform, devops, php, security.
4. **Primary domain** — the default for domain detection.
5. **MCP servers** for this repo — e.g. clickup, firebase, supabase, sentry, engram.
6. **Business constraints** — prohibited items or no-go zones.

## Create

### 1. `specs/`

```
specs/
├── PROJECT.md          (from templates/PROJECT.template.md — Intent, Technologies, spec index)
├── architecture/.gitkeep
├── features/.gitkeep
├── cross-cutting/.gitkeep
└── knowledge/
    ├── README.md       (from templates/specs-knowledge-readme.template.md)
    ├── research/.gitkeep
    ├── decisions/.gitkeep
    └── searchable/.gitkeep
```

Fill **Intent** and **Technologies** in `PROJECT.md` from the answers; list the no-go zones there. Working artifacts go under `specs/knowledge/`, never in a separate `knowledge/` at the repo root.

### 2. `.gitignore`

Append when missing:

```
.agent-output/
.engram/
```

### 3. MCP config (only for the servers the user chose)

- Claude Code: `.mcp.json` at the repo root.
- Cursor: `.cursor/mcp.json` (start from `templates/mcp.json.template`).

Use environment-variable placeholders for secrets; never write tokens into these files.

## Afterwards

Report the files created and remind the user:

- Shared workflows, skills and roles come from the kit; the project keeps only `specs/` and optional MCP config.
- `specs/PROJECT.md` is the entry point agents read first.
