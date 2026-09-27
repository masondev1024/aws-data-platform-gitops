variable "vpc_cidr" {
  type    = string
  default = "10.0.32.0/20"
}
variable "allowed_ssh_location" {
  description = "Optional administrator CIDR for legacy SSH access; SSM is the default access path."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.allowed_ssh_location == null ? true : can(cidrhost(var.allowed_ssh_location, 0))
    error_message = "allowed_ssh_location must be a valid CIDR block or null."
  }
}

variable "aws_region" {
  description = "The AWS region to deploy the infrastructure in"
  type        = string
  default     = "eu-west-1"
}

variable "cluster_endpoint_public_access" {
  description = "Keep the EKS API public only for bootstrap from an approved CIDR; set false when Terraform runs inside the VPC."
  type        = bool
  default     = false
}

variable "cluster_api_allowed_cidrs" {
  description = "Explicit administrator or CI CIDRs allowed to reach the EKS public API during bootstrap."
  type        = list(string)
  default     = []

  validation {
    condition = (
      !var.cluster_endpoint_public_access
      || (
        length(var.cluster_api_allowed_cidrs) > 0
        && alltrue([for cidr in var.cluster_api_allowed_cidrs : can(cidrhost(cidr, 0))])
      )
    )
    error_message = "cluster_api_allowed_cidrs must contain valid CIDRs when public EKS API access is enabled."
  }
}

variable "github_owner" {
  description = "GitHub owner or organization name"
  type        = string
  default     = "masondev1024"
}

variable "github_owner_id" {
  description = "Stable GitHub owner ID used by the immutable Actions OIDC subject claim"
  type        = string
  default     = "269997727"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "aws-data-platform-gitops"
}

variable "github_repo_id" {
  description = "Stable GitHub repository ID used by the immutable Actions OIDC subject claim"
  type        = string
  default     = "1202584860"
}

variable "github_branch" {
  description = "GitHub branch allowed to assume the OIDC role"
  type        = string
  default     = "main"
}

variable "db_password" {
  description = "Master password for the RDS database"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.db_password) >= 16 && length(var.db_password) <= 41
    error_message = "db_password must contain between 16 and 41 characters (AWS RDS limit)."
  }
}

variable "alertmanager_webhook_url" {
  description = "Webhook URL used by Alertmanager for SLO and rollout alerts"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.alertmanager_webhook_url) >= 20
    error_message = "alertmanager_webhook_url must be a non-empty webhook URL."
  }
}

variable "enable_rds_replica" {
  description = "Create the read replica only for an explicitly approved HA/replica-lag test."
  type        = bool
  default     = false
}

variable "enable_rds_multi_az" {
  description = "Enable synchronous Multi-AZ standby for an explicitly approved RDS failover drill."
  type        = bool
  default     = false
}

variable "enable_multi_az_nat" {
  description = "Create the second NAT Gateway only for an explicitly approved multi-AZ resilience test."
  type        = bool
  default     = false
}

variable "eks_node_instance_type" {
  description = "Worker instance type for the short-lived application validation profile."
  type        = string
  default     = "t3.medium"
}

variable "eks_node_desired_size" {
  description = "Minimum worker count for short-lived validation."
  type        = number
  default     = 1

  validation {
    condition     = var.eks_node_desired_size >= 1
    error_message = "eks_node_desired_size must be at least 1."
  }
}

variable "eks_node_min_size" {
  description = "Minimum managed node group size."
  type        = number
  default     = 1

  validation {
    condition     = var.eks_node_min_size >= 1
    error_message = "eks_node_min_size must be at least 1."
  }
}

variable "eks_node_max_size" {
  description = "Maximum managed node group size for the validation profile."
  type        = number
  default     = 2

  validation {
    condition     = var.eks_node_max_size >= var.eks_node_min_size
    error_message = "eks_node_max_size must be greater than or equal to eks_node_min_size."
  }
}

variable "allow_full_stack_apply" {
  description = "Explicit cost approval gate for the complete EKS/RDS/NAT/ALB stack."
  type        = bool
  default     = false
}
