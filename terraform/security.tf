resource "aws_security_group" "command_server" {
  name   = "command-server-sg"
  vpc_id = aws_vpc.main.id
  # SSM is the default operator access path. SSH is opt-in and must be
  # restricted to an explicitly supplied administrator CIDR.
  dynamic "ingress" {
    for_each = var.allowed_ssh_location == null ? [] : [var.allowed_ssh_location]
    content {
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }
  egress = []
}

resource "aws_security_group" "alb" {
  name   = "alb-sg"
  vpc_id = aws_vpc.main.id
  egress = []
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name   = "app-node-sg"
  vpc_id = aws_vpc.main.id
  egress = []
  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port       = 22
    to_port         = 22
    protocol        = "tcp"
    security_groups = [aws_security_group.command_server.id]
  }
}

resource "aws_security_group_rule" "alb_to_app" {
  type                     = "egress"
  security_group_id        = aws_security_group.alb.id
  source_security_group_id = aws_security_group.app.id
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
}

resource "aws_security_group_rule" "command_to_endpoints" {
  type                     = "egress"
  security_group_id        = aws_security_group.command_server.id
  source_security_group_id = aws_security_group.vpc_endpoints.id
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
}

resource "aws_security_group_rule" "command_to_dns_udp" {
  type              = "egress"
  security_group_id = aws_security_group.command_server.id
  cidr_blocks       = [aws_vpc.main.cidr_block]
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
}

resource "aws_security_group_rule" "command_to_dns_tcp" {
  type              = "egress"
  security_group_id = aws_security_group.command_server.id
  cidr_blocks       = [aws_vpc.main.cidr_block]
  from_port         = 53
  to_port           = 53
  protocol          = "tcp"
}

resource "aws_security_group_rule" "app_to_https" {
  type                     = "egress"
  security_group_id        = aws_security_group.app.id
  source_security_group_id = aws_security_group.vpc_endpoints.id
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
}

resource "aws_security_group_rule" "app_to_dns_udp" {
  type              = "egress"
  security_group_id = aws_security_group.app.id
  cidr_blocks       = [aws_vpc.main.cidr_block]
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
}

resource "aws_security_group_rule" "app_to_dns_tcp" {
  type              = "egress"
  security_group_id = aws_security_group.app.id
  cidr_blocks       = [aws_vpc.main.cidr_block]
  from_port         = 53
  to_port           = 53
  protocol          = "tcp"
}

resource "aws_security_group_rule" "app_to_rds" {
  type                     = "egress"
  security_group_id        = aws_security_group.app.id
  source_security_group_id = aws_security_group.rds.id
  from_port                = 3306
  to_port                  = 3306
  protocol                 = "tcp"
}

resource "aws_security_group" "vpc_endpoints" {
  name   = "vpc-endpoints-sg"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port = 443
    to_port   = 443
    protocol  = "tcp"
    security_groups = [
      aws_security_group.app.id,
      aws_security_group.command_server.id,
      aws_eks_cluster.main.vpc_config[0].cluster_security_group_id,
    ]
  }
}

resource "aws_security_group" "rds" {
  name   = "rds-isolated-sg"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port = 3306
    to_port   = 3306
    protocol  = "tcp"
    # EKS managed nodes use the cluster security group, not the legacy
    # app-node-sg. Keep the RDS private and allow only the in-VPC workload SGs.
    security_groups = [
      aws_security_group.app.id,
      aws_eks_cluster.main.vpc_config[0].cluster_security_group_id,
    ]
  }
}


resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.app_a.id, aws_route_table.app_b.id]
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app_private_a.id, aws_subnet.app_private_b.id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}

locals {
  # Keep operator, ECR, and token traffic inside the VPC so workload security
  # groups do not need unrestricted internet egress. These endpoints are part
  # of the explicitly approved validation profile and add hourly cost while
  # the stack is running.
  interface_endpoint_services = toset([
    "ec2messages",
    "ssm",
    "ssmmessages",
    "ecr.api",
    "ecr.dkr",
    "sts",
  ])
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoint_services

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app_private_a.id, aws_subnet.app_private_b.id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
}
