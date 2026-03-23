---
name: terraform-maintainer
description: Orchestrates Terraform module and consumer maintenance so tf-modules and all projects using them stay professional and in sync. Use when maintaining or releasing tf-module-* repos, upgrading providers or internal module refs, versioning with semver, normalizing changelogs, or applying DevOps/module standards across the Terraform estate. Integrates terraform-devops-modules, terraform-provider-upgrade, terraform-version-commit, and changelog-best-practices.
globs: "**/*.tf", "**/terraform/**", "infra/**"
---

# Terraform Maintainer

You are the **maintainer** of the Terraform module set and of every project that consumes those modules. Your job is to keep modules **professional**, **versioned**, **upgraded**, and **documented** by **orchestrating** existing tools—never reimplementing them.

**Orchestration only — do not reimplement.** This skill does NOT implement logic in Python or any other language. It invokes the scripts from **terraform-version-commit** (version-commit.py), **terraform-provider-upgrade** (upgrade-providers.py, upgrade-all-with-deps.py), and *
*changelog-best-practices** (normalize-changelog.py), and applies the **terraform-devops-modules** bar (structure, security, naming) when maintaining or auditing modules. Use the bash script `scripts/maintain.sh` or run the documented command sequence (see §3 and workflow B). Use simple bash or
shell commands to direct and sequence those tools. **The golden line (§ below) defines the mandatory sequence: all four skills’ procedures must run for full maintain.**

**Apply this skill when:** The user asks to maintain tf-modules, release modules, upgrade providers, sync dependent projects, normalize changelogs, or bring the Terraform estate up to a “professional set” of standards. **Sin parámetros = hacer todo:** ejecutar todos los
upgrades, inits, release completo y refresh en todos los repos y dependents; solo limitar (dry-run, projects only, no release) cuando el usuario lo pida explícitamente.

---

## Scope — todo el código Terraform conocido

**El maintainer es responsable de mantener todo el código Terraform conocido**, no solo los tf-modules. Eso incluye:

- **tf-modules** (todos los repos `tf-module-*` bajo el parent, e.g. tf-modules-parent/tf-modules): upgrade de providers y refs internos, release (commit, CHANGELOG, tag, push), changelog, refresh.
- **Proyectos dependientes** (e.g. my-infra-gitops, my-project/ops, y cualquier otro que consuma módulos privados): upgrade de **providers** y de **versiones de módulos internos** (`app.terraform.io/<your-org>/.../module`) a las últimas tags, `terraform init -upgrade` y `validate`, y **commit y push** de los cambios
  que genere el upgrade en cada dependent.

**Upgrade en dependents:** Al ejecutar upgrade en un proyecto dependiente, hay que usar `upgrade-providers.py` con el **tercer argumento (modules_root)** = path al directorio de tf-modules, para que se actualicen las **versiones de los módulos privados** a la última tag publicada — no solo los
providers. Así los planes (CI, Terraform Cloud, remoto) usan el comportamiento actual de los módulos.

**Cambios en dependents:** Si el upgrade deja en un dependent cambios sin commit (ej. `version = "~> 3.1"` → `"~> 4.0"` en main.tf), el maintainer debe **commitear y pushear** esos cambios en ese repo. **version-commit.py** (con `--parent-dir`) hace esto **automáticamente** al final del run: tras el
release y el refresh, hace commit y push en cada dependent con cambios, de modo que el **próximo run de TFC/CI** use ya las versiones de módulos con el fix (ej. module-base v4.0.5).

---

## Golden line (línea de oro) — canonical workflow

**terraform-maintainer is the single source of truth for module and repo maintenance.** Full maintain **must** run **all** procedures from the four skills, in order. This is the standard of excellence for updates and maintainability.

| Order | Skill                          | Procedure (what must run)                                                                                                                                                                                                                                                                               |
|-------|--------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1     | **terraform-devops-modules**   | Apply or verify: module structure (main.tf, variables.tf, outputs.tf, versions.tf), required_providers, composition, security (no secrets, sensitive outputs, least privilege), naming, HCL style. When maintaining: audit each module against this bar; when creating/refactoring: apply the full bar. |
| 2     | **terraform-provider-upgrade** | Bump provider versions (Registry) and internal private module refs (sibling tags). Run `upgrade-providers.py` per project or `upgrade-all-with-deps.py` on tf-modules root. Then `terraform init -upgrade` and `terraform validate`.                                                                    |
| 3     | **terraform-version-commit**   | Commit .tf changes, update CHANGELOG, compute semver (major/minor/patch), tag, push. Single repo: `version-commit.py --yes .` Batch: `version-commit.py --parent-dir <path> --yes` (dependency order).                                                                                                  |
| 4     | **changelog-best-practices**   | Normalize or fix CHANGELOG.md (Keep a Changelog, human-focused entries). Run `normalize-changelog.py` when CHANGELOG is script-generated or noisy; commit if changed.                                                                                                                                   |

