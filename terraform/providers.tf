terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    helm = { source = "hashicorp/helm", version = "~> 2.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.0" }
  }

  # backend 블록은 반드시 terraform 블록 '안'에 있어야 합니다.
  # 현재는 apply -target 중이므로 주석 처리가 되어 있어야 합니다.
  # backend "s3" {
  #   bucket         = "data-engineer-tf-state-xxxxx" 
  #   key            = "global/s3/terraform.tfstate"
  #   region         = "eu-west-1"
  #   dynamodb_table = "data-engineer-tf-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region
}
# EKS 클러스터 인증 정보를 가져오기 위한 데이터 소스 선언
data "aws_eks_cluster_auth" "cluster" {
  name = aws_eks_cluster.main.name
}

# helm provider 내부에는 backend가 들어갈 수 없습니다.
provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.main.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.main.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.cluster.token
  }
}

