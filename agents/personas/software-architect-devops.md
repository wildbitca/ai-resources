---
name: software-architect-devops
description: DevOps/IaC architecture validation, enforced patterns, and plan rejection red flags.
domain: devops
---

# Software Architect — DevOps/IaC

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