**Rule:** Do not skip any of the four. For "maintain" (default), execute 1 → 2 → 3 → 4 in dependency order; then refresh dependents (`terraform init -upgrade`). For single-module release, follow workflow A (§5); for batch, use maintain.sh or workflow B.

---

## Default: maintain (no argument) — do everything

**When the user runs terraform-maintainer with no argument**, you **must perform all maintenance tasks**: all upgrades, all inits, full release when there are changes, and changelog normalization. You are the maintainer; do everything by default. **Only skip or limit scope when the user explicitly
says so** (e.g. "dry-run only", "projects only", "no release").

**When the user runs terraform-maintainer with no argument** (e.g. “run terraform-maintainer”, “maintain tf-modules”), the intended behavior is **maintain** = full maintenance of the **tf-modules** tree following the **golden line** above:

1. **terraform-devops-modules:** Apply or verify standards (structure, versions.tf, security, naming) for each module being maintained.
2. **terraform-provider-upgrade:** Run **all** upgrades: provider versions and internal private module refs in every tf-module (and dependents when using `--parent-dir`). Run `terraform init -upgrade` and `terraform validate` in each repo.
3. **terraform-version-commit:** Run the **full** release: commit .tf, update CHANGELOG, semver tag, push — in **dependency order (small to big)**. Release every repo that has changes.
4. **changelog-best-practices:** Normalize CHANGELOG.md when script-generated or noisy; commit if needed.
5. **Refresh everything:** After tags are pushed, run `terraform init -upgrade` in **all** tf-modules and in **all** dependent projects so they resolve the new versions.
6. **Dependents: commit y push:** Si algún proyecto dependiente quedó con cambios sin commit (p. ej. versiones de módulos actualizadas por upgrade-providers), commitear y pushear en ese repo para que todo el código Terraform conocido quede mantenido.

**Rule:** No parameter = do **all** upgrades, all inits, full release, refresh, and commit+push in dependents when the upgrade changed their files. Do not omit steps unless the user explicitly asks to limit (e.g. "solo dry-run", "projects only", "no release").

**Do not** assume “create a new module” or “projects only” unless the user explicitly says so (e.g. “create a new module”, “run only on projects” / “projects only”).

---

## 1. The four skills you orchestrate (golden line)

These four skills form the **golden line**: every full maintain run must execute their procedures in order (see table in § Golden line). Do not skip any.

| Skill                          | Use it for                                                                                                                                                                                                                                              |
|--------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **terraform-devops-modules**   | Module structure (main.tf, variables.tf, outputs.tf, versions.tf), composition, provider constraints, security (state, IAM, networking), HCL style. Apply when creating or refactoring a module, or auditing that a module meets the standard.          |
| **terraform-provider-upgrade** | Bump provider versions (Registry API) and internal private module refs (from sibling tags). Use `upgrade-providers.py` on a single project; use `upgrade-all-with-deps.py` only on the **tf-modules root** (dependency-order upgrade + version-commit). |
| **terraform-version-commit**   | Commit .tf changes, update CHANGELOG, compute semver bump (major/minor/patch), tag, push. Use on a single tf-module or with `--parent-dir` for the whole tf-modules-parent tree (batch release in dependency order).                                              |
| **changelog-best-practices**   | Keep a Changelog format, human-focused entries, normalize script-generated CHANGELOGs (e.g. after version-commit). Use when writing, maintaining, or fixing CHANGELOG.md.                                                                               |

**Rule:** Read and follow each skill’s SKILL.md when you are about to perform that skill’s task. Do not only “mention” a skill—invoke its workflow and scripts with correct paths and options. **Foreground execution mandatory:** run all version-commit and upgrade-providers (or upgrade-all-with-deps)
commands in the foreground; do not use `nohup`, `&`, or background execution.

---

## 2. Execution: foreground only; use bash script or exact commands

**REQUIRED — run all commands in the foreground.** Do **not** use `nohup`, `&`, or background execution. Do **not** reimplement version-commit or upgrade logic; only invoke the existing scripts.

**Visible progress:** When invoking `version-commit.py`, use **unbuffered output** so the user sees progress (e.g. `PYTHONUNBUFFERED=1` or `python3 -u`). Otherwise the script can run for a long time with no visible output.

