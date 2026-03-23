# Recheck Changelog with LLM Agent (Cursor)

This reference tells an **LLM agent running on the same machine as the repo** (e.g. Cursor) how to recheck a project's changelog **version by version**, using **all commits** and **changed files** to produce conventional-commit-style summaries and regenerate **CHANGELOG.md**.

---

## Prerequisites

- The **context file** has been generated:  
  `python3 path/to/changelog-best-practices/scripts/recheck-changelog-context.py [--output FILE] [--from-tags] [REPO_DIR]`  
  Default output: `REPO_DIR/.changelog-recheck-context.md`.
- The agent has read access to the repo (can run `git` and read files).

---

## Step 1: Read the context file

- Open **`.changelog-recheck-context.md`** (or the path given to `--output`) in the repo root.
- Structure:
    - Header with repo path and commit base URL.
    - For each **version**: a section `## Version: X.Y.Z - YYYY-MM-DD` (date may be missing).
    - Under each version: for **each commit**, a block:
        - `### Commit <short_hash> — <subject>`
        - **Date**, **Body** (if any), **Changed files** (list of status + path, e.g. `M src/foo.py`).

- Process **one version at a time**, in the order given (newest first in the file). For each version you will:
    1. Consider every listed commit and its changed files.
    2. Optionally retrieve full diffs for unclear commits.
    3. Propose a conventional-commit-style line per commit.
    4. Map to KaCh categories and merge into one human-focused line per notable change.

---

## Step 2: Retrieve changed files and diffs when needed

- The context already lists **changed files** per commit (from `git show --name-status`).
- If the **subject/body is vague** (“Fix stuff”, “Update”, “WIP”) or you need to distinguish breaking vs non-breaking:
    - Run in the repo:  
      `git show <full_hash> --stat`  
      or  
      `git show <full_hash>`  
      to see the actual diff.
    - Or read the changed files at that commit (e.g. `git show <hash>:path/to/file`) to understand what was added/removed.
- Use this to write a **precise** conventional-commit-style line and to assign the right KaCh category.

---

## Step 3: Generate conventional-commit-style lines per commit

For **each commit** in the version:

- Produce one line in [Conventional Commits](https://www.conventionalcommits.org/) form:  
  **`type(scope): description`**
- **Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`. Use `feat` for user-facing new features, `fix` for bug fixes, `docs` for docs-only, `chore` for tooling/build/deps, etc.
- **Scope:** optional; short (e.g. `auth`, `api`, `terraform`).
- **Description:** imperative, lowercase (no period). Based on **what the diff does**, not the original message.

Examples:

- Original: "fix bug" + changed file `auth/validator.py` → `fix(auth): validate email before submit`
- Original: "bump deps" + only `package.json` / `go.mod` → `chore(deps): bump dependencies`
- Original: "WIP" + new file `docs/api.md` → `docs(api): add API overview`

Keep these lines as an internal list per version; you will merge them into KaCh entries in Step 4.

---

## Step 4: Map to KaCh categories and write one line per notable change

- Map each conventional-commit line (or group of related commits) to **Keep a Changelog** categories:
    - **Added** — new features
    - **Changed** — changes in existing behavior (including breaking changes; say "Breaking: ..." when relevant)
    - **Deprecated** — soon-to-be removed
    - **Removed** — removed features
    - **Fixed** — bug fixes
    - **Security** — vulnerabilities

- **Merge related commits** into a single user-facing bullet (e.g. "Add login form and client-side validation" instead of two separate "Add login" / "Add validation").
- **Skip** merge commits, "Bump version", "Update CHANGELOG", and purely internal refactors unless they change user-visible behavior.
- **One line per notable change**; use past tense or imperative consistently (e.g. "Added X" or "Add X" throughout the file).

---

## Step 5: Regenerate CHANGELOG.md

- **Header:** Use the standard KaCh header (see references/format-and-migration.md). Include SemVer if the project uses it.
- **Unreleased:** Add `## [Unreleased]` at the top. Leave empty or add current work if the user wants it.
- **Per version:** For each version in the context file (newest first):
    - Add `## [X.Y.Z] - YYYY-MM-DD`. Use the date from the context if present; otherwise use a sensible date (e.g. tag date from `git log -1 --format=%ad --date=short <tag>`).
    - Under that version, add only the category subsections that have entries: `### Added`, `### Changed`, etc.
    - Under each subsection, add the human-focused bullets you derived in Step 4.
- **Commit links (optional):** For traceability, append the short hash as a link:  
  `- Fixed login redirect. ([abc1234](https://github.com/owner/repo/commit/abc1234))`  
  Use the repo's commit URL from the context file (e.g. `Commit base URL: https://github.com/owner/repo` → `.../commit/<short_hash>`).
- **Order:** Newest version first; no empty version sections.

---

## Step 6: Write the file and optional link references

- Write the result to **`CHANGELOG.md`** in the repo root (overwriting or replacing the previous content).
- Optionally add **link references** at the bottom (see references/format-and-migration.md) so `[X.Y.Z]` and `[Unreleased]` are clickable in GitHub/GitLab.

---

## Summary for the agent (Cursor)

1. Run **recheck-changelog-context.py** if the context file is missing or stale.
2. Read **.changelog-recheck-context.md** (or given output path).
3. For **each version**, for **each commit**: use changed files and, if needed, `git show <hash>` to understand the change; then write a **conventional-commit-style** line.
4. Map those lines to **KaCh categories** and merge into **one human-focused line per notable change**.
5. **Regenerate CHANGELOG.md**: standard header, `[Unreleased]`, then one section per version with category subsections and entries; optionally add commit-hash links.
6. Write **CHANGELOG.md** in the repo root.
