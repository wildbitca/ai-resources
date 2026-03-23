---
name: terraform-devops-modules
description: DevOps Terraform and Cloud specialist for creating reusable, composable Terraform modules. Use when designing or implementing Terraform infrastructure, creating modules, or refactoring IaC. Enforces: module composition (child modules that use other modules), latest provider versions, power and flexibility with sensible defaults, and security-first posture from day 0.
globs: "**/*.tf", "**/terraform/**", "infra/**"
---

# Terraform DevOps Modules

Act as a DevOps Terraform and Cloud specialist. Create **reusable modules** with **composition** (modules that use other modules). Prioritize power and flexibility without excessive configurability. Apply **pro and security features from day 0**.

## Module Structure (Standard)

```
modules/<component-name>/
├── main.tf         # Core resources
├── variables.tf    # Inputs (few required, many with defaults)
├── outputs.tf      # Exposed values (mark sensitive when applicable)
├── versions.tf     # required_providers with latest-compatible constraints
└── README.md       # Usage, inputs, outputs
```

**REQUIRED:** Every module has `versions.tf` with explicit `required_providers`. **PROHIBITED:** Root-only provider config; child modules declare their needs.

## Provider Versions (Always Latest Compatible)

- **REQUIRED:** Use `version = ">= X.0"` or `~> X.0` so Terraform resolves the latest compatible version.
- Before creating a module, check the provider's latest major/minor on the [Terraform Registry](https://registry.terraform.io/browse/providers).
- Prefer `>= X.0, < (X+1).0` for stability; use `>= X.0` only when minor upgrades are acceptable.
- Example: `version = "~> 5.0"` for AWS provider allows 5.x, blocks 6.0.

## Module Composition (Child Uses Child)

- **Composition:** Modules may call other modules. Prefer **flat composition** at the root: root calls N child modules; each child is self-contained.
- When a component needs another (e.g. `ec2-instance` needs `vpc`), the **root** wires them: root calls `vpc`, passes `vpc_id`/`subnet_ids` to `ec2-instance`.
- **Dependency inversion:** Modules receive dependencies (VPC ID, subnet IDs, security group IDs) as variables—they do not create their own network.
- **REQUIRED:** One logical component per module. Compose at root or in a thin "umbrella" module.

Example root composition:

```hcl
module "network" {
  source   = "./modules/network"
  env      = var.env
  base_cidr = var.base_cidr
}

module "compute" {
  source       = "./modules/compute"
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  allowed_cidrs = var.allowed_cidrs
}
```

## Power and Flexibility Without Over-Config

- **Sensible defaults:** Most variables have defaults; few are required.
- **Required variables:** Only what truly varies per environment (e.g. `env`, `project_id`, `region`).
- **Optional variables:** Use for customization; default to secure/production-ready values.
- **PROHIBITED:** Exposing 20+ variables "por si acaso". Add variables when a real use case demands it.
- **Naming:** Use `env`, `project_id`, `name_prefix` or `name_suffix` for resource naming; avoid hardcoded names.

## Security and Pro Features (Day 0)

### State and Secrets

- **REQUIRED:** Remote state (S3 + DynamoDB, GCS, Azure Storage). Never commit `.tfstate`.
- **REQUIRED:** Enable encryption at rest for state bucket; use KMS/CMK when available.
- **REQUIRED:** Mark sensitive outputs: `output "db_password" { value = ...; sensitive = true }`.
- **PROHIBITED:** Secrets in variables, `.tf` files, or state in version control.

### IAM and Access

- **REQUIRED:** Least privilege. Grant minimum permissions for each role/resource.
- **REQUIRED:** Prefer IAM roles over long-lived keys; use `assume_role` and OIDC where possible.
- **REQUIRED:** Use `iam_instance_profile` / workload identity instead of access keys in compute.

### Networking

- **REQUIRED:** Private subnets for compute/databases; restrict public exposure.
- **REQUIRED:** Security groups with explicit `ingress`/`egress`; avoid `0.0.0.0/0` unless required and documented.

### Scanning and Validation

- **REQUIRED:** Run `terraform fmt -recursive` and `terraform validate` before commits.
- **RECOMMENDED:** Use `tfsec` or `checkov` in CI to detect misconfigurations.
- **RECOMMENDED:** Pin Terraform version in `required_version` (e.g. `>= 1.5.0`).

### Module Outputs

- **REQUIRED:** Every output has a `description`.
- **REQUIRED:** Sensitive data (passwords, tokens, keys) must have `sensitive = true`.

## HCL Style

- Indent 2 spaces; align `=` for consecutive single-line arguments.
- Meta-arguments first (`source`, `count`, `for_each`), then blocks.
- Use `snake_case` for resource names; avoid resource type in name (e.g. `main` not `vpc_vpc`).

## Workflow

1. Define the component boundary and its dependencies.
2. Create `versions.tf` with latest-compatible provider constraints.
3. Implement `main.tf` with sensible defaults; add variables only when needed.
4. Expose outputs for composition; mark sensitive ones.
5. Wire modules at root; avoid deep nesting.
6. Run `terraform fmt`, `terraform validate`, and `tfsec` (or equivalent).

## References

- **Module template:** See `references/module-template.md` for `versions.tf`, `variables.tf`, `outputs.tf` patterns.
- **AWS patterns:** See `references/aws-patterns.md` for VPC, EKS, RDS, S3.
- **GCP patterns:** See `references/gcp-patterns.md` for VPC, GKE, Cloud SQL.
- **Azure patterns:** See `references/azure-patterns.md` for VNet, AKS, Azure SQL.

Load these only when implementing for that cloud.
