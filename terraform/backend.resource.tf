# 테라폼 상태를 저장할 S3 버킷
resource "aws_s3_bucket" "terraform_state" {
  bucket = "data-engineer-tf-state-${random_string.suffix.result}" # 버킷 이름은 전 세계 고유해야 함
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
