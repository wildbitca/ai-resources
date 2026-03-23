---
name: cursor-plan-tracking
description: Cursor plan completion tracking in YAML frontmatter. Use when creating or updating .plan.md files.
---

# Cursor Plan Completion Tracking

- **REQUIRED:** When creating or updating Cursor plan files (`.plan.md`), include completion tracking in YAML frontmatter:
  - `status` (pending | in_progress | completed)
  - `completedAt` (ISO date when completed, else null)
  - `todos` with `id`, `content`, `status` per item
- **PROHIBITED:** Empty `todos: []`; add concrete deliverable items with status.
- **BENEFIT:** Clear visibility of plan completion state at a glance.
