resource "aws_iam_role" "bastion" {
  name = "CommandServerRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "bastion_admin" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  name = "CommandServerInstanceProfile"
  role = aws_iam_role.bastion.name
}

data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "bastion" {
  ami                  = data.aws_ssm_parameter.ami.value
  instance_type        = "t3.micro"
  subnet_id            = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.command_server.id]
  iam_instance_profile = aws_iam_instance_profile.bastion.name
  tags                 = { Name = "bastion-host" }
}

resource "aws_db_subnet_group" "main" {
  name       = "data-pipeline-db-subnet-group"
  subnet_ids = [aws_subnet.db_private_a.id, aws_subnet.db_private_b.id]
}

resource "aws_db_instance" "primary" {
  identifier             = "data-pipeline-primary"
  engine                 = "mysql"
  engine_version         = "8.0"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  username               = "admin"
  password               = "SuperSecretPassword123!" 
  backup_retention_period = 1
  multi_az               = false
  publicly_accessible    = false
  skip_final_snapshot    = true 
  availability_zone      = data.aws_availability_zones.available.names[0]
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


