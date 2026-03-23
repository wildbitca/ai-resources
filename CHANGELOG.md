# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

## [Unreleased]

## [0.1.0] — 2025-03-23

First tagged release. SemVer starts at **0.1.0**; bump **minor** for features, **patch** for fixes, **major** when breaking compatibility (see [SemVer](https://semver.org/)).

### Added

- Unified CLI `scripts/kit.py`: **`generate`** (vendor import from `resources.json` + `skills-index.json`), **`setup`** (MCP presets, IDE stubs, workflow validation, optional workflow id rewrite).
- `resources.json` manifest; optional `~/.config/ai-resources/state.json` after `setup`.
- Homebrew formula template: `packaging/homebrew/Formula/ai-resources.rb` (command **`ai-resources`**).

### Changed

- **Documentation:** shorter root `README.md`; release history lives in this file. Removed `RELEASING.md`, `packaging/homebrew/README.md`, `tag-release.sh`, and `bump-formula-sha.sh` — releases are driven by the **release manager** (maintainer or assistant); see **Release manager checklist** below.

---

When you publish **`vMAJOR.MINOR.PATCH`**, add a new section above `[Unreleased]` with that version and date, then move the relevant items from `[Unreleased]` into it.

## Release manager checklist

The person or assistant cutting a release should do this in order (no helper scripts in-repo):

1. **Pick the next SemVer** (e.g. `1.2.0`) from what’s in `[Unreleased]` and team agreement.
2. **Edit `CHANGELOG.md`:** move items under `[Unreleased]` into a new section `## [1.2.0] — YYYY-MM-DD` (use the real date).
3. **Commit** the changelog on `main` (and any other release commits).
4. **Tag and push:**  
   `git tag -a v1.2.0 -m "Release 1.2.0"`  
   `git push origin v1.2.0`
5. **SHA256 for Homebrew** (after the tag exists on GitHub, replace `wildbitca` if needed):  
   `curl -fsSL "https://github.com/wildbitca/ai-resources/archive/refs/tags/v1.2.0.tar.gz" | shasum -a 256`
6. **Update `packaging/homebrew/Formula/ai-resources.rb`:** set `url` to that tarball, `sha256` to the hash from step 5, `version` to `1.2.0` (and bump `revision` only if you re-release the same version without a new tag). If the hash in the formula changes the tarball, recompute and update until stable, then move the tag to the final commit if needed.
7. **Publish the tap:** copy or merge the updated formula into the **homebrew tap** repo; commit and push so users can `brew update && brew upgrade ai-resources`.
8. **Optional:** create a **GitHub Release** on `v1.2.0` with notes from the changelog section.
