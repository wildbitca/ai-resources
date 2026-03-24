---
name: terraform-provider-upgrade
description: Workflow for upgrading Terraform provider versions across modules. Use when the user wants to update providers, upgrade provider versions, run terraform init and validate to confirm nothing broke, or search for latest provider versions in the Terraform Registry.
globs: "**/*.tf", "**/terraform/**", "infra/**"
triggers: "**/*.tf, terraform provider, upgrade provider, provider version, terraform registry, terraform init, terraform validate, required_providers, provider update"
---

# Terraform Provider Upgrade

Workflow to **update provider versions** in each module: search for latest versions, update `required_providers`, run `terraform init` and `terraform validate` to confirm nothing broke.

## Workflow

1. **Find modules** with `required_providers` (root + `modules/*/versions.tf` or any `*.tf`).
2. **Search latest versions** per provider via Terraform Registry API.
3. **Update** `versions.tf` (or `terraform` block) with new constraints.
4. **Run** `terraform init` and `terraform validate` to verify.
5. **Confirm** no errors; optionally run `terraform plan` for full check.

## Provider Versions API

- **Endpoint:** `GET https://registry.terraform.io/v1/providers/:namespace/:type/versions`
- **Example:** `curl -s https://registry.terraform.io/v1/providers/hashicorp/aws/versions`
- **Response:** JSON with `versions` array; each object has `version` (e.g. `"5.67.0"`).
- **Source mapping:** `hashicorp/aws` → namespace=`hashicorp`, type=`aws`. Parse from `source` in `required_providers` (e.g. `hashicorp/aws`).

## How to Parse `required_providers`

Look for blocks like:

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

Extract: provider key, `source` (namespace/type), current `version`. The `source` format is `namespace/type` (e.g. `hashicorp/aws`, `hashicorp/google`).

## Version Update Strategy

- From API: get all `versions`, sort by semver, take latest.
- **Constraint format:** Prefer `~> X.Y` (e.g. `~> 5.67`) for minor/patch flexibility, or `>= X.Y.0, < (X+1).0` for major pin.
- **REQUIRED:** Preserve major compatibility unless the user explicitly requests a major upgrade.

## Execution Order

1. Update **root** module first.
2. Update **child modules** (each may have its own `required_providers`).
3. Run from **project root** (where root module lives):
    - `terraform init -upgrade`
    - `terraform validate`
4. If validation fails, revert changes or fix incompatibilities before committing.

## Execution: foreground mandatory

**REQUIRED:** Run `upgrade-providers.py` and `upgrade-all-with-deps.py` only in the **foreground**. Do not use `nohup`, `&`, or background execution. This avoids orphan processes and reduces resource use.

## Scope: normal repos vs tf-modules

- **Normal Terraform projects** (e.g. my-infra-gitops, my-project/ops, any repo that is not a tf-module): use **upgrade-providers.py** only. It updates provider and internal module versions, runs `terraform init -upgrade` and `terraform validate`. It **never** commits, tags, or pushes — you decide when to
  commit.
- **tf-modules tree** (directory that contains all `tf-module-*` git repos): use **upgrade-all-with-deps.py** when you want to upgrade every module in dependency order and then commit/tag/push each so the next one can use the new tag. That script is the only one that runs version-commit (commit,
  tag, push).

## Script

Use `scripts/upgrade-providers.py` (or `scripts/upgrade-providers.sh`) from the skill directory:

```bash
python3 /path/to/skills/terraform-provider-upgrade/scripts/upgrade-providers.py /path/to/terraform/project [report_path] [modules_root]
# Or from project root:
python3 upgrade-providers.py .
```

The script (use for any Terraform project; **no commit, no tag, no push**):

- Finds all `.tf` files with `required_providers`
- For each provider, fetches latest version from `https://registry.terraform.io/v1/providers/:namespace/:type/versions`
- Updates version constraints to `~> major.minor`
- **Internal module versions (optional):** If `modules_root` is passed (path to the directory containing all `tf-module-*` repos), the script runs `git fetch origin --tags --prune` in each sibling repo, then reads the latest git tag and builds a map `module_name -> "~> X.Y"`. It then **replaces**
  every `module` block’s `version` for `app.terraform.io/<your-org>/<name>/module` with that constraint so the **latest released minor is strictly specified** (e.g. latest tag `v3.1.1` → `version = "~> 3.1"`; even if the file had `~> 3.0`, it is updated to `~> 3.1` so Terraform and editors use the
  latest). This keeps dependencies on your own modules in sync after publishing new versions.
