# Data Lake용 S3 버킷 (eu-west-1 생성)
resource "aws_s3_bucket" "data_lake" {
  bucket = "data-pipeline-lake-${random_string.suffix.result}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.platform.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# 퍼블릭 접근 원천 차단
resource "aws_s3_bucket_public_access_block" "data_lake_block" {
  bucket                  = aws_s3_bucket.data_lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 데이터 수명 주기 정책 (비용 최적화)
resource "aws_s3_bucket_lifecycle_configuration" "data_lake_lifecycle" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    id     = "archive_cold_data"
    status = "Enabled"

    # [추가] 빈 필터를 넣어 버킷 내 모든 객체에 적용됨을 명시함
    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }
}