**Batch maintain (workflow B) — use one of:**

1. **Bash orchestrator (recommended):** Run the skill’s script, which only calls the existing tools in sequence with clear step messages:
   ```bash
   .cursor/skills/terraform-maintainer/scripts/maintain.sh /path/to/tf-modules-parent
   ```
   Optional: `--dry-run-only` to only discover repos and order (no release).  
   **Timeout:** When running maintain.sh in-agent, use the **maximum allowed timeout (600000 ms / 10 minutes)** so the script can progress as far as possible. The full release step often takes 15–45+ minutes; for **complete** runs (all commit/tag/push), run the same command in a terminal. Optional:
   `TIMEOUT_SEC=2700 ./maintain.sh ...` (45 min cap) when run in terminal.

2. **Exact command sequence (no reimplementation):**
    - Step 1 (dry-run): `python3 -u .cursor/skills/terraform-version-commit/scripts/version-commit.py --parent-dir <PATH> --dry-run [--dependent-projects <PATH>/infra-gitops]`
    - Step 2 (release): `python3 -u .cursor/skills/terraform-version-commit/scripts/version-commit.py --parent-dir <PATH> --yes [--dependent-projects ...]`
      Summarize dry-run output for the user; then run the release step in the foreground. If it times out, output the same command for the user to run in their terminal.

**Single-repo release (workflow A):** Run `python3 -u .../version-commit.py --yes .` from the module dir (invoke existing script only). If it times out, output the command for the user to run locally.

---

## 3. Paths and scripts (assume Cursor skills layout)

**Orchestrator (bash only — invokes the tools below; no reimplementation):**

- **maintain.sh:**  
  `.cursor/skills/terraform-maintainer/scripts/maintain.sh`  
  Usage: `maintain.sh /path/to/tf-modules-parent [--dry-run-only]`

**Tools invoked by the orchestrator (do not reimplement; call these):**

- **terraform-version-commit:**  
  `.cursor/skills/terraform-version-commit/scripts/version-commit.py`
- **terraform-provider-upgrade:**  
  `.cursor/skills/terraform-provider-upgrade/scripts/upgrade-providers.py`  
  `.cursor/skills/terraform-provider-upgrade/scripts/upgrade-all-with-deps.py`
- **changelog-best-practices:**  
  `.cursor/skills/changelog-best-practices/scripts/normalize-changelog.py`

**Typical layout:** tf-modules live under e.g. `tf-modules-parent/tf-modules/` (each `tf-module-*` is a git repo). Dependent projects live alongside (e.g. `tf-modules-parent/infra-gitops`, `my-project/ops`). Para mantener **todo** el código Terraform conocido, pasar todos los dependents en `--dependent-projects` (rutas separadas por coma), o
definir `DEPENDENT_PROJECTS` (ej. `/path/to/infra-gitops,/path/to/my-project/ops`) antes de ejecutar `maintain.sh`.

---

## 4. Modes (when to do what)

| User intent                                                      | Mode                    | What to do                                                                                                                                                                                                        |
|------------------------------------------------------------------|-------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| No argument / “maintain” / “maintain tf-modules”                 | **Maintain tf-modules** | Full maintenance of tf-modules: upgrade → maintain (standards) → fix (changelog) → commit → publish, in **dependency order (small to big)**. Then refresh dependents (`terraform init -upgrade`). See workflow B. |
| “Create a new module”                                            | **New module**          | Apply terraform-devops-modules bar, README, then upgrade + version-commit as in A or B. See workflow D.                                                                                                           |
| “Projects only” / “only on projects” / “dependent projects only” | **Projects only**       | Upgrade and refresh **only** consuming projects (e.g. my-infra-gitops, my-project/ops). No tf-module release. See workflow C.                                                                                               |

---

## 5. Workflows

### A. Single tf-module: release with upgrade and changelog

1. **Standards (terraform-devops-modules):** Ensure the module has versions.tf, required_providers, structure (main.tf, variables.tf, outputs.tf), sensible defaults, no secrets in code, sensitive outputs marked.
2. **Upgrade (terraform-provider-upgrade):** Run `upgrade-providers.py <path/to/tf-module-xxx> [report] <path/to/tf-modules>`. Fix any validate failures (e.g. unresolvable module version → keep constraint at last published tag).
3. **Release (terraform-version-commit):** From the module dir or with project_root: `version-commit.py --yes [--no-push] .` Commit, CHANGELOG, tag, push (unless --no-push).
4. **Changelog (changelog-best-practices):** If CHANGELOG.md is script-generated and noisy, run `normalize-changelog.py CHANGELOG.md` (optionally `--repo-url=...`), then commit if needed.

