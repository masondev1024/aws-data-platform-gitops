resource "terraform_data" "full_stack_apply_guardrail" {
  input = var.allow_full_stack_apply

  lifecycle {
    precondition {
      condition     = var.allow_full_stack_apply
      error_message = "전체 EKS/RDS/NAT/ALB 스택은 비용 승인 후 -var='allow_full_stack_apply=true'를 명시해야 합니다. 단기 검증은 저비용 validation 프로필을 사용하세요."
    }
  }
}
