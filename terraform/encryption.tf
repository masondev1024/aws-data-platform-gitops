resource "aws_kms_key" "platform" {
  description             = "D2C platform encryption key for short-lived validation resources"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "platform" {
  name          = "alias/d2c-platform"
  target_key_id = aws_kms_key.platform.key_id
}
