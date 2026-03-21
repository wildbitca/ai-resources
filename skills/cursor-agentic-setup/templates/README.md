# Cursor Agentic Setup — Templates

These templates are the canonical source for Cursor agentic configuration. When running the `cursor-agentic-setup` skill:

1. **Setup (new project):** Copy all files to `PROJECT/.cursor/`, adapting placeholders.
2. **Update (existing project):** Compare with project's `.cursor/`, add/update missing or outdated files.
3. **.gitignore:** Add `.cursor/agents/` — do not version agent outputs or analysis (local only).

## Placeholders to adapt

- `{{handoff_file}}` — Injected by orchestrator. With worktree: `.cursor/handoff-<branch>.md`; without: `.cursor/handoff-<workflow>.md`. Per-workflow anti-bottleneck.
- `{{PROJECT_NAME}}` — Project name
- `{{DOMAIN}}` — mobile | web | infra
- `{{TEST_CMD}}` — e.g. `flutter test`, `npm test`, `pytest`
- `{{ANALYZE_CMD}}` — e.g. `dart analyze`, `eslint .`
- `{{DEPS_AUDIT_CMD}}` — e.g. `dart pub outdated`, `npm audit`
- `{{TEST_DIR}}` — e.g. `test/`, `__tests__/`, `tests/`

## Structure

```
templates/
├── prompt-new-project-setup.md   # Prompt to paste when setting up a new project
├── handoff.md.template
├── feature-implementation.workflow.yaml   # Flow + rules in comments
├── mcp-mobile.json           # Flutter/mobile base
├── mcp-web.json              # Web base (cursor-ide-browser)
├── mcp-plugin-servers.json   # HTTP MCPs: supabase, sentry, figma, cloudflare
├── mcp-empty.json            # Empty global backup
└── agents/
    ├── planner.md
    ├── implementer.md
    ├── tester.md
    ├── verifier.md
    ├── software-architect.md
    ├── code-reviewer.md
    └── security-auditor.md
```

## MCP templates

- **mcp-mobile.json** — dart, maestro, firebase, clickup, revenuecat. Merge with mcp-plugin-servers.json entries as needed (supabase, sentry, cloudflare).
- **mcp-web.json** — cursor-ide-browser. Merge with mcp-plugin-servers.json for supabase, sentry, cloudflare.
- **mcp-plugin-servers.json** — supabase, sentry, figma, cloudflare-docs, cloudflare-bindings, cloudflare-builds, cloudflare-observability.
- **mcp-empty.json** — For global `~/.cursor/mcp.json` when using project-local MCP only.
