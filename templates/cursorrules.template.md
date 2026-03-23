# {{PROJECT_NAME}} — Project Configuration

## Agent System

**Shared resources:** **`ai-resources`** repository (root: `rules/`, `skills/`, `workflows/`, `AGENTS.md`, generated `skills-index.json`). After **`brew upgrade`** or **`git pull`**, run **`python3 $AGENT_KIT/scripts/kit.py generate`** — see **`README.md`** at repo root.

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

| User intent                 | Workflow file                                           |
|-----------------------------|---------------------------------------------------------|
| Feature (any domain)        | `$AGENT_KIT/workflows/_feature-template.workflow.yaml`  |
| Bugfix (any domain)         | `$AGENT_KIT/workflows/_bugfix-template.workflow.yaml`   |
| Security audit / pen test   | `$AGENT_KIT/workflows/security-devsecops.workflow.yaml` |
| Explore / plan (any domain) | `$AGENT_KIT/workflows/explore-and-plan.workflow.yaml`   |

{{EXTRA_WORKFLOWS}}

Disambiguation: implement now → feature; understand/plan first → explore-and-plan.

## Specs (Source of Truth)

Living specifications at `specs/`. **Single index:** `specs/PROJECT.md`. Agents MUST read relevant specs before implementing.

## MCP Servers

Defined in `.cursor/mcp.json`: {{MCP_LIST}}.

## No-Go Zones

{{NO_GO_ZONES}}

## Knowledge (under specs — working artifacts)

Ephemeral research, plans, and validation reports live under **`specs/knowledge/`** (same repo tree as living specs). **`specs/features/`, `specs/cross-cutting/`, and `specs/architecture/` are the product source of truth**; promote content from knowledge into those paths, then remove obsolete
knowledge files (see `specs/knowledge/README.md` if present).

- `specs/knowledge/research/` — codebase exploration, bug analysis (YYMMDD-{topic}.md)
- `specs/knowledge/decisions/` — plans, architecture decisions (YYMMDD-{topic}-plan.md)
- `specs/knowledge/searchable/` — validation reports (YYMMDD-{topic}-validation.md)

Every research step MUST check `specs/knowledge/research/` first. Every plan step MUST save to `specs/knowledge/decisions/`. Every verify step MUST save to `specs/knowledge/searchable/`.

## Quick Reference

{{BUSINESS_RULES_SUMMARY}}
