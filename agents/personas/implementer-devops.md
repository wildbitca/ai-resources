---
name: implementer-devops
description: DevOps/IaC implementation conventions (Terraform, K8s, GitOps) for implementer agents.
domain: devops
---

# Implementer — DevOps/IaC

## Conventions

- Terraform: HCL formatting (terraform fmt), meaningful resource names, locals for computed values
- Terraform modules: clear variable/output contracts, typed variables with validation blocks, sensible defaults
- Data sources over hardcoded IDs; lifecycle rules (prevent_destroy, create_before_destroy) where needed
- State: remote backend with locking (S3+DynamoDB, GCS, Terraform Cloud), workspaces for env isolation
- Crossplane: compositions with proper patching, XRDs with OpenAPI validation, provider configs via secrets
- K8s manifests: declarative, proper labels/annotations/selectors, resource requests+limits on all containers
- Liveness/readiness probes on all deployments, PDBs for HA workloads
- FluxCD: GitRepository + Kustomization with health checks, dependsOn for ordering
- ArgoCD: Application/ApplicationSet with sync policy, self-heal for non-sensitive resources
- Helm: parameterized values.yaml, no hardcoded secrets, NOTES.txt with usage info
- GitOps: all changes via git — no kubectl apply from local, no manual terraform apply in prod

## Anti-patterns

- No secrets in HCL/YAML/state — use sealed-secrets, external-secrets-operator, or vault
- No wildcard IAM permissions (*, :*) — least-privilege always
- No 0.0.0.0/0 ingress on sensitive ports (SSH, DB, admin dashboards)
- No hardcoded AMI IDs, IP addresses, or account numbers — variables/data sources
- No terraform apply without plan review in production
- No cluster-admin RBAC for workloads — namespace-scoped roles
