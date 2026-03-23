# ai-resources

Single repository for **CLI**, **Cursor rules/skills/workflows**, **vendor skill packs**, and **generated discovery indexes** (`skills-registry.md`, `skills-index.json`). IDE-agnostic pieces can bootstrap Claude, Gemini, OpenCode, etc. via `agent-kit setup`.

**`~/.cursor`** on your machine should hold **only Cursor-generated state** (extensions, chats, projects, caches). Hand-authored rules and skills live here under **`cursor/`** — not under `~/.cursor` unless you later add an install step that symlinks.

## Layout

| Path | Role |
|------|------|
| `bin/agent-kit` | CLI entry (sets `AGENT_KIT` to repo root) |
| `cursor/` | Rules (`.mdc`), workflows, skills, agents, templates, `AGENTS.md` |
| `vendor-manifest.json` | Third-party skill repos to copy into `cursor/skills/vendor/` |
| `scripts/` | `vendor_sync.py`, `generate-artifacts.sh`, generators |

## CLI

Put the repo on `PATH` or symlink `~/bin/agent-kit` → `<clone>/bin/agent-kit`.

```bash
export PATH="/path/to/ai-resources/bin:$PATH"
# optional explicit root:
export AGENT_KIT=/path/to/ai-resources
```

| Command | What it does |
|---------|----------------|
| `agent-kit self-update` | `git pull` this repo (if `.git` exists) + **vendor-sync** + **generate** |
| `agent-kit vendor-sync` | Clone/copy vendors from `vendor-manifest.json` → `AGENT_SKILLS_ROOT` |
| `agent-kit generate` | Regenerate `cursor/skills-registry.md` + `cursor/skills-index.json` |
| `agent-kit setup <target>` | Stubs: `cursor` \| `claude` \| `gemini` \| `opencode` \| `codex` \| `vscode` \| `all` |

## Environment

| Variable | Default |
|----------|---------|
| `AGENT_KIT` | Directory containing `bin/agent-kit` (auto-set by the script) |
| `AGENT_SKILLS_ROOT` | `$AGENT_KIT/cursor/skills` |
| `SKILL_REGISTRY_OUT` | `$AGENT_KIT/cursor/skills-registry.md` |
| `SKILLS_INDEX_OUT` | `$AGENT_KIT/cursor/skills-index.json` |

## Generated artifacts

After adding/removing skills or vendors, run **`agent-kit generate`** (or **`self-update`**).

| File | Purpose |
|------|---------|
| `cursor/skills-registry.md` | Human-readable table of every `SKILL.md` path |
| `cursor/skills-index.json` | Machine-readable: `id`, `path`, `name`, `description`, `triggers`, `globs` |

## Third-party skills (vendors)

Edit **`vendor-manifest.json`**, then **`agent-kit vendor-sync`**. Default vendor: **Gentleman-Skills**.

Legacy: `scripts/sync-gentleman-skills.sh` delegates to **`agent-kit vendor-sync`** + **`generate`**.

## IDE bootstrap (`agent-kit setup`)

Creates minimal pointers so each tool knows where the repo lives:

| Target | What gets written |
|--------|---------------------|
| `cursor` | `~/.cursor/AGENT_KIT.md` (pointer to `$AGENT_KIT`; does not replace your whole `~/.cursor`) |
| `claude` / `gemini` / … | Short instruction files under respective home dirs |

Run `agent-kit setup all` once per machine (idempotent).

## Docs

- `docs/engram-protocol-summary.md` — Engram two-step read
- `docs/token-economics.md` — delegation discipline
- `docs/handoff-envelope.md` — sub-agent result shape
- `MIGRATION.md` — policy when moving off global `~/.cursor` + `~/agent-kit`

## Cursor orchestration

Rules live in **`cursor/rules/`** (e.g. `010-orchestrator.mdc`, `014-engram-mcp-protocol.mdc`, `015-agent-agnostic-paths.mdc`). Slash **`/self-update`** maps to **`agent-kit self-update`** — see `cursor/rules/self-update.mdc`.
