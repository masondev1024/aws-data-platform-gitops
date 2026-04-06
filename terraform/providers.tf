terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
    helm = { source = "hashicorp/helm", version = "~> 2.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.0" }
    }
}
provider "aws" {
  region = var.aws_region
}

# EKS 생성 후 인증 정보를 가져오기 위한 데이터 소스
data "aws_eks_cluster_auth" "cluster" { name = aws_eks_cluster.main.name }

provider "kubernetes" {
  host                   = aws_eks_cluster.main.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.main.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.cluster.token
}

provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.main.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.main.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.cluster.token
  }
  # [주의] 이 블록은 2단계에서 주석을 해제합니다.
  # backend "s3" {
  #   bucket         = "data-engineer-tf-state-xxxxx" # backend.resource.tf에서 생성될 버킷명
  #   key            = "global/s3/terraform.tfstate"
  #   region         = "eu-west-1"
  #   dynamodb_table = "data-engineer-tf-locks"
  #   encrypt        = true
  # }

}
