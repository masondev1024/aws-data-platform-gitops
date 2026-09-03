resource "terraform_data" "full_stack_apply_guardrail" {
  input = var.allow_full_stack_apply

  lifecycle {
    precondition {
      condition     = var.allow_full_stack_apply
      error_message = "전체 EKS/RDS/NAT/ALB 스택은 비용 승인 후 -var='allow_full_stack_apply=true'를 명시해야 합니다. 단기 검증은 저비용 validation 프로필을 사용하세요."
    }
  }
}

resource "terraform_data" "rds_multi_az_guardrail" {
  input = {
    allow_full_stack_apply = var.allow_full_stack_apply
    enable_rds_multi_az    = var.enable_rds_multi_az
  }

  lifecycle {
    precondition {
      condition     = !var.enable_rds_multi_az || var.allow_full_stack_apply
      error_message = "RDS Multi-AZ failover는 비용 승인 후 -var='allow_full_stack_apply=true'와 -var='enable_rds_multi_az=true'를 함께 명시해야 합니다."
    }
  }
}
