# {{PROJECT_NAME}} — Project-Specific Business Requirements

## Agent System

**Shared resources:** `~/.cursor/` (rules, roles, skills, workflow templates, AGENTS.md). Run `/self-update` to pull latest.

**Project resources:** `<repo>/.cursor/` (domain personas, domain commands, project-specific workflows, mcp.json).

**MCP:** Use only MCPs defined in `.cursor/mcp.json`: {{MCP_LIST}}.

## Canonical Domains

| Domain | Language | Framework | Inherits |
|--------|----------|-----------|----------|
{{DOMAIN_TABLE}}

## Workflow Auto-Selection

| User intent | Workflow file |
|-------------|---------------|
| Feature (any domain) | `~/.cursor/workflows/_feature-template.workflow.yaml` |
| Bugfix (any domain) | `~/.cursor/workflows/_bugfix-template.workflow.yaml` |
| Security audit / pen test | `~/.cursor/workflows/security-devsecops.workflow.yaml` |
| Explore / plan (any domain) | `~/.cursor/workflows/explore-and-plan.workflow.yaml` |
{{PROJECT_SPECIFIC_WORKFLOWS}}

**Worktree + merge (MANDATORY):** Code-change workflows ALWAYS use git worktree at start and merge to base branch at end.

## Specs (Source of Truth)

Living specifications: `specs/`. Feature specs, cross-cutting specs, and ADRs. See `specs/project.living.md` for the master index.

## Project Identity

- **Application Name**: {{APP_NAME}}
- **Domain**: {{APP_DOMAIN}}
- **Naming Convention**: "{{DISPLAY_NAME}}" for display, "{{PACKAGE_NAME}}" for packages, "{{FILE_CONVENTION}}" for files/dirs

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
{{TECH_STACK}}

## Directory Structure

{{DIR_STRUCTURE}}

## No-Go Zones

| Area | Prohibition | Rationale |
|------|-------------|-----------|
{{NO_GO_ZONES}}

## Business Rules — Quick Reference

Business rules live in `specs/`. This section is a quick-lookup summary for the orchestrator.

{{BUSINESS_RULES_SUMMARY}}

## Knowledge Directory

`knowledge/` at project root:
- `research/` — codebase exploration, analysis
- `decisions/` — plans, ADRs, architecture decisions
- `searchable/` — validation reports, searchable artifacts
