terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # [참고] backend_resources.tf를 먼저 apply 한 후, 생성된 S3 버킷 이름을 확인하고 주석을 해제하세요.
  # backend "s3" {
  #   bucket         = "data-engineer-tf-state-gx5de6" 
  #   key            = "global/s3/terraform.tfstate"
  #   region         = "eu-west-1" # 단일 리전
  #   dynamodb_table = "data-engineer-tf-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region
}
