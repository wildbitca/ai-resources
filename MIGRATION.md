# Migration from `~/.cursor` + `~/agent-kit`

## Canonical location

All AI setup **source code** lives in this repository **`ai-resources`**:

| Path | Contents |
|------|----------|
| `cursor/rules/` | Cursor rules (`.mdc`) |
| `cursor/workflows/` | Workflow YAML |
| `cursor/skills/` | Skills + `vendor/` |
| `cursor/agents/` | Personas / roles |
| `cursor/templates/` | Templates |
| `cursor/AGENTS.md` | Skill index |
| `bin/agent-kit` | CLI (`self-update`, `vendor-sync`, `generate`, `setup`) |
| `scripts/` | Generators, `vendor_sync.py` |
| `vendor-manifest.json` | Third-party skill packs |

## Environment

- **`AGENT_KIT`** — absolute path to this repository root (auto-set by `bin/agent-kit`).
- **`AGENT_SKILLS_ROOT`** — defaults to **`$AGENT_KIT/cursor/skills`**.

## `~/.cursor` policy

The global **`~/.cursor`** directory should contain **only Cursor IDE runtime state** (extensions, chats, projects, caches, etc.), not the git-tracked rules/skills/workflows. Those files were **removed** from `~/.cursor` after copying here.

An **install** step (later) will link or copy from this repo into `~/.cursor` as needed for Cursor to load rules.

## Old `~/agent-kit`

Superseded by this repo; safe to delete after verifying this clone is complete.
