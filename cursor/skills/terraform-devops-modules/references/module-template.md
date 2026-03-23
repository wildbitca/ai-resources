# Module Template Reference

## versions.tf (per module)

```hcl
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

Check registry for latest: AWS ~> 5.x, GCP ~> 5.x, Azure ~> 4.x (as of 2024).

## variables.tf (minimal required + defaults)

```hcl
variable "env" {
  description = "Environment (dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags for resources"
  type        = map(string)
  default     = {}
}
```

## outputs.tf (descriptions + sensitive)

```hcl
output "id" {
  description = "Resource identifier"
  value       = aws_example.main.id
}

output "secret_value" {
  description = "Sensitive value (do not log)"
  value       = aws_secretsmanager_secret.example.secret_string
  sensitive   = true
}
```
