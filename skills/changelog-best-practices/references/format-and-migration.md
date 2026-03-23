# Changelog Format and Migration Reference

**Contents:** Standard header · Categories · Version heading format · Minimal template · Link references · Migration (wrong headings, dates, commit dump, missing Unreleased, mixed categories, order, filename, multiple files) · Entry style

---

## Standard header (Keep a Changelog)

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
```

Omit the SemVer line if the project does not use semver.

---

## Categories (use only these)

| Heading        | Use for                           |
|----------------|-----------------------------------|
| **Added**      | New features                      |
| **Changed**    | Changes in existing functionality |
| **Deprecated** | Soon-to-be removed features       |
| **Removed**    | Removed features                  |
| **Fixed**      | Bug fixes                         |
| **Security**   | Vulnerabilities                   |

- Use `### Added`, `### Changed`, etc. under each version.
- Include only sections that have at least one entry; omit empty ones.

---

## Version heading format

- **Unreleased:** `## [Unreleased]` (no date).
- **Released:** `## [X.Y.Z] - YYYY-MM-DD` (e.g. `## [1.2.0] - 2025-02-11`).
- **Yanked:** `## [X.Y.Z] - YYYY-MM-DD [YANKED]`.

Date must be ISO 8601: `YYYY-MM-DD`. No other date formats in headings.

---

## Minimal template (from scratch)

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- (nothing yet)

## [1.0.0] - YYYY-MM-DD

### Added
- Initial release.
```

Replace `(nothing yet)` with real entries or remove the subsection until there is content. Prefer omitting empty sections.

---

## Link references (optional)

At the **bottom** of the file, add reference-style links so version headings become clickable (e.g. in GitHub):

```markdown
[Unreleased]: https://github.com/owner/repo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/owner/repo/compare/v0.9.0...v1.0.0
[0.9.0]: https://github.com/owner/repo/releases/tag/v0.9.0
```

- `[Unreleased]`: compare latest tag to `HEAD`.
- Each version: compare previous tag to this tag (or `releases/tag` for the first tag).
- Adjust URLs for GitLab, Bitbucket, etc. (e.g. `.../compare/prev...curr`).

---

## Migration: common problems and fixes

### 1. Wrong or missing version headings

| Before                          | After                                                                                                  |
|---------------------------------|--------------------------------------------------------------------------------------------------------|
| `## Version 1.0`                | `## [1.0.0] - YYYY-MM-DD`                                                                              |
| `## v1.0 (Jan 1, 2024)`         | `## [1.0.0] - 2024-01-01`                                                                              |
| `# 1.0.0`                       | `## [1.0.0] - YYYY-MM-DD`                                                                              |
| No version sections, only dates | Introduce `## [X.Y.Z] - YYYY-MM-DD` per release; infer version from tags or use `1.0.0`, `1.1.0`, etc. |

### 2. Wrong date format

- **DD/MM/YYYY** or **MM/DD/YYYY** → convert to **YYYY-MM-DD** (e.g. 11/02/2025 → 2025-02-11 only if you know it’s day/month).
- **“Jan 15, 2024”** → **2024-01-15**.
- **No date** → add ` - YYYY-MM-DD` using release date or today for “current” release.

### 3. Commit log dump (git log as changelog)

- **Do not** keep raw commit messages as-is.
- **Do:** Group by type (Added/Changed/Fixed/Removed/Deprecated/Security), one line per *notable* change.
- Merge related commits: e.g. “Add login” + “Fix login validation” → “Added login with validation.”
- Drop: merge commits, “Bump version”, “Update deps” (unless user-facing), purely internal refactors (unless behavior change).
- Prefer user-facing wording: “Fixed crash when opening large files” instead of “Fix null pointer in FileLoader.”

### 4. Missing Unreleased section

- Insert at the **top** (right after header): `## [Unreleased]` and under it the usual categories as needed.
- Moving forward, add new changes under `[Unreleased]`; on release, move that block to a new `## [X.Y.Z] - YYYY-MM-DD` and clear or refill `[Unreleased]`.

### 5. Mixed or non-standard categories

- Map to the six categories only: Added, Changed, Deprecated, Removed, Fixed, Security.
- Examples: “New” / “Features” → **Added**; “Updates” / “Improvements” → **Changed**; “Bugs” → **Fixed**; “Breaking” → **Changed** (and state “Breaking: …”) or **Removed** if something was removed.

### 6. Wrong order (oldest first)

- Reverse so **newest release first**. Order: `[Unreleased]`, then latest version (e.g. 2.0.0), then 1.9.0, 1.8.0, …

### 7. Different filename

- Rename to **CHANGELOG.md** in project root. If the project docs reference the old name (e.g. HISTORY.md), update those references or add a one-line note in the old file: “Changelog has moved to CHANGELOG.md.”

### 8. Multiple changelog files

- Prefer a single **CHANGELOG.md** at the root. If merging: keep reverse chronological order, deduplicate versions, normalize headings and dates as above. Document in the header if the changelog was merged from multiple files (optional).

---

## Entry style (readability)

- **One line per change** when possible; use a second line only for a short clarification.
- **Start with a verb** (Added/Changed/Fixed/…) or with the subject: “Login form now validates email.”
- **Be consistent:** all past tense (“Added X”) or all imperative (“Add X”).
- **Commit hashes as links (good practice):** Keep the short hash and link it to the commit URL (e.g. `([2be7436](https://github.com/org/repo/commit/2be7436))`) so readers can open the diff. Use the repo base URL and append `/commit/<hash>`.
- **Link to issues/PRs** optionally at the end: “- Fixed crash on startup (#123).”
- **Breaking changes:** Under **Changed** or **Removed**, state clearly: “Breaking: removed deprecated `oldApi()`; use `newApi()` instead.”
