---
name: terraform-version-commit
description: Workflow for committing Terraform changes with strict semver versioning. Use when the user wants to commit Terraform changes, increment version from last tag following semver, intelligently bump major/minor/patch based on accumulated changes, or use an LLM (e.g. Cursor on the local machine) to generate commit messages and changelog entries.
globs: "**/*.tf", "**/terraform/**", "infra/**"
---

# Terraform Version Commit

Commit Terraform changes, read the last tag, increment to the next version following **strict semver**, and bump intelligently based on the type of accumulated changes.

## Semver Rules (Terraform/IaC)

**Rule: Minor versions are backward compatible (BC) only. Any breaking or behavioral change must bump major.**

| Bump      | When                               | Examples                                                                                                                                                                                                       |
|-----------|------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **MAJOR** | Breaking or non‑BC changes         | Removed/renamed resource, data, module, variable, output; changed variable type or made required; changed output value; provider major upgrade; any change that forces callers to change their config or state |
| **MINOR** | Additive, 100% backward compatible | New resource, data, module; new **optional** variable (with default); new output; new attribute on existing resource; deprecation only                                                                         |
| **PATCH** | Fixes, no contract change          | Typo, comment, description, provider patch/minor within same major, bug fix that does not change interface                                                                                                     |

- **When in doubt:** Prefer **major**. Do not use minor if anything could break or require caller changes.
- **REQUIRED:** Tag format `vX.Y.Z` or `X.Y.Z` (regex: `v?(\d+)\.(\d+)\.(\d+)(?:-[a-zA-Z0-9.-]+)?`).

## Commit message and split logic

- **Deep analysis:** The script parses the uncommitted diff to detect: variable added/removed/removed_default, resource added/removed, module version/source, output added/removed. From that it builds a short summary (e.g. "require timeout_seconds, invoker; remove defaults").
- **When to split:** Commits are split automatically when (a) changes touch **2+ files** with different scopes (e.g. `variables.tf` + `main.tf`), or (b) there are **6+** change items in one file. Otherwise a single commit is made.
- **Grouping:** When splitting, groups are ordered by scope: variables → main → outputs → versions → other. Each group gets one commit with a generated message (type and scope from the change kinds).

## Change Analysis Heuristics (semver bump)

1. **Conventional commits** (if used): `BREAKING:` or `!` → major; `feat:` → minor only if clearly additive; `fix:` → patch.
2. **Git diff** of `.tf` files (staged + unstaged):
    - Removed: `-resource "`, `-data "`, `-module "`, `-variable "`, `-output "` → **major**
    - Variable/output **changed** (both `-variable "` and `+variable "` or both `-output "` and `+output "` in diff) → **major**
    - Provider major bump (e.g. `version = "~> 6.` in required_providers) → **major**
    - Purely additive: new `+resource "`, `+data "`, `+module "`, `+variable "`, `+output "` (no corresponding removals) → **minor**
    - Only description, comment, or provider patch/minor (same major) → **patch**
3. **Default:** When uncertain, use **patch** (or **major** if breakage is possible).
4. **Note:** The script may treat a **module** source `version` change (e.g. `~> 0.5` → `~> 1.0`) as a provider major bump; use `--bump patch` or `--bump minor` when the only change is updating a dependency version.

## Workflow

1. **Commit first (smart commits):** The script analyzes **what** changed in the uncommitted `.tf` diff (variables, resources, modules, outputs) and either:
    - **Single commit:** One descriptive message (e.g. `refactor(variables): require timeout_seconds, invoker; remove defaults`).
    - **Split commits (automatic):** When changes span multiple files or many items, it commits in logical groups (e.g. one commit for `variables.tf`, one for `main.tf`) so history is easier to follow. No flag needed — the script decides based on number of files, scopes, and change count.
2. **Tag only when repo is clean:** If any uncommitted changes remain, script exits with "Repo has uncommitted changes. Commit or stash before tagging."
3. **Bump from committed history:** Learn what changed between the latest tag and HEAD: `git diff <last_tag>..HEAD -- *.tf` plus conventional commits since tag. That determines major/minor/patch.
4. Get last tag; parse version; if none, start at `v1.0.0`.
5. After confirmation (or `--yes`): **CHANGELOG.md** is created or updated (see below), committed, then the tag is created.
6. **Push:** Tras crear el tag, el script ejecuta `git push` y `git push --tags` (use `--no-push` para omitir).

## CHANGELOG.md

The script **generates and maintains** a `CHANGELOG.md` file in the repo root (Keep a Changelog style).

- **If there is no CHANGELOG.md:** One is created with a `# Changelog` header and a new release section `## [X.Y.Z] - YYYY-MM-DD` containing:
    - **Changed:** commit one-liners since the previous tag (or all commits if no tag), capped at 50.
    - If no commits: list of changed `.tf` files (capped at 30).
    - **Summary:** the semver bump reason (e.g. "nuevo(s) recurso(s) (additive)").
