---
name: crossplane-upjet-building
description: Build, configure, and maintain Crossplane providers with Upjet. Use when setting up or scaffolding a new upjet provider from the template, generating CRDs and controllers from Terraform/OpenTofu provider schemas, fixing CI (check-diff, lint), configuring publish pipelines (Upbound Marketplace, GHCR), migrating Terraform to Crossplane managed resources, or debugging schema/codegen issues. Integrates with terraform-to-crossplane-migration and kro-rgd-from-crossplane when converting Terraform or designing custom APIs over Crossplane CRs.
globs: "**/crossplane/**", "infra/**", "**/*.tf"
triggers: "crossplane, upjet, provider, CRD, controller, codegen, scaffold provider, upbound, GHCR, terraform schema, managed resource, crossplane build"
---

# Crossplane & Upjet Provider Building

Senior DevOps and platform engineering workflow for building and maintaining Upjet-based Crossplane providers. Use Kubernetes, Crossplane, Terraform/OpenTofu, and Go tooling correctly; invoke related skills when the task involves Terraform→Crossplane conversion or custom APIs (e.g. kro RGDs).

## When to Use Related Skills

- **terraform-to-crossplane-migration**: Converting Terraform resources or modules to Crossplane CRs (ProviderConfig, XRDs, Compositions); mapping providers and sync order.
- **kro-rgd-from-crossplane**: Designing RGDs or custom APIs that wrap or bundle Crossplane provider CRs.
- **changelog-best-practices**: When cutting releases or updating CHANGELOG for provider versions.

## Prerequisites and Setup

- **Go** 1.24+ (match CI; use dev container for parity).
- **OpenTofu** (or Terraform) for schema generation: `tofu init` + `tofu providers schema -json` → `config/schema.json`. Prefer OpenTofu on arm64 to avoid QEMU/Go runtime issues.
- **goimports** on PATH: `go install golang.org/x/tools/cmd/goimports@latest`.
- **Kubernetes** (e.g. Kind) for `make run`.

First-time in a provider repo:

```bash
make submodules
make tools.install
make generate
```

## Generate and Validate (CI Parity)

- **check-diff**: Ensures generated code is committed. Run in dev container when Go/arch differs from CI: `make check-diff.ci` or `make dev ARGS="check-diff"`.
- **Host (skip version check):** `SKIP_LOCALSETUP=1 make check-diff`; ensure goimports is on PATH.
- **Fixing CI failures:** Regenerate, then commit: `make generate && make check-diff && git add -A && git commit -m "chore: regenerate for CI" && git push`.

## Schema Generation (OpenTofu vs Terraform)

- Upjet consumes `config/schema.json`. Generate via Terraform/OpenTofu provider schema (e.g. `tofu init` in a small module, then `tofu providers schema -json`).
- If Terraform crashes under QEMU on arm64, switch to **OpenTofu** in the Makefile: `OPENTOFU_VERSION`, `TOFU`/`TOFU_WORKDIR`, and replace `terraform` with `tofu` in targets; remove Terraform BSL checks.

## Pipelines (GitHub Actions)

- **CI:** All jobs run `make tools.install`. check-diff runs `make generate` and verifies no uncommitted generated files. Use latest action semver (e.g. `actions/checkout@v6`, `actions/setup-go@v6`); no SHA pins.
- **Publish to Upbound:** On tag push `v*`: install Up CLI (`curl -sL "https://cli.upbound.io" | sh`), `up login -t "${{ secrets.UPBOUND_TOKEN }}"`, then `up project build --repository "<image_path>"` and `up project push --repository "<image_path>" --tag="${{ github.ref_name }}"`. Require
  `UPBOUND_TOKEN` secret; fail fast with a clear message if missing.
- **upbound.yaml:** Minimal Project manifest in repo root so `up project build`/`push` recognize the repo (`spec.repository` = target image path).

## Starting a New Upjet Provider

1. Clone/fork [crossplane/upjet-provider-template](https://github.com/crossplane/upjet-provider-template).
2. Rename for provider (module path, image name, package name).
3. Set Terraform/OpenTofu provider source and version in Makefile/config; run `make generate`.
4. Add CI: `make tools.install`, check-diff, lint, tests. Optionally add dev container (`dev.Dockerfile`, `docker-compose.dev.yml`, targets `dev`, `check-diff.ci`) and OpenTofu if needed.
5. For Upbound publish: add tag-triggered workflow, `upbound.yaml`, and `UPBOUND_TOKEN` secret.

## Commands Quick Reference

| Task                   | Command                                                                                               |
|------------------------|-------------------------------------------------------------------------------------------------------|
| Generate               | `make submodules && make generate`                                                                    |
| Check CI parity        | `make check-diff.ci` or `make dev ARGS="check-diff"`                                                  |
| Run provider           | `make run`                                                                                            |
| Build                  | `make build`; full install `make all`                                                                 |
| Upbound CLI            | `curl -sL "https://cli.upbound.io" \| sh`; login `up login -t "$UPBOUND_TOKEN"`                       |
| Build + push (Upbound) | `up project build --repository "<image>"` then `up project push --repository "<image>" --tag="<tag>"` |

## Conventions

- One resource per PR when adding managed resources; add tests.
- Keep `upbound.yaml` and workflow `IMAGE_PATH` in sync with the target Marketplace image.
- For detailed project-specific notes (e.g. provider-upjet-cloudflare customizations), see workspace `docs/CROSSPLANE-UPJET-REFERENCE.md` if present.
