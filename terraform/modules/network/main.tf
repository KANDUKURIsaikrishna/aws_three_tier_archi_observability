# VPC
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "bookstore-vpc" }
}

# Public subnets
# nosemgrep: terraform.aws.security.aws-subnet-has-public-ip-address.aws-subnet-has-public-ip-address
resource "aws_subnet" "public" {
  count                   = length(var.public_subnets)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnets[count.index].cidr
  availability_zone       = var.public_subnets[count.index].az
  map_public_ip_on_launch = true
  tags                    = { Name = "public-subnet-${count.index + 1}" }
}

# Private subnets
resource "aws_subnet" "private" {
  count             = length(var.private_subnets)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnets[count.index].cidr
  availability_zone = var.private_subnets[count.index].az
  tags              = { Name = "private-subnet-${count.index + 1}" }
}

# Internet Gateway
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "bookstore-igw" }
}

# NAT Gateways.
#   var.single_nat_gateway = true  -> 1 NAT for the whole VPC (single-AZ SPOF)
#   var.single_nat_gateway = false -> 1 NAT per AZ (default; survives an AZ loss)
# NAT is a managed service and does not draw on the EC2 vCPU quota, so per-AZ
# is available even while that quota is capped. The S3 gateway endpoint below
# still keeps S3/ECR-layer traffic off the NAT(s) either way.
locals {
  nat_azs   = distinct([for s in var.public_subnets : s.az])
  nat_count = var.single_nat_gateway ? 1 : length(local.nat_azs)
}

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = { Name = "bookstore-nat-${count.index + 1}" }
}

resource "aws_nat_gateway" "nat" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  # One NAT per public subnet (= per AZ) when multi; all in public[0] when single.
  subnet_id  = aws_subnet.public[count.index].id
  tags       = { Name = "bookstore-nat-${count.index + 1}" }
  depends_on = [aws_internet_gateway.igw]
}

# Public Route Table
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "bookstore-public-rt" }
}

# Private Route Tables — one per NAT Gateway. With per-AZ NAT that means one
# route table per AZ, each pointing at the NAT in its own AZ so a private
# subnet's egress never crosses an AZ boundary (and an AZ failure only takes
# out that AZ's egress, not everyone's).
resource "aws_route_table" "private" {
  count  = local.nat_count
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat[count.index].id
  }
  tags = { Name = "bookstore-private-rt-${count.index + 1}" }
}

data "aws_region" "current" {}

# S3 Gateway VPC Endpoint — free (no hourly/data charge), routes S3 traffic
# (ECR image layers are stored in S3, plus Terraform state reads) off the
# NAT Gateway(s) instead of paying the per-GB data-processing fee for it.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat([aws_route_table.public.id], aws_route_table.private[*].id)
  tags              = { Name = "bookstore-s3-endpoint" }
}

# Route Table Associations
resource "aws_route_table_association" "public" {
  count          = length(var.public_subnets)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count     = length(var.private_subnets)
  subnet_id = aws_subnet.private[count.index].id
  # Single NAT: every private subnet -> the one private route table.
  # Per-AZ NAT: private subnet -> the route table for its own AZ.
  route_table_id = var.single_nat_gateway ? aws_route_table.private[0].id : aws_route_table.private[index(local.nat_azs, var.private_subnets[count.index].az)].id
}