- **If CHANGELOG.md exists:** A new release section is **inserted** after the first `## [` (e.g. after `## [Unreleased]` or the previous top release), so the newest release stays at the top. Content is the same: commits since last tag, optional file list, and summary.
- The CHANGELOG update is committed with message `chore: update CHANGELOG for vX.Y.Z` before the tag is created, so the tag points at a commit that includes the changelog.
- Use **`--no-changelog`** to skip creating or updating CHANGELOG.md.

## Script

**Foreground execution mandatory:** Always run `version-commit.py` in the **foreground**. Do not use `nohup`, `&`, or background execution. If the run times out or blocks on credentials, output the exact command for the user to run in their terminal.

**Integration:** With `--parent-dir`, version-commit runs **terraform-provider-upgrade** (upgrade-providers.py) so that **provider versions** and **internal module versions** are upgraded to latest before deciding what to release. Install the **terraform-provider-upgrade** skill alongside this one (
e.g. both under `.cursor/skills/`), or set `TERRAFORM_PROVIDER_UPGRADE_SCRIPT` to the full path of `upgrade-providers.py`.

Use `scripts/version-commit.py`:

```bash
python3 /path/to/skills/terraform-version-commit/scripts/version-commit.py [options] [project_root]
# With org-iac batch: use --parent-dir /path/to/org-iac (project_root ignored)
```

