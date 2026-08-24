resource "aws_iam_role" "bastion" {
  name = "CommandServerRole-v2"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}
# [수정] AdministratorAccess 삭제 및 최소 권한 정책 연결
# 1. SSM을 통한 접속 권한 (필수)
resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
# 2. EKS 관리 및 리소스 조회 권한 (Bastion에서 kubectl 사용을 위해 필요)
resource "aws_iam_role_policy_attachment" "bastion_eks_read" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# 3. EC2 리소스 조회 권한 (Karpenter 노드 확인 등)
resource "aws_iam_role_policy_attachment" "bastion_ec2_read" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess"
}
resource "aws_iam_role_policy" "bastion_eks_describe" {
  name = "BastionEKSDescribePolicy"
  role = aws_iam_role.bastion.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListClusters"
        ]
        Resource = "*" # 특정 클러스터 ARN으로 제한하는 것이 더 안전합니다.
      }
    ]
  })
}
resource "aws_iam_instance_profile" "bastion" {
  name = "CommandServerInstanceProfile-v2"
  role = aws_iam_role.bastion.name
}

data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "bastion" {
  ami                    = data.aws_ssm_parameter.ami.value
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.command_server.id]
  iam_instance_profile   = aws_iam_instance_profile.bastion.name
  # 퍼블릭 IP 할당 명시 (이미 서브넷 설정에 되어있지만 가독성을 위해 추가)
  associate_public_ip_address = true
  tags                        = { Name = "bastion-host" }
}

resource "aws_db_subnet_group" "main" {
  name       = "data-pipeline-db-subnet-group"
  subnet_ids = [aws_subnet.db_private_a.id, aws_subnet.db_private_b.id]
}

resource "aws_db_instance" "primary" {
  identifier              = "data-pipeline-primary"
  engine                  = "mysql"
  engine_version          = "8.0"
  instance_class          = "db.t3.micro"
  allocated_storage       = 20
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  username                = "admin"
  password                = var.db_password
  backup_retention_period = 1
  multi_az                = false
  publicly_accessible     = false
  skip_final_snapshot     = true
  availability_zone       = data.aws_availability_zones.available.names[0]
}

resource "aws_db_instance" "replica" {
  identifier             = "data-pipeline-replica"
  replicate_source_db    = aws_db_instance.primary.identifier
  instance_class         = "db.t3.micro"
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  skip_final_snapshot    = true
  availability_zone      = data.aws_availability_zones.available.names[1]
}

