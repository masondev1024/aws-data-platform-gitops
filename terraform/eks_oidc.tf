# EKS service accounts use the cluster OIDC issuer. The GitHub Actions OIDC
# provider is a separate trust domain and must not be reused for IRSA roles.
data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]

  tags = {
    Name      = "eks-oidc"
    ManagedBy = "Terraform"
  }
}