- **--bump:** Override auto-detection.
- **--dry-run:** Show tag anterior, nuevo tag y razón; no commit ni tag.
- **--yes / -y:** No preguntar; aplicar tag directamente (útil en CI).
- **--no-push:** No hacer `git push` ni `git push --tags` después de crear el tag (por defecto sí hace push para que los módulos que dependen de este puedan usar la nueva versión).
- **--no-changelog:** No crear ni actualizar CHANGELOG.md al crear el tag.
- **--message "text":** Custom commit message for the first (or only) commit. When split commits are used, only the first group gets this message; later groups use auto-generated messages. When omitted, messages are generated from the diff (e.g. `refactor(variables): require X, Y; remove defaults`).
- **--write-llm-context FILE:** Write an LLM context file and exit (no commit/tag). The file describes repo, version, bump, and each commit group (files, change items, diff). Use **Cursor (or another LLM on the same machine)** to generate a JSON file with `commit_messages` (one per group) and
  optional `changelog_entries` (human-focused bullets for CHANGELOG ### Changed). Then run the script again with **--messages-file** to apply those messages. See **references/llm-commit-messages.md**.
- **--messages-file FILE:** Path to a JSON file with `commit_messages` (list of strings, one per commit group) and optional `changelog_entries` (list of strings for CHANGELOG ### Changed). Use after generating the file from the LLM context (e.g. with Cursor). Overrides auto-generated commit messages
  and CHANGELOG bullets.
- **--parent-dir PATH:** Discover all `tf-module-*` git repos under PATH (e.g. `org-iac`). Before releasing, runs **terraform-provider-upgrade** (upgrade-providers.py) on all tf-modules and on **dependent projects** so **both provider versions and internal module versions** are updated to latest (
  registry + sibling tags). Then **release only repos that have changes** (uncommitted `.tf`, CHANGELOG, or any file; or commits since last tag). After releasing, runs **terraform init -upgrade** in all tf-modules and dependent projects so `.terraform/modules` is refreshed and code editors see the
  latest code. **Requires** the **terraform-provider-upgrade** skill installed as a sibling (or `TERRAFORM_PROVIDER_UPGRADE_SCRIPT` set to the path of `upgrade-providers.py`). Ignores `project_root`.
- **--dependent-projects PATH1,PATH2:** With `--parent-dir`: paths to dependent Terraform projects (e.g. org-gitops, pacha/ops). Upgrade and `terraform init -upgrade` run on each so they use latest module versions. If omitted, `parent/org-gitops` is used when present.
- **--release-all-in-upgrade-set:** With `--parent-dir`: release **every** repo in the upgrade set (roots first), not only those with changes. Use when you need **cascade dependency update**: roots (e.g. gcp-kms) are committed/tagged/pushed first so dependents’ upgrade step can see the new tags via
  `git fetch origin --tags` in sibling repos. Without this flag, roots with no local changes are skipped and dependents keep using the previous tag.
- **--recursive:** With `--parent-dir` only. **Loop until nothing to release.** Each iteration: (1) runs **terraform-provider-upgrade** on every tf-module repo (in dependency order) and on **dependent projects**; (2) runs **terraform init -upgrade** in all repos and dependents (refresh module cache
  for editors); (3) determines which repos need release; (4) releases them in dependency order (commit, CHANGELOG, tag, push); (5) runs **terraform init -upgrade** again in all tf-modules and dependents so editors see the newly pushed tags. Repeats until nothing to release. Requires the *
  *terraform-provider-upgrade** skill. Use `--yes` with `--recursive` for non-interactive runs.

### All tf-modules in org-iac (batch)

When the user asks to run **terraform-version-commit on all tf-modules\* dirs/repos inside org-iac**:

1. **Upgrade (with --parent-dir):** Run **terraform-provider-upgrade** (upgrade-providers.py) on all tf-modules (in dependency order) and on **dependent projects**. This updates **both provider versions** (from Terraform Registry) and **internal wildbit module versions** (from sibling repos’ latest
   tags) to latest. Then run **terraform init -upgrade** in each so the module cache is refreshed for editors.
2. **List** all repos: the script looks under `PATH` (or `PATH/tf-modules` if that dir exists) for directories whose name starts with `tf-module-` and that contain a `.git` directory.
3. **Decide which need a release:** A repo "needs release" if it has uncommitted changes in **any** file (`.tf`, CHANGELOG.md, versions.tf after upgrade, etc.) **or** at least one commit since the last tag (or has commits but no tag yet).
4. **Dependency tree and release order (reverse traverse, roots first):** The script builds a dependency graph and computes an **upgrade set** = repos that need release **plus** all their transitive dependencies. It then processes in **topological order (roots first)** so that when a root (e.g.
   gcp-kms) is released and pushed, the next repo’s upgrade step sees the new tag via `git fetch origin --tags` in sibling repos (cascade). If a cycle is detected among tf-modules, the script exits with an error.
5. **Run version-commit:** For each repo in upgrade order: upgrade (providers + module refs) → terraform init -upgrade → if needs release (or `--release-all-in-upgrade-set`), run version-commit (commit, CHANGELOG, tag, push). Repos without changes are skipped unless `--release-all-in-upgrade-set` is
   used.
6. **Refresh module cache:** After releasing (push), the script runs **terraform init -upgrade** in all tf-modules and dependent projects so `.terraform/modules` has the newly pushed tags and code editors work properly.

Example (dry-run to see which would be released):

```bash
python3 /path/to/skills/terraform-version-commit/scripts/version-commit.py --parent-dir /path/to/org-iac --dry-run
```

Example (release all that have changes, no prompt):

```bash
python3 /path/to/skills/terraform-version-commit/scripts/version-commit.py --parent-dir /path/to/org-iac --yes
```

Example (recursive: upgrade providers and internal module refs, then release; repeat until nothing to release):

```bash
python3 /path/to/skills/terraform-version-commit/scripts/version-commit.py --parent-dir /path/to/org-iac --recursive --yes
```

Output shows: "Discovered N tf-module repo(s), M with changes (need release)", lists each repo with `[RELEASE]` next to those that will be processed, then **Release order (dependencies first)** with numbered steps and each repo's dependencies, then runs version-commit in that order. With
`--recursive`, each iteration prints "Recursive iteration K", runs upgrade on all repos, then releases in order until no repo needs release.

El script imprime para **cada repo** (single-repo mode):

- **Repo**, **tag anterior**, **cálculo semver** (versión actual + bump → nuevo tag), **nuevo tag** y **razón** del bump.
- Luego pregunta: **¿Usar la versión X.Y.Z y crear el tag? (s/n) [n]**.
  Solo si el usuario responde afirmativamente (o se usa `--yes`) se crea el tag. El semver se calcula a partir de `last_tag..HEAD` (diff de `.tf` + conventional commits); se toma el bump de mayor impacto (major > minor > patch).

## Manual Fallback

```bash
# Last tag
LAST=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
# Parse, increment patch by default
# git add -A && git commit -m "chore: bump to vX.Y.Z" && git tag vX.Y.Z
```

## LLM-generated commit messages and changelog (Cursor)

To use **Cursor (or another LLM on the same machine)** to generate commit messages and optional changelog entries:

1. **Write context:** From the repo root, run:
   ```bash
   python3 /path/to/terraform-version-commit/scripts/version-commit.py --write-llm-context .version-commit-llm-context.md .
   ```
   This writes a markdown file with repo/version info and, for each commit group, the changed files, change items, and diff.

2. **Generate messages with Cursor:** Open the context file and ask the agent to produce a JSON file (e.g. `.version-commit-llm-messages.json`) with:
    - **commit_messages:** One conventional-commit-style string per group (e.g. `feat(variables): add timeout_seconds (no default)`).
    - **changelog_entries:** (Optional) Human-focused bullets for CHANGELOG ### Changed. If omitted, the script uses the commit subjects.

3. **Apply and release:** Run the script with the messages file:
   ```bash
   python3 /path/to/terraform-version-commit/scripts/version-commit.py --messages-file .version-commit-llm-messages.json --yes .
   ```

**Agent instructions:** See **references/llm-commit-messages.md** for how to read the context and write the JSON.

## References

- **Semver bump logic:** See `references/semver-rules.md` for detailed MAJOR/MINOR/PATCH rules.
- **LLM commit/changelog workflow:** See `references/llm-commit-messages.md` when using Cursor to generate commit messages and changelog entries.
- **HashiCorp versioning:** https://developer.hashicorp.com/terraform/plugin/best-practices/versioning
