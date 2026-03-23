---
name: terraform-to-crossplane-migration
description: Migrate infrastructure from Terraform (HCL) to Crossplane (Kubernetes CRDs). Use when converting Terraform modules or resources to Crossplane managed resources (ProviderConfig, Composition, XRD), or when moving IaC from Terraform to GitOps with Crossplane.
globs: "**/*.tf", "**/terraform/**", "infra/**"
---

# Terraform to Crossplane Migration

Migrate Terraform resources to Crossplane managed resources (upjet providers) or custom XRDs/Compositions. Preserve semantics, dependencies, and outputs.

## Mapping Overview

| Terraform | Crossplane |
|-----------|------------|
| `resource "X" "name"` | CustomResource (apiVersion/kind) or Composition instance |
| `data "X" "name"` | Read via Crossplane provider or pre-created resources |
| `module "m"` | Composition (multiple resources) or grouped CRs |
| `variable` / `output` | XRD spec/status or Composition schema |
| `depends_on` | `dependsOn` or implicit via references |
| `provider "github"` | `ProviderConfig` + provider CRs |

## Workflow

### 1. Identify provider and resources

- Map Terraform provider (e.g. `github`, `google`, `aws`) to Crossplane provider (e.g. `provider-upjet-github`, `provider-upjet-gcp`).
- Use Upbound provider docs or `kubectl get crd | grep <provider>` for resource kinds.

### 2. Per-resource conversion

- **Terraform block** → **Crossplane CR**:
  - `resource "repo_github_upbound_io_repository" "r"` → `Repository.repo.github.upbound.io` (apiVersion/kind from provider).
  - Map HCL arguments to `spec.forProvider` (or `spec.writeConnectionSecretToRef` for secrets).
  - Preserve `deletionPolicy: Delete` (default) or `Orphan` when needed.
  - Set `managementPolicies: ['*']` for full management.
  - Add `providerConfigRef.name: default` unless using a named config.

### 3. References and dependencies

- `resource.X.id` → `ref.name` or `id` from another CR; use `Ref` and `Selector` where supported.
- `depends_on` → order manifests (ArgoCD sync waves, `dependsOn` in Applications) or explicit refs.

### 4. Outputs and exports

- Terraform `output` → Crossplane `status` or a separate ConfigMap/Secret; document in XRD/Composition schema if custom.

### 5. State and secrets

- Do not migrate Terraform state; treat Crossplane as greenfield.
- Connection secrets: use `writeConnectionSecretToRef` and reference from consumers.

## Conventions

- **Naming**: Use `metadata.name` = Terraform resource name (K8s-safe: lowercase, hyphens).
- **Annotations**: Preserve `crossplane.io/external-name` when the provider expects it (e.g. external ID).
- **Organization**: One YAML file per resource type or logical group; use `---` for multi-doc.
- **Sync order**: Use `argocd.argoproj.io/sync-wave` for apply order (e.g. ProviderConfig → VPC → subnets → instances).

## Common Providers

- **GitHub (upbound)**: `repo.github.upbound.io`, `team.github.upbound.io`, `teamrepository.team.github.upbound.io`, etc.
- **GCP**: `provider-upjet-gcp` for `compute`, `sql`, `storage`, etc.
- **AWS**: `provider-upjet-aws` for equivalent AWS resources.

## Reference

- **Mapping**: See `references/mapping.md` for provider-specific Terraform→Crossplane resource mapping and sync order.

## Validation

- Apply with `kubectl apply -f` (or GitOps); verify with `kubectl get <kind>` and provider logs.
- Check `status.conditions` for reconciliation errors.
- Ensure ProviderConfig and credentials are configured before migrating dependent resources.
