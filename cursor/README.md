# Cursor agentic config (ai-resources)

This **`cursor/`** tree is the **canonical** Cursor-oriented config inside **ai-resources**: rules, agents, handoff template, workflows, and skills. Set **`AGENT_KIT`** to the repository root so paths resolve as **`$AGENT_KIT/cursor/...`**. Workspace-specific overrides live in `<repo>/.cursor/` or `<repo>/.cursorrules`.

---

## Structure

| Path | Purpose |
|------|--------|
| **rules/** | `.mdc` rule files (`alwaysApply`, `globs`, `description`). |
| **agents/** | Role prompts (implementer, tester, verifier, …). |
| **workflows/** | YAML workflows and `_domain-commands.yaml`. |
| **handoff.md.template** | Template; live handoffs live in the **current workspace** (e.g. `.agent-output/`). |
| **skills/** | First-party and vendor skills; registry: **`skills-registry.md`**. |
| **README.md** | This file. |

---

## Where things live

- **Kit (here):** `$AGENT_KIT/cursor/{rules,agents,workflows,skills,...}` — versioned in git.
- **Per workspace:** `specs/PROJECT.md`, `<repo>/.cursor/mcp.json`, handoffs under `<repo>/.agent-output/`.

---

## Commands (reference)

- **Handoff:** Use **`$AGENT_KIT/cursor/handoff.md.template`** as structure; edit live files in the workspace.
- **Rules:** See **`$AGENT_KIT/cursor/rules/`** (e.g. `000-project-bootstrap.mdc`, `100-token-economics.mdc`).
- **Skills:** Under **`$AGENT_KIT/cursor/skills/`**; resolve IDs via **`$AGENT_KIT/cursor/skills-registry.md`**.
