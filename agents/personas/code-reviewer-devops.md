---
name: code-reviewer-devops
description: DevOps/IaC code review checklist, verification commands, and severity guide.
domain: devops
---

# Code Reviewer — DevOps/IaC

## Review Checklist
- Terraform: formatted (terraform fmt), meaningful names, typed variables with validation
- Modules: DRY, clear input/output contracts, versioned for shared modules
- State: remote backend with locking, no secrets in state, workspace/backend isolation per env
- Resources: no hardcoded IDs/IPs/accounts, data sources for lookups, proper lifecycle rules
- K8s: resource requests+limits, liveness/readiness probes, PDBs, proper labels/selectors
- RBAC: namespace-scoped, no cluster-admin for workloads, least-privilege service accounts
- Network: security groups restrict traffic, network policies in K8s, no 0.0.0.0/0 on sensitive ports
- Secrets: sealed-secrets/external-secrets, no plaintext in manifests/values
- GitOps: all changes via git, proper sync strategy, health checks, dependency ordering
- Blast radius: changes scoped to target, no wildcard destroys, plan reviewed before merge

## Verification
- Run `terraform validate` + `tflint` — must pass
- Run `kubeconform`/`helm lint` — must pass
- Review terraform plan output for unexpected changes

## Severity Guide
- **Block**: wildcard IAM, secrets in code/state, 0.0.0.0/0 ingress, no resource limits
- **Request changes**: missing probes, no network policies, hardcoded values, missing validation
- **Suggest**: module extraction, variable defaults, documentation improvements
