# GCP Terraform Patterns

## VPC Module

- Subnets with secondary ranges for GKE; Private Google Access for private subnets.
- Output: `network_id`, `network_name`, `subnet_ids`, `subnet_self_links`.

## GKE Module

- Receives `network`, `subnet`; uses private cluster, Workload Identity.
- Default: node auto-repair, release channel, shielded nodes.
- Output: `cluster_id`, `cluster_endpoint`, `cluster_ca_certificate` (sensitive).

## Cloud SQL Module

- Private IP only; no public IP by default.
- Encryption; point-in-time recovery for prod.
- Output: `connection_name`, `private_ip`; use Secret Manager for passwords.

## GCS Module

- Uniform bucket-level access; versioning.
- Encryption with Google-managed or CMEK keys.
- Output: `bucket_name`, `bucket_url`.

## IAM

- Use `google_project_iam_member` or `google_service_account` + `google_project_iam_member`.
- Prefer Workload Identity for GKE pods.
