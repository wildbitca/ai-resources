---
name: cursor-agentic-setup
description: "DEPRECATED — Use /setup-project command instead. This skill was for the old per-project submodule approach."
disable-model-invocation: true
---

# Cursor Agentic Setup — DEPRECATED

**This skill is superseded by the `/setup-project` command** (see `~/.cursor/rules/setup-project.mdc`).

The agent system now uses a centralized global setup:
- **Global:** `~/.cursor/` — rules, roles, personas, skills, workflows, templates, AGENTS.md
- **Per-project:** `<repo>/.cursor/mcp.json` only + `<repo>/.cursorrules` + `<repo>/specs/` + `<repo>/knowledge/`

To set up a new project, use `/setup-project`. To update shared resources, use `/self-update`.

The templates in this skill's `templates/` directory are kept for historical reference only.
