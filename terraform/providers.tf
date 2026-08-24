terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls        = { source = "hashicorp/tls", version = "~> 4.0" }
    helm       = { source = "hashicorp/helm", version = "~> 2.0" }
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
# helm provider 내부에는 backend가 들어갈 수 없습니다.
provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.main.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.main.certificate_authority[0].data)

    # SSO 토큰은 짧은 수명을 가지므로 plan 시점에 미리 읽은
    # aws_eks_cluster_auth 값을 재사용하지 않고, Helm 요청 시 AWS CLI가
    # 현재 세션의 토큰을 발급하도록 한다.
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = [
        "eks",
        "get-token",
        "--cluster-name",
        aws_eks_cluster.main.name,
        "--region",
        var.aws_region,
      ]
    }
  }
}
