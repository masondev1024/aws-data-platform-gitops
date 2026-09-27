provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = local.tags
  }
}

locals {
  name_prefix = "kyobo-${var.session_id}"
  public_subnet_cidrs = [
    cidrsubnet(var.vpc_cidr, 4, 0),
    cidrsubnet(var.vpc_cidr, 4, 1),
  ]
  private_db_subnet_cidrs = [
    cidrsubnet(var.vpc_cidr, 4, 8),
    cidrsubnet(var.vpc_cidr, 4, 9),
  ]

  tags = {
    Project       = "kyobo-platform-live-lab"
    Session       = var.session_id
    Approval      = var.approval_id
    Operator      = var.operator_name
    CostBudgetUSD = tostring(var.cost_budget_usd)
    MaxHours      = tostring(var.max_session_hours)
    Recovery      = var.recovery_contact
    ManagedBy     = "terraform"
    Scope         = "ephemeral-lane-e"
  }
}

data "aws_caller_identity" "current" {}

data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.lab.identity[0].oidc[0].issuer
}

ephemeral "aws_secretsmanager_secret_version" "db_master_password" {
  secret_id     = var.db_master_password_secret_id
  version_stage = var.db_master_password_secret_version_stage
}

resource "terraform_data" "approval_gate" {
  input = {
    account_id  = var.aws_account_id
    approval_id = var.approval_id
    region      = var.aws_region
    session_id  = var.session_id
  }

  lifecycle {
    precondition {
      condition     = var.apply_approval_phrase == "APPROVED_FOR_EPHEMERAL_APPLY"
      error_message = "Apply is blocked. Keep using backendless init/validate only until the leader records SS0, account, region, and cost approval."
    }
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
      error_message = "The active AWS account does not match aws_account_id."
    }
  }
}

resource "aws_vpc" "lab" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }

  depends_on = [terraform_data.approval_gate]
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id            = aws_vpc.lab.id
  cidr_block        = local.public_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]
  #trivy:ignore:AVD-AWS-0164:exp:2026-10-31 Short-lived no-NAT lab tradeoff. No SSH path is created; EKS public API is /32-scoped and private endpoint is enabled for nodes.
  map_public_ip_on_launch = true

  tags = {
    Name                                         = "${local.name_prefix}-public-${count.index + 1}"
    "kubernetes.io/role/elb"                     = "1"
    "kubernetes.io/cluster/${local.name_prefix}" = "shared"
  }
}

resource "aws_subnet" "private_db" {
  count = 2

  vpc_id                  = aws_vpc.lab.id
  cidr_block              = local.private_db_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-private-db-${count.index + 1}"
  }
}

resource "aws_route_table" "private_db" {
  vpc_id = aws_vpc.lab.id

  tags = {
    Name = "${local.name_prefix}-private-db-local-only"
  }
}

resource "aws_route_table_association" "private_db" {
  count = length(aws_subnet.private_db)

  subnet_id      = aws_subnet.private_db[count.index].id
  route_table_id = aws_route_table.private_db.id
}

resource "aws_db_subnet_group" "mysql" {
  name       = "${local.name_prefix}-mysql"
  subnet_ids = aws_subnet.private_db[*].id

  tags = {
    Name = "${local.name_prefix}-mysql"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.lab.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.lab.id
  }

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_iam_role" "eks_cluster" {
  name = "${local.name_prefix}-eks-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "eks.amazonaws.com"
      }
    }]
  })

  depends_on = [terraform_data.approval_gate]
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

#trivy:ignore:AVD-AWS-0039:exp:2026-10-31 EKS 1.36 uses default envelope encryption for Kubernetes secrets; customer-managed KMS adds region-dependent cost and remains gated.
#trivy:ignore:AVD-AWS-0040:exp:2026-10-31 Public endpoint is restricted to operator_cidr /32 while private endpoint is enabled for node bootstrap.
resource "aws_eks_cluster" "lab" {
  name     = local.name_prefix
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.eks_kubernetes_version

  vpc_config {
    subnet_ids              = aws_subnet.public[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.operator_cidr]
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  upgrade_policy {
    support_type = "STANDARD"
  }

  depends_on = [aws_iam_role_policy_attachment.eks_cluster]

  tags = {
    Name = local.name_prefix
  }
}

resource "aws_iam_role" "eks_node" {
  name = "${local.name_prefix}-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })

  depends_on = [terraform_data.approval_gate]
}

resource "aws_iam_role_policy_attachment" "eks_worker_node" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_cni" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "ecr_readonly" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_eks_node_group" "lab" {
  cluster_name    = aws_eks_cluster.lab.name
  node_group_name = "${local.name_prefix}-managed"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = aws_subnet.public[*].id
  instance_types  = [var.eks_node_instance_type]
  capacity_type   = "ON_DEMAND"
  disk_size       = 20

  scaling_config {
    desired_size = var.eks_node_desired_size
    max_size     = var.eks_node_max_size
    min_size     = var.eks_node_min_size
  }

  update_config {
    max_unavailable = 1
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_worker_node,
    aws_iam_role_policy_attachment.eks_cni,
    aws_iam_role_policy_attachment.ecr_readonly,
  ]

  tags = {
    Name = "${local.name_prefix}-managed"
  }
}

resource "aws_security_group" "db" {
  name        = "${local.name_prefix}-db"
  description = "Allow MySQL only from the default EKS managed-node security group path."
  vpc_id      = aws_vpc.lab.id
  egress      = []

  tags = {
    Name = "${local.name_prefix}-db"
  }

  depends_on = [terraform_data.approval_gate]
}

resource "aws_vpc_security_group_ingress_rule" "db_mysql_from_eks_nodes" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_eks_cluster.lab.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "MySQL from EKS managed nodes using the supported default VPC CNI security group path."
}

