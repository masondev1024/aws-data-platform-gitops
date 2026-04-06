variable "vpc_cidr" {
  type    = string
  default = "10.0.32.0/20"
}
# 현재는 팀원들의 원활한 공동 작업을 위해 SSH를 전체 개방(0.0.0.0/0) 
# 실제 운영 환경으로 전환 시에는 회사 VPN IP 또는 팀원들의 고정 IP로 제한
variable "allowed_ssh_location" {
  type    = string
  default = "0.0.0.0/0"
}

variable "aws_region" {
  description = "The AWS region to deploy the infrastructure in"
  type        = string
  default     = "eu-west-1" 
}

variable "github_owner" {
  description = "GitHub owner or organization name"
  type        = string
  default     = "masondev1024"
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "my-data-platform"
}

variable "github_branch" {
  description = "GitHub branch allowed to assume the OIDC role"
  type        = string
  default     = "main"
}
