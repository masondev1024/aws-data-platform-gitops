variable "aws_region" {
  description = "Explicit AWS region approved for this isolated live lab."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be an explicit AWS region such as eu-west-1."
  }
}

variable "aws_account_id" {
  description = "Twelve-digit AWS account ID approved for this isolated live lab."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "operator_cidr" {
  description = "Single operator or approved CI IPv4 /32 allowed to reach the public EKS API during bootstrap."
  type        = string

  validation {
    condition     = can(cidrhost(var.operator_cidr, 0)) && can(regex("/32$", var.operator_cidr)) && var.operator_cidr != "0.0.0.0/0"
    error_message = "operator_cidr must be a single IPv4 /32 and must not be 0.0.0.0/0."
  }
}

variable "operator_name" {
  description = "Human operator accountable for cost, teardown, and evidence export."
  type        = string

  validation {
    condition     = length(trimspace(var.operator_name)) >= 3
    error_message = "operator_name must identify the accountable operator."
  }
}

variable "approval_id" {
  description = "External approval record confirming account, region, cost ceiling, and teardown scope."
  type        = string

  validation {
    condition     = can(regex("^SS0-[0-9]{8}-[A-Za-z0-9._-]{3,64}$", var.approval_id))
    error_message = "approval_id must match SS0-YYYYMMDD-<ticket>; this remains pending until leader obtains user approval."
  }
}

variable "session_id" {
  description = "Short-lived session identifier copied into every resource tag."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{5,40}$", var.session_id))
    error_message = "session_id must be lowercase letters, digits, and hyphens, 6-41 characters."
  }
}

variable "cost_budget_usd" {
  description = "Approved USD planning ceiling for the whole live lab session. This is a human guardrail recorded in tags, not an AWS hard spending cap."
  type        = number
  default     = 5.50

  validation {
    condition     = var.cost_budget_usd > 0 && var.cost_budget_usd <= 5.50
    error_message = "This approved session profile is limited to USD 5.50 including reserve."
  }
}

variable "max_session_hours" {
  description = "Whole-session planning lifetime including creation and cleanup. Start cleanup by two hours; this tag is not an automated AWS stop condition."
  type        = number
  default     = 3

  validation {
    condition     = var.max_session_hours >= 1 && var.max_session_hours <= 3
    error_message = "max_session_hours must be between 1 and 3, including cleanup."
  }
}

variable "recovery_contact" {
  description = "Non-sensitive identifier for the local operator responsible for session recovery; do not put personal contact details in resource tags."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$", var.recovery_contact))
    error_message = "recovery_contact must be a non-sensitive operator or task identifier."
  }
}

variable "apply_approval_phrase" {
  description = "Final manual gate. Keep PLAN_ONLY before the approved AWS live phase."
  type        = string

  validation {
    condition     = contains(["PLAN_ONLY", "APPROVED_FOR_EPHEMERAL_APPLY"], var.apply_approval_phrase)
    error_message = "apply_approval_phrase must be PLAN_ONLY or APPROVED_FOR_EPHEMERAL_APPLY."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the isolated lab VPC."
  type        = string
  default     = "10.72.0.0/20"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "availability_zones" {
  description = "Two availability zones in aws_region for the isolated public worker subnets and private DB subnets."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) == 2 && alltrue([for az in var.availability_zones : startswith(az, var.aws_region)])
    error_message = "availability_zones must contain exactly two AZs from aws_region."
  }
}

variable "eks_kubernetes_version" {
  description = "EKS Kubernetes minor version in standard support for this lab."
  type        = string
  default     = "1.36"

  validation {
    condition     = contains(["1.35", "1.36"], var.eks_kubernetes_version)
    error_message = "eks_kubernetes_version must be 1.35 or 1.36 to avoid accidental extended-support cluster creation."
  }
}

variable "eks_node_instance_type" {
  description = "Managed node group instance type sized for Argo CD, Prometheus, MySQL, app rollout overlap, and system headroom. Jenkins stays on bounded local Docker."
  type        = string
  default     = "m7i.xlarge"
}

variable "eks_node_min_size" {
  description = "Fixed two-node worker floor for AZ-spread failure tests; autoscaling beyond this cost-bounded pool is disabled."
  type        = number
  default     = 2

  validation {
    condition     = var.eks_node_min_size == 2
    error_message = "eks_node_min_size is fixed at 2 for this cost-bounded live lab."
  }
}

variable "eks_node_desired_size" {
  description = "Desired worker count for the short-lived validation lab."
  type        = number
  default     = 2

  validation {
    condition     = var.eks_node_desired_size == 2
    error_message = "eks_node_desired_size is fixed at 2 for this cost-bounded live lab."
  }
}

variable "eks_node_max_size" {
  description = "Maximum worker count for the short-lived validation lab."
  type        = number
  default     = 2

  validation {
    condition     = var.eks_node_max_size == 2
    error_message = "eks_node_max_size is fixed at 2; add capacity only after a separately approved estimate."
  }
}

variable "db_username" {
  description = "RDS MySQL master username. This is not secret material; the password must come from the approved external Secrets Manager secret."
  type        = string
  default     = "raffle_admin"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{2,15}$", var.db_username))
    error_message = "db_username must start with a letter and contain only letters, digits, and underscores, 3-16 characters."
  }
}

variable "db_master_password_secret_id" {
  description = "Name or ARN of an operator-created AWS Secrets Manager secret whose AWSCURRENT secret string is the RDS master password. Terraform reads it through an ephemeral resource at apply; Terraform must not create or store the secret value."
  type        = string

  validation {
    condition     = length(trimspace(var.db_master_password_secret_id)) >= 6
    error_message = "db_master_password_secret_id must identify an existing operator-seeded Secrets Manager secret."
  }
}

variable "db_master_password_secret_version_stage" {
  description = "Secrets Manager staging label to read in-memory for the RDS master password."
  type        = string
  default     = "AWSCURRENT"

  validation {
    condition     = can(regex("^[A-Za-z0-9/_+=.@-]{1,256}$", var.db_master_password_secret_version_stage))
    error_message = "db_master_password_secret_version_stage must be a valid Secrets Manager staging label."
  }
}

variable "db_password_wo_version" {
  description = "Monotonic password version passed to aws_db_instance.password_wo_version. Increment after rotating the externally seeded Secrets Manager password."
  type        = number
  default     = 1

  validation {
    condition     = var.db_password_wo_version >= 1 && floor(var.db_password_wo_version) == var.db_password_wo_version
    error_message = "db_password_wo_version must be a positive integer."
  }
}

variable "waf_enforcement_mode" {
  description = "WAF rule behavior. COUNT is the safe default; BLOCK requires explicit live approval."
  type        = string
  default     = "COUNT"

  validation {
    condition     = contains(["COUNT", "BLOCK"], var.waf_enforcement_mode)
    error_message = "waf_enforcement_mode must be COUNT or BLOCK."
  }
}

variable "waf_drill_header_value" {
  description = "Benign custom header value used for the WAF violation drill."
  type        = string
  default     = "kyobo-live-lab-deny"

  validation {
    condition     = can(regex("^[A-Za-z0-9._:-]{8,64}$", var.waf_drill_header_value))
    error_message = "waf_drill_header_value must be a bounded token, not an external attack payload."
  }
}
