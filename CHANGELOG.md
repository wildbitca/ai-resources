# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

## [Unreleased]

## [0.3.1] — 2026-03-24

### Added

- **Workflow Discovery Protocol:** `_discovery_recipe()` now generates a "Workflow Discovery Protocol" section **before** Skill Discovery in every agent stub. The section includes a dynamically-built trigger table (scanned from `workflows/*.workflow.yaml` at setup time) and a MANDATORY rule that forbids substituting built-in tools (e.g. `EnterPlanMode`, ad-hoc Plan agents) for workflow-defined phases.
- **`_scan_workflow_triggers()` helper:** reads all `.workflow.yaml` files and extracts `name` + `trigger` fields for the generated table.
- **`_workflow_recipe()` helper:** builds the Workflow Discovery Protocol markdown from scanned triggers — single source, shared across all 9 agent stubs.
- **Workflow contract** added to Key paths table in generated stubs.

### Changed

- **All 9 agent stubs updated:** Claude Code, Cursor, Gemini CLI, OpenCode, Codex, GitHub Copilot, Windsurf, Continue.dev, and Aider now receive workflow discovery instructions alongside skill discovery — workflows are evaluated first.

## [0.3.0] — 2026-03-24

### Added

- **Universal Skill Discovery Protocol:** every agent stub now includes a 5-step discovery recipe that tells the agent how to read `skills-index.json`, match skills by description/triggers, and load the relevant `SKILL.md` files on demand. This replaces the previous minimal pointer stubs.
- **New setup targets:** `windsurf` (`~/.codeium/windsurf/memories/global_rules.md`), `continue` (`~/.continue/AGENT_KIT.md`), `aider` (`~/.aider/CONVENTIONS.md`). Total: 9 agent targets + MCP + symlinks.
- **Skill symlinks for native discovery:** `kit.py setup` creates symlinks from `~/.claude/skills/ai-resources` and `~/.agents/skills/ai-resources` (Codex) to the centralized `skills/` directory, enabling native SKILL.md discovery in agents that support it.
- **`_discovery_recipe()` helper:** single-source function that generates the discovery instructions, shared across all agent stubs for consistency.

### Changed

- **All existing stubs rewritten:** Claude Code, Cursor, Gemini CLI, OpenCode, Codex, and GitHub Copilot stubs now include the full discovery protocol and refresh hints (previously only had minimal path references).
- **`cmd_setup` refactored:** dispatch table replaces if/elif chain — easier to extend with new agent targets.
- **Stub function signatures unified:** all `_write_*_stub` functions now receive `(ak_s, hint, *, dry_run)` for consistency.
- **CLI `--target` choices expanded:** added `windsurf`, `continue`, `aider` to the argparse choices.

## [0.2.1] — 2026-03-23

### Changed

- **Skills, agents, roles, workflows:** removed all references to internal projects, companies, and repository names; kit is now fully agnostic and reusable by any team or organization.
- **`flutter-icons` skill:** brand color placeholder (`#006a64`) in config examples now carries explicit `← Replace with your brand color` labels on every occurrence to prevent agents from applying it literally.
- **Terraform scripts:** `upgrade-providers.py` regex now matches any Terraform Cloud org (not hardcoded); `upgrade-all-with-deps.py` renamed `WILDBIT_MODULE_RE` → `TF_PRIVATE_MODULE_RE`.

### Fixed

- **`terraform-version-commit` script:** removed non-generic auto-detect fallback for dependent projects; `--dependent-projects` must now be passed explicitly (safe default: no dependents processed if omitted).
- **`maintain.sh`:** dependent-projects argument is now only forwarded when `DEPENDENT_PROJECTS` env var is set (no fictitious default path).

## [0.2.0] — 2026-03-23

### Added

- **Engram como dependencia Homebrew:** `Formula/ai-resources.rb` declara `depends_on "gentleman-programming/tap/engram"` — `brew install ai-resources` instala el binario `engram` automáticamente.
- **Setup automático de engram para Claude Code:** `kit.py setup` (targets `claude` y `all`) ejecuta `claude plugin marketplace add Gentleman-Programming/engram && claude plugin install engram`, registrando el servidor MCP, hooks y la Memory Protocol skill sin pasos manuales.
- **MCP engram en Cursor:** `resources.json` → `mcp.cursor.mcpServers` incluye la entrada `engram` (`engram mcp` stdio); se aplica al correr `kit.py setup`.

### Changed

- `Formula/ai-resources.rb`: tag y version actualizados a `v0.2.0`; eliminado `revision 1` (era un parche de build, no aplica a la nueva versión).

## [0.1.0] — 2026-03-23

Primera release **usable** con Homebrew. El tag **`v0.1.0`** anterior no permitía instalar bien (tap inexistente / fórmula mal ubicada / `brew tap` sin URL apuntando a otro repo); el tag actual incluye **`Formula/ai-resources.rb`** en la raíz y la orden de tap correcta.

### Added

- CLI unificado `scripts/kit.py`: **`generate`** (import desde `resources.json` + `skills-index.json`), **`setup`** (MCP, IDE stubs, validación de workflows).
- Manifiesto `resources.json`; estado opcional `~/.config/ai-resources/state.json` tras `setup`.
- **Homebrew:** fórmula **`Formula/ai-resources.rb`** en la raíz; **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (obligatoria la URL: sin ella Homebrew busca `wildbitca/homebrew-ai-resources`) + **`brew install ai-resources`**. Instalación por **git** en el tag (sin `sha256` de tarball). Repo privado: `HOMEBREW_GITHUB_API_TOKEN`.

### Changed

- **README** orientado a instalación y uso con comandos mínimos.
- **Documentación:** historial en este archivo; releases sin scripts auxiliares en repo (ver checklist abajo). Eliminados `RELEASING.md`, `packaging/homebrew/README.md`, `tag-release.sh`, `bump-formula-sha.sh`.
- **Homebrew:** mismo repositorio que código y tap (ya no hace falta `homebrew-ai-resources` aparte); eliminado `packaging/homebrew/`.

### Fixed

- Comando **`ai-resources`**: el wrapper ya no usa una ruta fija a `opt/python@3.12/bin/python3` (podía no existir); añade el `bin` de `python@3.12` al `PATH` y usa **`python3`** o **`python3.12`**. Fórmula **`revision 1`**.

---

When you publish **`vMAJOR.MINOR.PATCH`**, add a new section above `[Unreleased]` with that version and date, then move the relevant items from `[Unreleased]` into it.

## Release manager checklist

1. Elegir SemVer y mover entradas en **`CHANGELOG.md`**.
2. Actualizar **`Formula/ai-resources.rb`:** `tag:` y `version` acordes al nuevo tag.
3. Commit en `main` y **push**.
4. **Tag y push:** `git tag -a vX.Y.Z -m "Release X.Y.Z"` · `git push origin vX.Y.Z`
5. Opcional: GitHub Release. Usuarios: `brew update && brew upgrade ai-resources` (privado: `HOMEBREW_GITHUB_API_TOKEN`). El **README** debe mantener **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (no omitir la URL).
