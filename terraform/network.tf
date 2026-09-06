data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "ha-compact-vpc" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "ha-igw" }
}

resource "aws_subnet" "public_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone = data.aws_availability_zones.available.names[0]
  # Public subnets host the internet-facing ALB/NAT gateways, not arbitrary
  # public EC2 addresses.
  map_public_ip_on_launch = false
  tags = { Name = "sbn-pub-a"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = false
  tags = { Name = "sbn-pub-b"
  "kubernetes.io/role/elb" = "1" }
}

resource "aws_subnet" "app_private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 6, 1)
  availability_zone = data.aws_availability_zones.available.names[0]
  tags = { Name = "sbn-app-a"
    "kubernetes.io/role/internal-elb"             = "1"
    "karpenter.sh/discovery"                      = "data-engineer-cluster"
    "kubernetes.io/cluster/data-engineer-cluster" = "shared" # ALB 및 기타 AWS 컨트롤러 연동을 위한 명시적 태그 
  }
}

resource "aws_subnet" "app_private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 6, 2)
  availability_zone = data.aws_availability_zones.available.names[1]
  tags = { Name = "sbn-app-b"
    "kubernetes.io/role/internal-elb" = "1"
    "karpenter.sh/discovery"          = "data-engineer-cluster"
  "kubernetes.io/cluster/data-engineer-cluster" = "shared" }

}

resource "aws_subnet" "db_private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 12)
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "sbn-db-a" }
}

resource "aws_subnet" "db_private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 13)
  availability_zone = data.aws_availability_zones.available.names[1]
  tags              = { Name = "sbn-db-b" }
}
