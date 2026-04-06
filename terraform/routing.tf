resource "aws_eip" "nat_a" { domain = "vpc" }
resource "aws_eip" "nat_b" { domain = "vpc" }

resource "aws_nat_gateway" "nat_a" {
  allocation_id = aws_eip.nat_a.id
  subnet_id     = aws_subnet.public_a.id
  tags          = { Name = "nat-gw-a" }
}

resource "aws_nat_gateway" "nat_b" {
  allocation_id = aws_eip.nat_b.id
  subnet_id     = aws_subnet.public_b.id
  tags          = { Name = "nat-gw-b" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "rtb-public" }
}

resource "aws_route_table_association" "pub_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "pub_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "app_a" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_a.id
  }
  tags = { Name = "rtb-app-a" }
}

resource "aws_route_table_association" "app_a" {
  subnet_id      = aws_subnet.app_private_a.id
  route_table_id = aws_route_table.app_a.id
}

resource "aws_route_table" "app_b" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat_b.id
  }
  tags = { Name = "rtb-app-b" }
}

resource "aws_route_table_association" "app_b" {
  subnet_id      = aws_subnet.app_private_b.id
  route_table_id = aws_route_table.app_b.id
}

resource "aws_route_table" "db_a" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "rtb-db-a" }
}

resource "aws_route_table_association" "db_a" {
  subnet_id      = aws_subnet.db_private_a.id
  route_table_id = aws_route_table.db_a.id
}

resource "aws_route_table" "db_b" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "rtb-db-b" }
}

resource "aws_route_table_association" "db_b" {
  subnet_id      = aws_subnet.db_private_b.id
  route_table_id = aws_route_table.db_b.id
}
