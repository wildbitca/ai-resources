# LLM-generated commit messages and changelog (Cursor)

This reference tells an **LLM agent (e.g. Cursor)** how to generate **commit messages** and optional **changelog entries** for the Terraform version-commit script from the context file produced by `--write-llm-context`.

---

## Prerequisites

- The **context file** has been written by the script:
  ```bash
  python3 .../version-commit.py --write-llm-context .version-commit-llm-context.md <repo>
  ```
- The agent can read the context file (e.g. `.version-commit-llm-context.md` in the repo root).

---

## Step 1: Read the context file

- Open the context file (path was passed to `--write-llm-context`).
- It contains:
    - **Repo and version:** repo path, last tag, new version, bump type, reason.
    - **Commit groups:** For each group (1..N):
        - **Files:** list of .tf files in that group.
        - **Change items:** structured list (file, kind, name, detail) — e.g. variable_added, resource_removed, module_version.
        - **Diff:** the actual git diff for those files (unified format).

- You must produce **exactly one commit message per group**, in order (group 1 → first message, group 2 → second, etc.).

---

## Step 2: Generate conventional commit messages (one per group)

For **each commit group**:

- Use the **change items** and **diff** to understand what changed.
- Write one line in [Conventional Commits](https://www.conventionalcommits.org/) form: **`type(scope): description`**.
- **Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`. For Terraform:
    - `feat` — new variable, resource, output, module (additive).
    - `fix` — bug fix that does not change interface.
    - `refactor` — removed variable/resource/output, or made variables required (breaking).
    - `chore` — provider/module version bumps, descriptions, comments.
- **Scope:** Use the logical scope from the files (e.g. `variables`, `main`, `outputs`, `versions`). Often the context lists files like `variables.tf`, `main.tf` — use that for scope.
- **Description:** Imperative, lowercase, concise. Summarize what the diff does for a human reader (e.g. "add timeout_seconds and invoker (no defaults)", "bump provider versions").

Examples:

- Group with variable_removed_default, variable_added → `refactor(variables): require timeout_seconds and invoker; remove defaults`
- Group with only module_version in versions.tf → `chore(versions): bump provider versions`
- Group with resource_added in main.tf → `feat(main): add google_cloud_run_service resource`

---

## Step 3: Optional — human-focused changelog entries

- **changelog_entries** is optional. If you omit it, the script will use the **commit message subjects** as the CHANGELOG ### Changed bullets.
- If you want **curated, user-facing** bullets (e.g. one line per logical change, or merged groups), add a list of strings.
- **Length:** One entry per group is typical, but you can merge related groups into one line or split one group into two lines. The list length does not need to match the number of groups; it replaces the whole ### Changed section.
- Use past tense or imperative consistently (e.g. "Added X and Y (breaking: no defaults).", "Bumped Terraform provider versions.").

---

## Step 4: Write the JSON file

- Write a **JSON file** in the repo root (e.g. `.version-commit-llm-messages.json`) with this structure:

```json
{
  "commit_messages": [
    "refactor(variables): require timeout_seconds and invoker; remove defaults",
    "chore(versions): bump provider versions"
  ],
  "changelog_entries": [
    "Made timeout_seconds and invoker required (breaking: defaults removed).",
    "Bumped Terraform provider versions."
  ]
}
```

- **commit_messages:** **Required.** Array of strings; length must equal the number of commit groups in the context. Order must match (group 1 → index 0, etc.).
- **changelog_entries:** **Optional.** Array of strings. If present, the script uses these for CHANGELOG ### Changed instead of the commit subjects. If omitted, the script uses the commit messages as CHANGELOG bullets.

- Save the file so the user can run:
  ```bash
  python3 .../version-commit.py --messages-file .version-commit-llm-messages.json --yes .
  ```

---

## Summary for the agent (Cursor)

1. Read the **context file** (e.g. `.version-commit-llm-context.md`).
2. For **each commit group**, from the change items and diff, write **one conventional commit message** (`type(scope): description`).
3. Optionally write **changelog_entries**: human-focused bullets for CHANGELOG ### Changed.
4. Write a **JSON file** with `commit_messages` (required, one per group) and optionally `changelog_entries`.
5. Tell the user to run the script with `--messages-file <path>` to apply and release.
