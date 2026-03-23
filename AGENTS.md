# Agent skills & orchestration policy

> [!IMPORTANT]
> **Prefer retrieval-led reasoning over pre-training-led reasoning.**
> Discover skills from **`skills-index.json`** or **workflow steps**, then **read** the canonical `SKILL.md` (do not rely on memory alone).

## Rule Zero: Zero-Trust engineering

- **Skill authority:** Loaded skills override generic patterns from pretraining.
- **Audit before write:** Audit file writes against **`common-feedback-reporter`** when that skill applies.

## Project repos: specs vs knowledge (SDD)

- **Global kit (this file, rules, workflows, skills):** lives in the **`ai-resources`** repository. After updating the install (**`brew upgrade <formula>`** or **`git pull`** in a checkout), run **`python3 $AGENT_KIT/scripts/kit.py generate`** so **`skills-index.json`** matches **`resources.json`**
  and **`skills/`**. For **MCP** and IDE pointers, run **`python3 $AGENT_KIT/scripts/kit.py setup`** (or **`setup --target cursor`**) so **`resources.json`** → **`mcp.cursor`** is applied to **`~/.cursor/mcp.json`** where configured (see **`README.md`**).
- **Product source of truth:** `<repo>/specs/` — features, cross-cutting, architecture, **`specs/PROJECT.md`**.
- **Working artifacts:** `<repo>/specs/knowledge/{research,decisions,searchable}/` — not canonical long-term. When promoted to specs, run **`knowledge-audit`** in the same workflow run where appropriate.
- **Workflows** under **`$AGENT_KIT/workflows/`** that touch `specs/knowledge/` must include **SDD closure** via **`$AGENT_KIT/skills/knowledge-audit/SKILL.md`** (or command `/knowledge-audit`).
- **Bootstrap:** **`$AGENT_KIT/rules/000-project-bootstrap.mdc`**, `/setup-project` (`setup-project.mdc`).

## Skill discovery (unified)

| Need                      | Use                                                                                                                                                   |
|---------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Skill discovery index** | **`skills-index.json`** at repo root (pretty-printed JSON: `id`, `path`, `name`, `description`, `triggers`, `globs`) — same file for humans and tools |
| **Canonical content**     | Each **`skills/**/SKILL.md`** — this is the only source of procedures                                                                                 |
| **Resolution rules**      | **`rules/050-subagent-delegation.mdc`** (ID → path for workflows and Task prompts)                                                                    |

**Do not** maintain a second, hand-curated duplicate of the full skill list in this file. After **`python3 $AGENT_KIT/scripts/kit.py generate`** (or **`generate --skip-vendor`** when you only changed first-party skills), **`skills-index.json`** stays in sync with **`skills/**/SKILL.md`**. Commit the
regenerated index when those files change (see **`README.md`**).

### Ad-hoc tasks (no workflow)

1. Load **`skills-index.json`** and match `description` / `triggers` to the user request and open files.
2. Open the matching **`SKILL.md`** paths with your file-reading tool.
3. If nothing matches, proceed with **`specs/PROJECT.md`** and project rules; optionally delegate broad exploration with **`Task`** (`050-subagent-delegation.mdc`).

### Workflow-driven tasks

Follow the active workflow YAML: each step lists **`skills:`** — resolve each ID to a **`SKILL.md`** and read it before executing the step.

## Architecture reference

- **`rules/016-kit-architecture.mdc`** — kit map: orchestration, workflows, manifest, pointers.
- **`workflows/WORKFLOW_CONTRACT.md`** — YAML workflow shape.
- **`skills/SKILL_FRONTMATTER.md`** — how to write `SKILL.md` metadata for the index.

## Further reading

- **Orchestration:** `rules/010-orchestrator.mdc`, `rules/050-subagent-delegation.mdc`, `rules/100-token-economics.mdc`
- **Engram / persistence:** `rules/014-engram-mcp-protocol.mdc`, `skills/_shared/persistence-contract.md`
- **Imported skills (`gpm-*`):** flat dirs under **`skills/`** only; re-import from **`resources.json`** via **`python3 $AGENT_KIT/scripts/kit.py generate`** when you intentionally refresh upstream copies (conflicts: **`--force`** as needed).
