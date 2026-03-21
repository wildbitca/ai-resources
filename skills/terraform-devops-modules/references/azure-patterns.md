# Azure Terraform Patterns

## VNet Module

- Subnets for compute, databases, app services.
- Output: `vnet_id`, `vnet_name`, `subnet_ids`, `subnet_names`.

## AKS Module

- Receives `vnet_id`, `subnet_id`; uses Azure CNI or Kubenet.
- Default: RBAC, Azure AD integration, private cluster option.
- Output: `cluster_id`, `kube_config` (sensitive), `oidc_issuer_url`.

## Azure SQL Module

- Private endpoint; no public access.
- TDE; backup retention.
- Output: `fqdn`, `id`; use Key Vault for admin password.

## Storage Account Module

- Https only; minimal public network access.
- Encryption; versioning for blob.
- Output: `storage_account_id`, `primary_blob_endpoint`.

## IAM

- Use `azurerm_role_assignment` with `azurerm_user_assigned_identity`.
- Prefer managed identity over service principal keys.
