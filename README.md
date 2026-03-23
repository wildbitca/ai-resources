# ai-resources

Single repository for **rules/skills/workflows**, **imported skills** (flat `gpm-*` trees), and the **generated skill index** (`skills-index.json`). IDE-agnostic pieces can bootstrap Claude, Gemini, OpenCode, etc. via **`python3 scripts/kit.py setup`**.

**`~/.cursor`** on your machine should hold **only Cursor-generated state** (extensions, chats, projects, caches). Hand-authored rules and skills live **in this repo** (`rules/`, `skills/`, …). You do not need to copy them into `~/.cursor` unless you add an install step that symlinks or links rules
there.

## Quick reference (`AGENT_KIT`)

Set **`AGENT_KIT`** to the root of this install (git clone or Homebrew prefix). **`scripts/kit.py`** resolves the repo from its own path when **`AGENT_KIT`** is unset.

| What                                     | Where                                                                                                                                   |
|------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| Rules, workflows, skills, templates      | `$AGENT_KIT/rules/`, `$AGENT_KIT/workflows/`, `$AGENT_KIT/skills/`, `$AGENT_KIT/templates/`                                             |
| Agents (roles / personas)                | `$AGENT_KIT/agents/`                                                                                                                    |
| Skill index (generated; humans + tools)  | `$AGENT_KIT/skills-index.json`                                                                                                          |
| Policy + discovery guide                 | `$AGENT_KIT/AGENTS.md` (not a duplicate skill list)                                                                                     |
| Update kit + re-import + index           | Homebrew: `brew upgrade <formula>` then **`python3 $AGENT_KIT/scripts/kit.py generate`**. Git checkout: `git pull` then the same.       |
| Machine / IDE bootstrap + workflow check | **`python3 $AGENT_KIT/scripts/kit.py setup`** — see below                                                                               |
| Last setup record (optional)             | **`~/.config/ai-resources/state.json`** — `agent_kit`, `targets`, `updated_at` after **`setup`** (not used to override **`AGENT_KIT`**) |
| Engram / delegation (entry rules)        | `$AGENT_KIT/rules/014-engram-mcp-protocol.mdc`, `$AGENT_KIT/rules/050-subagent-delegation.mdc`                                          |

## Layout

| Path                    | Role                                                                                         |
|-------------------------|----------------------------------------------------------------------------------------------|
| `rules/`                | Cursor rules (`.mdc`), orchestration, token economics, etc.                                  |
| `workflows/`            | YAML workflows                                                                               |
| `skills/`               | Flat skill dirs `{skill_id}/SKILL.md` (first-party + `gpm-*` imports)                        |
| `resources.json` (root) | Unified manifest; `skills.sources[]` drives the **import** step inside **`kit.py generate`** |
| `agents/`               | Shared role and persona definitions                                                          |
| `templates/`            | Project and MCP templates                                                                    |
| `AGENTS.md`             | Orchestration policy; points to generated indexes                                            |
| `scripts/kit.py`        | CLI: **`generate`**, **`setup`** (`python3 scripts/kit.py --help`)                           |

## `resources.json` (unified manifest)

Single JSON at repo root. **No separate markdown spec** — read the file and this section.

| Section                                   | Role                                                                                                                                                                      |
|-------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `skills.sources[]`                        | **Active** — `git` sources merged into **`skills/`** when you run **`kit.py generate`** (flat ids `gpm-*`, `.skill-source.yaml` per imported dir).                        |
| `agents.sources[]` / `personas.sources[]` | Reserved for future sync (import step warns if non-empty).                                                                                                                |
| `mcp`                                     | Machine presets — e.g. **`mcp.cursor`** → applied by **`kit.py setup`** → `~/.cursor/mcp.json`. Secrets: use `${env:VAR}` / envFile per **`rules/200-security-mcp.mdc`**. |

## CLI

The kit exposes **two** commands:

### `generate`

Import skills from **`resources.json`** into **`skills/`**, then build **`skills-index.json`**.

```bash
python3 scripts/kit.py generate
python3 scripts/kit.py generate --dry-run    # preview import; index still rebuilt from disk
python3 scripts/kit.py generate --skip-vendor   # only rebuild the index (no git imports)
python3 scripts/kit.py generate --force      # on vendor conflicts, overwrite when intended
```

### `setup`

Configure the machine and IDEs: apply **`mcp.*`** from **`resources.json`**, write pointer files under each tool’s home (`~/.cursor`, `~/.claude`, …), **validate** `workflows/*.workflow.yaml` against **`skills/`**, and optionally rewrite workflow skill ids.

```bash
python3 scripts/kit.py setup
python3 scripts/kit.py setup --target cursor
python3 scripts/kit.py setup --dry-run              # preview MCP + stub paths (no writes)
python3 scripts/kit.py setup --skip-workflow-check  # skip validation
python3 scripts/kit.py setup --fix-workflow-ids     # rewrite slash-style ids in workflows (in-place)
```

After a successful **`setup`** (not **`--dry-run`**), **`~/.config/ai-resources/state.json`** records **`agent_kit`**, **`targets`**, and **`updated_at`** for auditing.

## Environment

| Variable            | Default                                                   |
|---------------------|-----------------------------------------------------------|
| `AGENT_KIT`         | Parent of `scripts/` when running `kit.py` from this repo |
| `AGENT_SKILLS_ROOT` | `$AGENT_KIT/skills`                                       |
| `SKILLS_INDEX_OUT`  | `$AGENT_KIT/skills-index.json`                            |

## Generated artifacts

After changing **`resources.json`** or refreshing vendors, run **`python3 scripts/kit.py generate`**. After editing first-party skills only, **`python3 scripts/kit.py generate --skip-vendor`** is enough.

| File                | Purpose                                                                                                                               |
|---------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| `skills-index.json` | Single index (pretty-printed JSON): `id`, `path`, `name`, `description`, `triggers`, `globs` — use for discovery by humans and agents |

There are **no** separate `*.generated.*` copies or parallel Markdown registries.

**Commits:** Regenerate **`skills-index.json`** with **`kit.py generate`** (or **`generate --skip-vendor`** if only first-party skills under **`skills/`** changed) before merging changes that add, remove, or materially edit **`SKILL.md`** files or **`resources.json`** imports, so discovery stays
aligned.

## Further reading (in-repo)

| Topic                                   | Location                                                                                                     |
|-----------------------------------------|--------------------------------------------------------------------------------------------------------------|
| Architecture / orchestration map        | `rules/016-kit-architecture.mdc` (overview; CLI and layout on this page)                                     |
| Workflow YAML shape                     | `workflows/WORKFLOW_CONTRACT.md`                                                                             |
| Skill frontmatter                       | `skills/SKILL_FRONTMATTER.md`                                                                                |
| Engram MCP / persistence                | `rules/014-engram-mcp-protocol.mdc`, `rules/300-engram-memory.mdc`, `skills/_shared/persistence-contract.md` |
| Token economics / delegation discipline | `rules/100-token-economics.mdc`                                                                              |
| Handoff protocol                        | `rules/051-handoff-protocol.mdc`, `handoff.md.template`                                                      |

## Cursor orchestration

Rules live in **`rules/`** (e.g. `010-orchestrator.mdc`, `014-engram-mcp-protocol.mdc`, `015-agent-agnostic-paths.mdc`). Slash **`/self-update`** maps to refreshing the kit and running **`kit.py generate`** — see `rules/self-update.mdc`.
