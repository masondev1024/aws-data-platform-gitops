output "cluster_name" {
  description = "EKS cluster name for approved live-lab evidence collection."
  value       = aws_eks_cluster.lab.name
}

output "vpc_id" {
  description = "VPC ID passed explicitly to the AWS Load Balancer Controller to avoid relying on EC2 metadata discovery."
  value       = aws_vpc.lab.id
}

output "ecr_repository_url" {
  description = "Immutable ECR repository URL for the approved local source artifact."
  value       = aws_ecr_repository.app.repository_url
}

output "waf_web_acl_arn" {
  description = "Regional WAF ACL ARN. This is not secret material."
  value       = aws_wafv2_web_acl.lab.arn
}

output "db_writer_endpoint" {
  description = "Private RDS MySQL writer endpoint for the live-lab application secret."
  value       = aws_db_instance.mysql_primary.address
}

output "db_reader_endpoint" {
  description = "Private RDS MySQL async reader endpoint for read-only smoke checks."
  value       = aws_db_instance.mysql_reader.address
}

output "db_primary_identifier" {
  description = "Session-scoped RDS primary identifier for the approved failover drill."
  value       = aws_db_instance.mysql_primary.identifier
}

output "db_reader_identifier" {
  description = "Session-scoped asynchronous read replica identifier for the controlled promotion drill."
  value       = aws_db_instance.mysql_reader.identifier
}

output "db_fence_security_group_id" {
  description = "Empty-ingress security group used to block clients from the old writer after async replica promotion."
  value       = aws_security_group.db_fenced.id
}

output "operator_cidr" {
  description = "The approved single-operator /32 applied to the ingress boundary."
  value       = var.operator_cidr
}

output "session_id" {
  description = "Session identifier used for local rendering and resource inventory."
  value       = var.session_id
}

output "approval_id" {
  description = "Approval record identifier used for session-scoped ingress and resource tags."
  value       = var.approval_id
}

output "db_master_password_secret_id" {
  description = "Operator-seeded Secrets Manager secret identifier read ephemerally during apply. This is not the password value."
  value       = var.db_master_password_secret_id
}

output "aws_load_balancer_controller_role_arn" {
  description = "IRSA role ARN that must be annotated on the aws-load-balancer-controller ServiceAccount."
  value       = aws_iam_role.aws_load_balancer_controller.arn
}

output "aws_argo_rollouts_cloudwatch_role_arn" {
  description = "Session-scoped IRSA role for Argo Rollouts CloudWatch analysis."
  value       = aws_iam_role.argo_rollouts_cloudwatch.arn
}

output "waf_association_managed_by_operator" {
  description = "The local scoped deployment step associates the verified session ACL to the verified ALB."
  value       = true
}

output "resource_scope_tags" {
  description = "Tags that teardown and evidence collection must use to scope this session."
  value       = local.tags
}

output "apply_gate_state" {
  description = "Reminder that normal local validation stays PLAN_ONLY until explicit approval is recorded."
  value       = var.apply_approval_phrase
}
