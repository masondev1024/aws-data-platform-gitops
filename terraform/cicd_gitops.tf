# ECR 레포지토리 (eu-west-1 생성)
resource "aws_ecr_repository" "app_repo" {
  name                 = "data-pipeline-app"
  image_tag_mutability = "MUTABLE"
  force_delete         = true 
}

resource "aws_ecr_lifecycle_policy" "app_repo_policy" {
  repository = aws_ecr_repository.app_repo.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

# GitHub Actions용 OIDC Provider (글로벌 IAM)
resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions_role" {
  name = "GitHubActionsDeployRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github_actions.arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        # [주의] 본인의 GitHub 계정명/레포지토리명으로 수정 필수
        "StringLike" = { "token.actions.githubusercontent.com:sub": "repo:YourGitHubName/YourRepoName:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_ecr_access" {
  role       = aws_iam_role.github_actions_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser"
}
