# Terraform Maintainer — Workflow Reference

**Orchestration only:** This skill invokes existing scripts (version-commit, upgrade-providers, normalize-changelog) and applies terraform-devops-modules standards. Do not reimplement; use the bash script or the exact commands below. Full maintain follows the **golden line** (all four skills’ procedures in order).

**Foreground execution mandatory:** Run all commands in the foreground. Use `python3 -u` or `PYTHONUNBUFFERED=1` when calling version-commit.py so progress is visible (otherwise output can be buffered and the run can appear to do nothing).

## Golden line (línea de oro)

Full maintain **must** run: (1) **terraform-devops-modules** — audit/apply structure, versions.tf, security, naming; (2) **terraform-provider-upgrade** — upgrade-providers.py / upgrade-all-with-deps.py, then init -upgrade and validate; (3) **terraform-version-commit** — commit, CHANGELOG, semver tag, push in dependency order; (4) **changelog-best-practices** — normalize-changelog.py when needed. Then refresh dependents and **commit+push in dependents** (version-commit does this automatically so the next TFC/CI run picks up fixed module versions). Do not skip any.

## Default: maintain (no argument) — do everything

Running **terraform-maintainer** with **no argument** means perform **all** maintenance tasks: all upgrades, all inits, full release (when there are changes), and changelog normalization. Then refresh **all** tf-modules and **all** dependents. **Then**, if any dependent project has uncommitted changes (e.g. version bumps from upgrade-providers), **commit and push** in that repo so all known Terraform code is maintained. Only limit scope when the user explicitly says so (e.g. "dry-run only", "projects only", "no release"). In short: (1) devops-modules, (2) upgrade (providers + internal refs) + init -upgrade + validate everywhere, (3) commit/publish in dependency order, (4) changelog, (5) init -upgrade in all repos and dependents, (6) commit+push in dependents when upgrade changed their files. Use the “Batch: all tf-modules + dependents” flow below.

- **Create a new module:** Only when the user explicitly says so; then apply devops-modules bar, README, and release.
- **Projects only:** Only when the user explicitly says so; then upgrade and refresh consuming projects only (no tf-module release).

---

## Script paths (Cursor skills)

| Script | Path |
|--------|------|
| **maintain (orchestrator)** | `.cursor/skills/terraform-maintainer/scripts/maintain.sh` |
| version-commit | `.cursor/skills/terraform-version-commit/scripts/version-commit.py` |
| upgrade-providers | `.cursor/skills/terraform-provider-upgrade/scripts/upgrade-providers.py` |
| upgrade-all-with-deps | `.cursor/skills/terraform-provider-upgrade/scripts/upgrade-all-with-deps.py` |
| normalize-changelog | `.cursor/skills/changelog-best-practices/scripts/normalize-changelog.py` |

---

## Single tf-module release

```bash
# 1. Upgrade providers + internal module refs (from sibling tf-modules tags)
python3 upgrade-providers.py /path/to/org-iac/tf-modules/tf-module-XXX /tmp/skipped.txt /path/to/org-iac/tf-modules

# 2. Release (commit .tf, CHANGELOG, tag, push)
cd /path/to/org-iac/tf-modules/tf-module-XXX
python3 version-commit.py --yes .

# 3. Optional: normalize CHANGELOG
python3 normalize-changelog.py CHANGELOG.md
# then commit if changed
```

---

## Batch: all tf-modules + dependents (org-iac)

**Recommended:** Run the bash orchestrator so output is unbuffered and progress is visible. It runs dry-run then release; use `--dry-run-only` to only dry-run.

```bash
.cursor/skills/terraform-maintainer/scripts/maintain.sh /path/to/org-iac
# Dry-run only (no commit/tag/push):
.cursor/skills/terraform-maintainer/scripts/maintain.sh /path/to/org-iac --dry-run-only
```

Alternative (run in foreground; use `python3 -u` for visible output):

```bash
python3 -u .cursor/skills/terraform-version-commit/scripts/version-commit.py --parent-dir /path/to/org-iac --dry-run --dependent-projects /path/to/org-iac/org-gitops
python3 -u .cursor/skills/terraform-version-commit/scripts/version-commit.py --parent-dir /path/to/org-iac --yes --dependent-projects /path/to/org-iac/org-gitops
```

**Note:** version-commit.py with `--parent-dir` runs upgrade-providers (with modules_root) on all tf-modules and dependents; releases in dependency order; then runs `terraform init -upgrade` everywhere. **It then automatically commits and pushes** any uncommitted changes in dependents (e.g. module version bumps), so the **next TFC/CI run uses the fixed module versions** (e.g. gcp-kms v4.0.5). Use `--dependent-projects` with comma-separated paths (e.g. `org-gitops,pacha/ops`) or set `DEPENDENT_PROJECTS` when running maintain.sh.

---

## Only upgrade a dependent project (e.g. pacha/ops)

```bash
python3 upgrade-providers.py /path/to/pacha/ops /tmp/skipped.txt /path/to/org-iac/tf-modules
# Then: cd /path/to/pacha/ops && terraform init -upgrade && terraform validate
# If there are changes (e.g. version bumps), commit and push so CI/remote plans use current versions:
# git add -A && git commit -m "chore(terraform): upgrade provider and module versions" && git push
```

If a module version (e.g. gcp-kms ~> 4.0) is not yet on the registry, keep version at last published (e.g. ~> 3.0) until the tag is pushed.

---

## Upgrade all tf-modules in dependency order (no version-commit)

Use only when you want to bump providers and internal refs in every tf-module **and** then release each (commit, tag, push). Otherwise use version-commit with --parent-dir.

```bash
python3 upgrade-all-with-deps.py /path/to/org-iac/tf-modules [report_path] [--dry-run]
```

---

## When to use which skill

| Task | Skill |
|------|--------|
| **Full maintain (golden line)** — run all procedures in order | **terraform-maintainer** (orchestrates the four below) |
| Add or refactor a module (structure, variables, outputs, security) | terraform-devops-modules |
| Bump provider versions or internal module refs in one project | terraform-provider-upgrade (upgrade-providers.py) |
| Release one or all tf-modules (commit, semver tag, push) | terraform-version-commit |
| Fix or normalize CHANGELOG.md | changelog-best-practices |

---

## When to use which mode (terraform-maintainer)

| User says | Mode | Flow |
|-----------|------|------|
| (nothing) / “maintain” / “maintain tf-modules” | Maintain tf-modules | Batch: all tf-modules + dependents (dependency order, then refresh dependents) |
| “Create a new module” | New module | Single tf-module release + devops-modules bar + README |
| “Projects only” / “only on projects” | Projects only | Only upgrade dependent project(s); no tf-module release |