- Runs `terraform init -upgrade` and `terraform validate`
- **Never use local module references** — always use the registry (e.g. `app.terraform.io/org/name/module`).
- If **validate fails** and the error references **`.terraform/modules`** (failure in a registry submodule that has not been updated yet), the script prints **`SKIPPED - pending upgrade of submodules`**, optionally appends to a **report file** (see below), and exits 0 so the run can continue. The
  user must fix or upgrade the dependent submodules and re-run.
- Otherwise exits with non-zero if validate fails.

**Arguments:**

- **Arg 1 (required):** Path to the Terraform project (module) root.
- **Arg 2 (optional):** Report file path. Skipped modules are appended as: `module_name\tpending upgrade of submodules (validate failed in .terraform/modules)`.
- **Arg 3 (optional):** Path to the parent directory containing all `tf-module-*` repos. When set, the script updates `version` for `app.terraform.io/<your-org>/.../module` sources from sibling repos’ latest tags.

**Example (provider upgrade + internal module refs):**

```bash
python3 upgrade-providers.py /path/to/tf-module-gcp-lb /path/to/skipped.txt /path/to/tf-modules
```

**Requirements:** Network access to registry.terraform.io; Python 3.7+.

**SSL certificate errors:** If you see `SSL: CERTIFICATE_VERIFY_FAILED` when fetching from the Registry, Python cannot verify HTTPS. The script uses the `certifi` CA bundle when available. Fix options: (1) `pip install certifi` so the script uses it; (2) on macOS with Python from python.org, run *
*Applications → Python 3.x → Install Certificates.command**; (3) set `SSL_CERT_FILE` to your CA bundle path.

## Dependency-order upgrade (tf-modules only)

**Use only** when the target is the directory that contains all `tf-module-*` repos. If you pass a normal project (e.g. my-infra-gitops, my-project/ops), the script exits with an error.

When upgrading **all** `tf-module-*` repos, internal dependencies must be updated **first**: if module A uses module B (`app.terraform.io/<your-org>/B/module`), then B must be upgraded, committed, tagged, and **pushed** before A so that A can resolve the new tag.

Use `scripts/upgrade-all-with-deps.py` to:

1. **Analyze dependencies:** Scan each repo’s `.tf` for `source = "app.terraform.io/<your-org>/<name>/module"` and build a dependency graph.
2. **Topological order:** Process modules in an order where every dependency is handled before its dependents (e.g. `module-base` before `module-child`).
3. **Per module in that order:**
    - Run `upgrade-providers.py` (providers + internal module version refs from current tags).
    - Run **terraform-version-commit** `--yes` (commit `.tf`, semver bump, tag). The version-commit script **pushes** the repo and tags by default, so the next module in the chain sees the new version.

**Usage:**

```bash
python3 /path/to/skills/terraform-provider-upgrade/scripts/upgrade-all-with-deps.py <tf_modules_root> [report_path] [--dry-run]
```

- **tf_modules_root (required):** Directory that contains all `tf-module-*` git repos.
- **report_path (optional):** Same as in `upgrade-providers.py`; skipped modules are appended here.
- **--dry-run:** Only print the dependency order; do not run upgrade or version-commit.

**Integration with terraform-version-commit:** The version-commit skill is invoked with `--yes` and **without** `--no-push`, so it commits, tags, and runs `git push` / `git push --tags` immediately after each tag. That way the next module’s `upgrade-providers.py` run will see the updated tag when
resolving internal module versions.

## Manual Fallback

When the script is not used:

1. For each provider in `required_providers`, run: `curl -s "https://registry.terraform.io/v1/providers/<namespace>/<type>/versions" | jq -r '.versions[].version' | sort -V | tail -1`
2. Update the `version` constraint in the relevant `.tf` file.
3. Run `terraform init -upgrade` and `terraform validate` from the root.
