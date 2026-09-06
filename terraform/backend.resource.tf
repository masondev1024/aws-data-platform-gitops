# 테라폼 상태를 저장할 S3 버킷
resource "aws_s3_bucket" "terraform_state" {
  bucket = "data-engineer-tf-state-${random_string.suffix.result}" # 버킷 이름은 전 세계 고유해야 함
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket                  = aws_s3_bucket.terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.platform.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# 상태 파일의 버전을 관리하여 실수로 삭제 시 복구 가능하도록 설정
resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# 동시 작업 시 상태 파일 락(Lock)을 걸어줄 DynamoDB 테이블
resource "aws_dynamodb_table" "terraform_locks" {
  name         = "data-engineer-tf-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  server_side_encryption {
    enabled = true
  }
  attribute {
    name = "LockID"
    type = "S"
  }
}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}
