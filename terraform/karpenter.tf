# Karpenter Controller용 IAM Role
module "karpenter_role" {
  source                             = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version                            = "~> 5.0"
  role_name                          = "karpenter-controller-role"
  attach_karpenter_controller_policy = true
  karpenter_controller_cluster_name  = aws_eks_cluster.main.name
  oidc_providers = {
    main = {
      provider_arn               = aws_iam_openid_connect_provider.eks.arn
      namespace_service_accounts = ["karpenter:karpenter"]
    }
  }
}

# Karpenter 설치
resource "helm_release" "karpenter" {
  namespace        = "karpenter"
  create_namespace = true
  name             = "karpenter"
  repository       = "oci://public.ecr.aws/karpenter"
  chart            = "karpenter"
  version          = "v0.32.1"

  set {
    name  = "settings.aws.clusterName"
    value = aws_eks_cluster.main.name
  }

  set {
    name  = "settings.aws.defaultInstanceProfile"
    value = aws_iam_instance_profile.bastion.name
  }

  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = module.karpenter_role.iam_role_arn
  }
}
