# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

## [Unreleased]

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
