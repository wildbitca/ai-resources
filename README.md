# ai-resources

Shared **rules**, **skills**, **workflows**, and a small **`kit.py`** CLI (`generate`, `setup`). Discovery uses **`skills-index.json`**; policy is **`AGENTS.md`**. **`~/.cursor`** is for IDE state only — rules and skills live **here**, not copied into `~/.cursor` unless you choose to.

**`AGENT_KIT`** = repo root (or unset when running `scripts/kit.py` from this tree).

| What                       | Where                                        |
|----------------------------|----------------------------------------------|
| Rules / workflows / skills | `$AGENT_KIT/rules/`, `workflows/`, `skills/` |
| Index (generated)          | `$AGENT_KIT/skills-index.json`               |
| Manifest                   | `resources.json` (root)                      |
| CLI                        | `python3 scripts/kit.py --help`              |

| Command    | Role                                                                                                               |
|------------|--------------------------------------------------------------------------------------------------------------------|
| `generate` | Import from `resources.json` → `skills/`, then build `skills-index.json` (`--skip-vendor`, `--dry-run`, `--force`) |
| `setup`    | MCP + IDE stubs + workflow check (`--target`, `--dry-run`, `--fix-workflow-ids`, …)                                |

**Env:** `AGENT_KIT`, `AGENT_SKILLS_ROOT` (default `$AGENT_KIT/skills`), `SKILLS_INDEX_OUT` (default `$AGENT_KIT/skills-index.json`).

If you change `skills/**` or imports, run **`generate`** and commit **`skills-index.json`** when appropriate.

## Homebrew

- Formula: **`packaging/homebrew/Formula/ai-resources.rb`** → installs the tree and exposes **`ai-resources`** (wrapper around `kit.py`).
- Private tap: separate repo (e.g. `homebrew-ai-resources`); employees `brew tap` + `brew install ai-resources` (PAT may be needed for private GitHub).

After each **git tag** `vX.Y.Z`, update the formula’s **`url`**, **`sha256`**, and **`version`**, then publish the tap — see **`CHANGELOG.md` → Release manager checklist**.

## Changelog

See **`CHANGELOG.md`** (includes release steps for the maintainer / assistant).

## More

| Topic             | File                             |
|-------------------|----------------------------------|
| Orchestration map | `rules/016-kit-architecture.mdc` |
| Workflow YAML     | `workflows/WORKFLOW_CONTRACT.md` |
| Skill frontmatter | `skills/SKILL_FRONTMATTER.md`    |
| Self-update       | `rules/self-update.mdc`          |
