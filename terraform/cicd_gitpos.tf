############################################
# CI/CD + GitOps Resources
# - ECR Repository
# - GitHub Actions OIDC
# - IAM Role for GitHub Actions
############################################

############################
# Data sources
############################
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

############################
# ECR Repository
############################
resource "aws_ecr_repository" "app_repo" {
  name                 = "data-pipeline-app"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name        = "data-pipeline-app"
    Environment = "dev"
    ManagedBy   = "Terraform"
  }
}

resource "aws_ecr_lifecycle_policy" "app_repo_policy" {
  repository = aws_ecr_repository.app_repo.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 30 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

############################
# GitHub Actions OIDC Provider
############################
resource "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com"
  ]

  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1"
  ]

  tags = {
    Name      = "github-actions-oidc"
    ManagedBy = "Terraform"
  }
}

############################
# GitHub Actions IAM Role
############################
resource "aws_iam_role" "github_actions_role" {
  name = "GitHubActionsDeployRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github_actions.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            # 예: repo:yooseongjin527/asac_de2_infra_1st:ref:refs/heads/main
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/${var.github_branch}"
          }
        }
      }
    ]
  })

  tags = {
    Name      = "GitHubActionsDeployRole"
    ManagedBy = "Terraform"
  }
}

############################
# GitHub Actions Inline Policy
############################
resource "aws_iam_role_policy" "github_actions_ecr_policy" {
  name = "GitHubActionsEcrPolicy"
  role = aws_iam_role.github_actions_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ECR 로그인 토큰
      {
        Sid    = "EcrAuth"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      },

      # ECR 이미지 push / 조회
      {
        Sid    = "EcrPushPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = aws_ecr_repository.app_repo.arn
      },

      # GitHub Actions에서 배포 확인용으로 EKS 정보 조회
      {
        Sid    = "EksReadOnly"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListClusters",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeVpcs"
        ]
        Resource = "*"
      }
    ]
  })
}

############################
# Optional: outputs
############################
output "ecr_repository_name" {
  description = "ECR repository name"
  value       = aws_ecr_repository.app_repo.name
}

output "ecr_repository_url" {
  description = "ECR repository URL"
  value       = aws_ecr_repository.app_repo.repository_url
}

output "github_actions_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC"
  value       = aws_iam_role.github_actions_role.arn
}

output "github_actions_oidc_provider_arn" {
  description = "OIDC provider ARN for GitHub Actions"
  value       = aws_iam_openid_connect_provider.github_actions.arn
}