resource "aws_security_group" "db_fenced" {
  name        = "${local.name_prefix}-db-fenced"
  description = "No-ingress security group used to fence the old writer before application writes resume."
  vpc_id      = aws_vpc.lab.id
  egress      = []

  tags = {
    Name = "${local.name_prefix}-db-fenced"
  }

  depends_on = [terraform_data.approval_gate]
}

resource "aws_db_instance" "mysql_primary" {
  identifier        = "${local.name_prefix}-mysql-primary"
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true
  engine            = "mysql"
  engine_version    = "8.4"
  instance_class    = "db.t3.small"

  db_subnet_group_name   = aws_db_subnet_group.mysql.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = true
  network_type           = "IPV4"

  username            = var.db_username
  password_wo         = ephemeral.aws_secretsmanager_secret_version.db_master_password.secret_string
  password_wo_version = var.db_password_wo_version

  backup_retention_period  = 1
  copy_tags_to_snapshot    = true
  deletion_protection      = false
  delete_automated_backups = true

  # Ephemeral live-lab cleanup must be able to destroy the primary without
  # leaving a final snapshot that can exceed the approved planning ceiling.
  # New short-lived labs must fail rather than enroll in paid extended support.
  engine_lifecycle_support = "open-source-rds-extended-support-disabled"

  skip_final_snapshot = true

  tags = {
    Name = "${local.name_prefix}-mysql-primary"
  }

  depends_on = [
    aws_eks_node_group.lab,
    aws_route_table_association.private_db,
  ]
}

resource "aws_db_instance" "mysql_reader" {
  engine_lifecycle_support = "open-source-rds-extended-support-disabled"

  identifier = "${local.name_prefix}-mysql-reader"
  # The provider requires the source ARN when a DB subnet group is specified.
  replicate_source_db = aws_db_instance.mysql_primary.arn
  instance_class      = "db.t3.small"
  # Read replicas inherit encryption from their source; state this explicitly so
  # Terraform does not interpret the inherited encryption as drift and replace it.
  storage_encrypted = true

  db_subnet_group_name   = aws_db_subnet_group.mysql.name
  vpc_security_group_ids = [aws_security_group.db.id]
  availability_zone      = var.availability_zones[0]
  publicly_accessible    = false
  multi_az               = false
  network_type           = "IPV4"

  copy_tags_to_snapshot    = true
  deletion_protection      = false
  delete_automated_backups = true

  # Ephemeral live-lab cleanup must be able to destroy the replica without
  # leaving a final snapshot that can exceed the approved planning ceiling.
  skip_final_snapshot = true

  tags = {
    Name = "${local.name_prefix}-mysql-reader"
  }
}

resource "aws_ecr_repository" "app" {
  name                 = "${local.name_prefix}/data-pipeline-app"
  image_tag_mutability = "IMMUTABLE"
  # This repository name includes the unique session ID; Terraform destroy may
  # remove only this session's image layers so cleanup cannot stall on test tags.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name = "${local.name_prefix}-app"
  }

  depends_on = [terraform_data.approval_gate]
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.lab.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]

  tags = {
    Name = "${local.name_prefix}-oidc"
  }
}

resource "aws_iam_policy" "aws_load_balancer_controller" {
  name        = "${local.name_prefix}-aws-load-balancer-controller"
  description = "Scoped bootstrap policy for the AWS Load Balancer Controller in the ephemeral validation cluster."
  policy      = file("${path.module}/policies/aws-load-balancer-controller-policy.json")

  depends_on = [terraform_data.approval_gate]
}

resource "aws_iam_role" "aws_load_balancer_controller" {
  name = "${local.name_prefix}-aws-load-balancer-controller"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Condition = {
        StringEquals = {
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud" = "sts.amazonaws.com"
          "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
        }
      }
    }]
  })

  depends_on = [terraform_data.approval_gate]
}

resource "aws_iam_role_policy_attachment" "aws_load_balancer_controller" {
  role       = aws_iam_role.aws_load_balancer_controller.name
  policy_arn = aws_iam_policy.aws_load_balancer_controller.arn
}

resource "aws_wafv2_web_acl" "lab" {
  name  = "${local.name_prefix}-web-acl"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "custom-header-live-lab-drill"
    priority = 10

    dynamic "action" {
      for_each = var.waf_enforcement_mode == "COUNT" ? [1] : []
      content {
        count {}
      }
    }

    dynamic "action" {
      for_each = var.waf_enforcement_mode == "BLOCK" ? [1] : []
      content {
        block {}
      }
    }

    statement {
      byte_match_statement {
        field_to_match {
          single_header {
            name = "x-live-lab-waf-drill"
          }
        }
        positional_constraint = "EXACTLY"
        search_string         = var.waf_drill_header_value

        text_transformation {
          priority = 0
          type     = "NONE"
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${replace(local.name_prefix, "-", "")}CustomHeaderDrill"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${replace(local.name_prefix, "-", "")}WebAcl"
    sampled_requests_enabled   = true
  }

  depends_on = [terraform_data.approval_gate]
}