### B. Full tf-modules tree + dependents — **default “maintain”**

This is the **maintain** path (no argument) and implements the **golden line**: (1) terraform-devops-modules audit/apply, (2) terraform-provider-upgrade, (3) terraform-version-commit, (4) changelog-best-practices as needed — in **dependency order (small to big)**.

1. **Upgrade + release in order:** The **version-commit.py** script (from terraform-version-commit) already runs upgrade on all tf-modules and dependents when given `--parent-dir`, then releases in **dependency order**. Invoke it; do not reimplement.
   ```bash
   # Option A: use the bash orchestrator (recommended; shows step messages)
   .cursor/skills/terraform-maintainer/scripts/maintain.sh /path/to/tf-modules-parent

   # Option B: call version-commit directly (use python3 -u for visible output)
   python3 -u .cursor/skills/terraform-version-commit/scripts/version-commit.py --parent-dir /path/to/tf-modules-parent --yes --dependent-projects /path/to/infra-gitops
   ```  
   Use `--recursive` on version-commit.py to repeat until nothing to release. If upgrade script is missing, version-commit skips the upgrade step and still discovers/releases in order.
2. **Dependents:** After tags are pushed, run `terraform init -upgrade` in each dependent project so they resolve new module versions. version-commit with `--parent-dir` already runs upgrade-providers (with modules_root) on dependents, so internal module refs are updated. If a dependent has *
   *uncommitted changes** (e.g. version bumps in .tf), **commit and push** in that repo so all known Terraform code is maintained (e.g. `git add -A && git commit -m "chore(terraform): upgrade provider and module versions" && git push`).
3. **Changelog:** Optionally normalize CHANGELOG.md in each released tf-module with `normalize-changelog.py`.

### C. Projects only (no tf-module release)

**Use when the user says “projects only” or “only on projects”.** Do not release any tf-module; only upgrade and refresh consuming projects.

1. Run **upgrade-providers.py** on each consuming project with the third argument = path to tf-modules, so provider versions and internal private module versions are updated from sibling tags.
2. Run `terraform init -upgrade` and `terraform validate`. If a module version is not yet published (e.g. tag created with --no-push), keep that module’s version constraint at the last published version until the tag is pushed.

### D. New module or big refactor (professional bar)

**Use when the user says “create a new module” or similar.** Then apply the full professional bar and release.

1. **terraform-devops-modules:** Apply full bar: structure, versions.tf with required_providers, composition, security (no secrets, sensitive outputs, least privilege), naming.
2. Add README.md (usage, inputs, outputs) per devops-modules.
3. Then run upgrade + version-commit as in A or B; apply changelog-best-practices to CHANGELOG.md.

---

## 6. Dependency order (tf-modules)

Releasing or upgrading must respect dependencies: a module that uses `app.terraform.io/<your-org>/B/module` must be upgraded or released **after** B is tagged and pushed. version-commit with `--parent-dir` computes this order. If you run upgrades manually, process in the same order (e.g. module-base before
module-service-a, module-service-b).

---

## 7. Professional set checklist

- [ ] Every tf-module has `versions.tf` with explicit `required_providers` (terraform-devops-modules).
- [ ] Provider constraints are latest compatible (~> major.minor); no stale pins unless justified.
- [ ] Internal module refs (app.terraform.io/<your-org>/.../module) match published tags; dependents run `terraform init -upgrade` after releases.
- [ ] Dependent projects have upgrade-induced changes committed and pushed so CI/remote plans use current module versions.
- [ ] Releases use strict semver (terraform-version-commit); tags are pushed so registry and dependents can consume.
- [ ] CHANGELOG.md exists and follows Keep a Changelog; entries are human-focused (changelog-best-practices).
- [ ] No secrets in .tf or state in repo; sensitive outputs marked; remote state, encryption, least privilege (terraform-devops-modules).

---

## 8. References

- **Unified workflow summary and script invocations:** See [references/workflow.md](references/workflow.md).
- **Semver and version-commit options:** Read `.cursor/skills/terraform-version-commit/SKILL.md` when releasing.
- **Provider upgrade and upgrade-all-with-deps:** Read `.cursor/skills/terraform-provider-upgrade/SKILL.md` when upgrading.
- **Changelog format and normalize script:** Read `.cursor/skills/changelog-best-practices/SKILL.md` when editing CHANGELOG.
- **Module structure and security:** Read `.cursor/skills/terraform-devops-modules/SKILL.md` when creating or auditing modules.
