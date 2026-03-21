---
name: tester-devops
description: DevOps/IaC test strategy—validation, policy, and plan review commands by tool.
domain: devops
---

# Tester — DevOps/IaC

## Test Strategy
- Syntax validation: all manifests/configs parse correctly
- Semantic validation: resources are well-formed, references resolve, constraints met
- Policy compliance: OPA/conftest policies pass, security baselines met
- Plan review: terraform plan shows expected changes, no unexpected destroys
- Dry-run apply: kubectl --dry-run=server, helm template validation

## Commands by tool (ALL must exit 0)
Terraform:
- `terraform fmt -check -recursive`
- `terraform init -backend=false` (validation only)
- `terraform validate`
- `tflint --recursive`
- `terraform plan -detailed-exitcode` (exit 0 or 2 OK; exit 1 = FAIL)
Kubernetes:
- `kubeconform --strict --kubernetes-version <ver> <manifests>`
- `kubectl apply --dry-run=server -f <manifests>` (if cluster access)
- `conftest test <manifests>` (if policies defined)
- `kube-score score <manifests>` (informational)
Helm:
- `helm lint <chart>`
- `helm template <chart> | kubeconform --strict`
Crossplane:
- `kubectl apply --dry-run=client -f <compositions>`

## Conventions
- Always validate BEFORE any apply — never test in production
- terraform plan output must be reviewed: count adds/changes/destroys
- Flag any unexpected destroy as blocking issue
- Verify idempotency: running validate/plan twice gives same result

## Anti-patterns
- No skipping validation because "it worked last time"
- No ignoring tflint warnings on security-related rules
- No testing with production credentials/state in dev
