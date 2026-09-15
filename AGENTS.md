# Agent skills & orchestration policy

> [!IMPORTANT]
> **Prefer retrieval-led reasoning over pre-training-led reasoning.**
> When a skill matches the task, read its `SKILL.md` before acting — do not rely on memory alone.

## Rule Zero: Zero-Trust engineering

- **Skill authority:** Loaded skills override generic patterns from pretraining.
- **Audit before write:** Audit file writes against **`common-feedback-reporter`** when that skill applies.

## How the kit is delivered

- **`ai-resources setup`** links every skill into the agent's native skills directory (`~/.claude/skills/<id>`, `~/.agents/skills/<id>`), generates the role subagents, installs the kit hooks, and writes a short managed block into each tool's instruction file. Content outside that block is never touched.
- **`ai-resources generate`** imports vendor skills from **`resources.json`**, turns each `workflows/*.workflow.yaml` into a `workflow-<name>` skill, and rebuilds **`skills-index.json`** (for tools without native skill discovery).
- After updating the install (`brew upgrade ai-resources` or `git pull`), run `ai-resources generate` and `ai-resources setup`.

## Orchestration

| Need | Where |
|------|-------|
| Pick a workflow | The `workflow-*` skills — each description is the workflow's trigger |
| Run a workflow (steps, handoff, parallel groups, return format) | Skill **`kit-orchestration`** |
| Workflow YAML shape | **`workflows/WORKFLOW_CONTRACT.md`** |
| Domain commands | **`workflows/_domain-commands.yaml`** |
| Roles and personas | **`agents/roles/`**, **`agents/personas/`** |
| Commands | Skills `delegate`, `setup-project`, `self-update`, `knowledge-audit` |

## Project repos: specs vs knowledge (SDD)

- **Product source of truth:** `<repo>/specs/` — features, cross-cutting, architecture, **`specs/PROJECT.md`**.
- **Working artifacts:** `<repo>/specs/knowledge/{research,decisions,searchable}/` — not canonical long-term. When promoted to specs, run **`knowledge-audit`** in the same workflow run.
- **Workflows** that touch `specs/knowledge/` include SDD closure via the **`knowledge-audit`** skill.
- **Bootstrap:** skill **`setup-project`** and **`rules/000-project-bootstrap.mdc`**.

## Skills

- **Canonical content:** each **`skills/<id>/SKILL.md`** — the only source of procedures. Ids are flat and globally unique.
- **Index:** `skills-index.json` is generated; do not hand-maintain a skill list anywhere else.
- **Imported skills (`gpm-*`):** re-import from **`resources.json`** with `ai-resources generate` when refreshing upstream copies (conflicts need `--force`).

## Further reading

- **Kit map:** `rules/016-kit-architecture.mdc`
- **Engram / persistence:** `rules/014-engram-mcp-protocol.mdc`, `rules/300-engram-memory.mdc`, `skills/_shared/persistence-contract.md`
- **Multi-model routing:** `rules/017-multimodel-routing.mdc`, `docs/multi-model.md`
