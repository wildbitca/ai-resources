# {{PROJECT_NAME}} — Project Configuration

## Agent System

**Shared resources:** `~/.cursor/` (rules, roles, personas, skills, workflows, templates, AGENTS.md). Run `/self-update` to pull latest.

**Project resources:** `<repo>/.cursor/` contains only `mcp.json` (Cursor IDE requirement).

## Project Identity

- **Application Name**: {{APP_NAME}}
- **Domain**: {{APP_DOMAIN}}
- **Naming**: "{{DISPLAY_NAME}}" (display), "{{PACKAGE_NAME}}" (package), "{{FILE_CONVENTION}}" (file/directory)

## Canonical Domains

| Domain ID | Stack | Persona suffix | Signals |
|-----------|-------|----------------|---------|
{{DOMAIN_TABLE}}

## Workflow Auto-Selection

| User intent | Workflow file |
|-------------|---------------|
| Feature (any domain) | `~/.cursor/workflows/_feature-template.workflow.yaml` |
| Bugfix (any domain) | `~/.cursor/workflows/_bugfix-template.workflow.yaml` |
| Security audit / pen test | `~/.cursor/workflows/security-devsecops.workflow.yaml` |
| Explore / plan (any domain) | `~/.cursor/workflows/explore-and-plan.workflow.yaml` |
{{EXTRA_WORKFLOWS}}

Disambiguation: implement now → feature; understand/plan first → explore-and-plan.

## Specs (Source of Truth)

Living specifications at `specs/`. See `specs/project.living.md` for the master index. Agents MUST read relevant specs before implementing.

## MCP Servers

Defined in `.cursor/mcp.json`: {{MCP_LIST}}.

## No-Go Zones

{{NO_GO_ZONES}}

## Knowledge Directory

Workflows persist artifacts in `knowledge/` at project root:
- `knowledge/research/` — codebase exploration, bug analysis (YYMMDD-{topic}.md)
- `knowledge/decisions/` — plans, architecture decisions (YYMMDD-{topic}-plan.md)
- `knowledge/searchable/` — validation reports (YYMMDD-{topic}-validation.md)

Every research step MUST check `knowledge/research/` first. Every plan step MUST save to `knowledge/decisions/`. Every verify step MUST save to `knowledge/searchable/`.

## Quick Reference

{{BUSINESS_RULES_SUMMARY}}
