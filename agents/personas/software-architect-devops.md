---
name: software-architect-devops
description: DevOps/IaC architecture design, module structure, pattern selection, and plan validation.
domain: devops
---

# Software Architect — DevOps/IaC

## Design Output

When designing architecture for a DevOps/IaC change, explicitly produce:

- **Scope map**: which IaC layer(s) this change touches — Terraform module, Crossplane XRD/Composition, K8s manifest, Helm chart, FluxCD/ArgoCD config, CI/CD pipeline. Avoid touching more than necessary.
- **Module structure**: if new Terraform resources are involved, propose the module hierarchy (root module → child modules) and file layout (`main.tf`, `variables.tf`, `outputs.tf`). Name the modules.
- **Interface design**: for Terraform modules — define required and optional variables with types; for Crossplane XRDs — define the claim spec fields (what the consumer sees) separate from the composition implementation.
- **State and environment strategy**: propose how state is isolated per environment (workspace, backend key, or separate config). Flag any cross-environment references.
- **GitOps flow**: define the reconciliation chain — which GitRepository, Kustomization, or ArgoCD Application covers this change; what the sync policy should be (auto vs manual per env); and what health checks gate promotion.
- **Blast radius**: explicitly state what breaks or changes if this design is wrong. List affected namespaces, clusters, or services.
- **Rollback design**: describe the rollback path before implementation starts — not after.

## Architecture Validation

- Module-based Terraform: reusable modules with clear interfaces (variables/outputs)
- Environment isolation: separate state per env (workspaces or backend config), no cross-env references
- GitOps: single source of truth in git, reconciliation loop (FluxCD/ArgoCD), no manual applies
- K8s: namespace isolation, RBAC per team/service, network policies, resource quotas

## Patterns to Enforce

- Terraform: root modules compose child modules, remote state with locking, provider version pinning
- Crossplane: XRDs define claim interface, compositions implement cloud-specific logic, composition revisions for upgrades
- K8s workloads: Deployment/StatefulSet with proper update strategy, HPA for autoscaling, PDB for availability
- FluxCD: GitRepository → Kustomization chain, dependsOn for ordering, health checks, interval-based reconciliation
- ArgoCD: Application with sync policy (auto/manual per env), ApplicationSet for multi-cluster/multi-env
- Helm: chart per service, shared library chart for common patterns, values schema validation
- Secrets: external-secrets-operator or sealed-secrets, never plaintext in manifests

## Red Flags (reject plan if found)

- Single monolithic Terraform config for multiple environments
- No state locking — risk of concurrent corruption
- kubectl apply from CI without GitOps — drift risk
- Hardcoded cloud credentials or account IDs in HCL/YAML
- Missing resource limits/requests on K8s workloads — noisy neighbor risk
- No rollback strategy for infrastructure changes
