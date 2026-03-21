# AWS Terraform Patterns

## VPC Module

- Public/private subnets across AZs; NAT gateway in public for private egress.
- Output: `vpc_id`, `public_subnet_ids`, `private_subnet_ids`, `nat_gateway_ids`.
- Security: No `0.0.0.0/0` ingress; restrict NAT egress if required.

## EKS Module

- Receives `vpc_id`, `subnet_ids`; creates cluster with OIDC for IRSA.
- Default: private endpoint, cluster logging enabled, encryption for secrets.
- Output: `cluster_id`, `cluster_endpoint`, `cluster_certificate_authority_data` (sensitive), `oidc_provider_arn`.

## RDS Module

- Private subnet; no public access by default.
- Encryption at rest; parameter group for secure defaults.
- Output: `endpoint`, `port`, `master_secret_arn` (use Secrets Manager, not plain password in output).

## S3 Module

- Versioning enabled; encryption (AES-256 or KMS).
- Block public access by default.
- Output: `bucket_id`, `bucket_arn`, `bucket_domain_name`.

## IAM

- Use `aws_iam_role` + `aws_iam_role_policy` or attach AWS managed policies.
- Prefer `assume_role_policy` with OIDC for workloads (EKS, GitHub Actions).
