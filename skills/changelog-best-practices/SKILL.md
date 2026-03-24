---
name: changelog-best-practices
description: Write, maintain, and migrate CHANGELOG.md files using Keep a Changelog format and best practices. Use when creating a changelog from scratch, updating an existing one, fixing/migrating a badly written changelog, or rechecking changelogs version-by-version with an LLM agent (e.g. Cursor) to regenerate human-readable CHANGELOG.md from commits and changed files.
triggers: "CHANGELOG.md, changelog, release notes, keep a changelog, semver, version history, write changelog, update changelog, fix changelog, migrate changelog"
---

# Changelog Best Practices

Apply this skill when: (1) writing a **CHANGELOG.md from scratch**, (2) **maintaining** an existing changelog, (3) **fixing or migrating** a poorly written changelog to a good format, or (4) **rechecking** changelogs of one or more repos version-by-version with an LLM agent (e.g. Cursor on the same
machine) to produce better, commit-standard summaries and regenerate CHANGELOG.md.

**Standard:** [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (KaCh). Mention [Semantic Versioning](https://semver.org/) in the header when the project uses it.

**File:** Use `CHANGELOG.md` (root of project). Markdown only.

---

## 1. Writing from scratch

1. **Add the header** (3–4 lines): title, purpose sentence, format link, optional SemVer link.
2. **Add `## [Unreleased]`** at the top (no date). Under it, add only the standard category subsections that have content (see reference).
3. **Add first release** as `## [X.Y.Z] - YYYY-MM-DD` (ISO 8601). Group changes under `### Added`, `### Changed`, etc. Omit empty sections.
4. **Optional:** At the bottom, add link references for versions (e.g. `[1.0.0]: https://.../compare/v0.9.0...v1.0.0`) so headings are linkable.

Exact header text, category list, and a minimal template are in **references/format-and-migration.md**.

---

## 2. Maintaining an existing changelog

- **Before each release:** Move `[Unreleased]` entries into a new section `## [X.Y.Z] - YYYY-MM-DD`, then clear or repopulate `[Unreleased]` for the next cycle.
- **When adding a change:** Add one line under the right category in `[Unreleased]` (or in the target version section if editing a specific release). Use past tense or imperative; be consistent.
- **Categories:** Use only Added, Changed, Deprecated, Removed, Fixed, Security. Add subsections only when they have at least one entry; omit empty sections.
- **Order:** Newest version first. Dates YYYY-MM-DD only.
- **Do not:** Paste raw git log or commit messages. Curate: one clear line per notable change; combine related commits into a single entry when it helps readers.

---

## 3. Fixing / migrating a bad changelog

**Goals:** One file `CHANGELOG.md`, Keep a Changelog structure, human-focused entries (no raw git dumps), consistent dates and categories.

1. **Inspect current file:** Identify filename (rename to `CHANGELOG.md` if different), structure (sections, dates, categories), and problems (see reference: commit-dump, wrong dates, missing Unreleased, mixed formats).
2. **Normalize structure:**
    - Ensure top-level sections are version headings: `## [X.Y.Z]` or `## [Unreleased]`, with optional ` - YYYY-MM-DD` for releases.
    - Convert any other “version” formats (e.g. “Version 1.0”, “v1.0 (Jan 1)”) to `## [1.0.0] - YYYY-MM-DD`. Use today or the last release date if unknown.
    - Ensure reverse chronological order (newest first). Reorder sections if needed.
3. **Map to standard categories:** Group existing entries under Added, Changed, Deprecated, Removed, Fixed, Security. Merge or split lines so each entry is one clear, user-facing sentence. Remove noise (merge commits, “bump version”, purely internal refactors unless user-visible).
4. **Add missing pieces:** If there is no header, add the standard header. If there is no `[Unreleased]`, add it at the top. If dates are wrong format (e.g. DD/MM/YYYY or “Jan 1, 2024”), convert to YYYY-MM-DD.
5. **Preserve history:** Prefer editing in place. Only rewrite past entries when they are wrong or misleading (e.g. missing breaking change). Do not delete released versions; fix or clarify them.
6. **Optional:** Add link references at the bottom for each version so `[X.Y.Z]` and `[Unreleased]` are linkable (see reference).

For **migration patterns** (e.g. “Version 1.0” → `## [1.0.0]`, date conversion, splitting commit-dump into categories), see **references/format-and-migration.md**.

---

## 4. Recheck changelogs (version-per-version with LLM agent / Cursor)

**Use when:** The user asks to **recheck the changelogs** of one or more repos. Work **version by version**, including **all commits** per version. Use an **LLM agent (Cursor on the same machine)** to retrieve changed files, generate better commit-style summaries
following [Conventional Commits](https://www.conventionalcommits.org/), and **regenerate CHANGELOG.md** for that project.

**Workflow:**

1. **Generate context per repo:** Run **scripts/recheck-changelog-context.py** for each repo:
    - Discovers versions from existing `CHANGELOG.md` (sections `## [X.Y.Z]`) or from git tags (`v*` semver) with `--from-tags`.
    - For each version, collects **all commits** in that version's range.
    - Per commit: records hash, subject, body, date, and **list of changed files** (`git show --name-status`).
    - Writes a structured context file (default: `.changelog-recheck-context.md` in the repo root) for the agent.
2. **Per version with the LLM (Cursor):** For each version in the context file:
    - **Retrieve changed files:** Use the listed commits and their changed-file lists; when the subject/body is unclear, run `git show <hash>` (or read the file at that path) to inspect the actual diff and understand what changed.
    - **Generate better commit messages:** Propose a **conventional-commit-style** line per commit: `type(scope): description` (e.g. `fix(auth): validate email before submit`, `feat(api): add pagination to list endpoint`). Types: feat, fix, docs, style, refactor, perf, test, chore; scope and
      description from the real change, not the raw message.
    - **Map to KaCh categories:** From the refined understanding, map each notable change to **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**. Combine related commits into **one human-focused line per notable change**; skip trivial or internal-only changes unless
      user-visible.
3. **Regenerate CHANGELOG.md:** Write the project's `CHANGELOG.md`:
    - Standard KaCh header (see references/format-and-migration.md).
    - `## [Unreleased]` at the top (empty or with current work if any).
    - One section per version: `## [X.Y.Z] - YYYY-MM-DD` with category subsections and the human-focused entries.
    - Optionally append commit-hash links: `([short_hash](https://github.com/org/repo/commit/short_hash))` for traceability.

**Script usage:**  
`python3 path/to/changelog-best-practices/scripts/recheck-changelog-context.py [--output FILE] [--from-tags] [REPO_DIR]`

- `REPO_DIR` defaults to current directory.
- Use `--from-tags` to derive versions only from git tags (ignore existing CHANGELOG).
- Output path defaults to `.changelog-recheck-context.md` in the repo; add that path to `.gitignore` if you do not want to commit the generated context.

**Agent instructions (Cursor):** Follow **references/recheck-with-llm.md** to read the context file, retrieve changed files/diffs when needed, produce conventional-commit-style summaries, map to KaCh categories, and write the new CHANGELOG.

---

## Rules (all scenarios)

- **Changelogs are for humans.** Every line should answer “what does this mean for the user?” — not “what commit was made?”
- **One entry per notable change.** Combine related commits into one line; skip trivial or internal-only changes unless they affect behavior.
- **Consistent wording.** Use past tense (“Added X”) or imperative (“Add X”); pick one and stick to it in the file.
- **No empty sections.** Omit a category if there are no changes for that release.
- **Deprecations and breaking changes:** Always list under Deprecated/Removed or in Changed with a clear note. Users must see what will break before upgrading.
- **Yanked releases:** Use `## [X.Y.Z] - YYYY-MM-DD [YANKED]` and keep the section so the version is still documented.

---

## Script: normalize-changelog.py

For **terraform-version-commit** (or similar) generated changelogs that contain commit hashes, `### Summary` blocks, and excessive blank lines:

- Run **scripts/normalize-changelog.py** on each `CHANGELOG.md` to: add the standard KaCh header and `[Unreleased]`, sort versions newest-first, replace commit-hash lines and `### Summary` with one human-readable `### Changed` line per release, **keep the commit hash as a link to the repo** (e.g.
  `([2be7436](https://github.com/org/repo/commit/2be7436))`), and collapse excess blank lines.
- Usage: `python3 path/to/changelog-best-practices/scripts/normalize-changelog.py [--repo-url=URL] <CHANGELOG.md>`
- **Commit links:** The script infers the repo URL from `git remote get-url origin` in the changelog’s directory and appends the short hash as a link (e.g. `... changes. ([abc1234](https://github.com/org/repo/commit/abc1234))`). Use `--repo-url=https://github.com/org/repo` to override when not in a
  git repo or when remote is different.
- The script preserves real content (e.g. `### Migration` and custom bullets). Summary text is mapped to short phrases (e.g. "Minor Terraform and config changes.", "Major provider upgrade (breaking).").

---

## When to read the reference

- **Exact template or category list** → references/format-and-migration.md
- **Migration heuristics** (wrong dates, commit-dump, non-standard headings) → references/format-and-migration.md
- **Optional link reference syntax** (compare URLs, Unreleased link) → references/format-and-migration.md
- **Recheck changelogs with Cursor/LLM** (version-per-version, changed files, conventional commits, regenerate CHANGELOG) → references/recheck-with-llm.md  
