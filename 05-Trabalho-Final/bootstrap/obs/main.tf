# A rede e a fiap-lab da demo 01-Terraform/02-Modules: so LIDA por tag, nunca
# criada, alterada ou destruida por este stack.
data "aws_vpc" "lab" {
  tags = { Name = "fiap-lab" }
}

# us-east-1e nao oferece t3.medium: so entram subnets publicas de AZs que oferecem o tipo.
data "aws_ec2_instance_type_offerings" "obs" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.lab.id]
  }
  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.obs.locations
  }
  tags = { Tier = "Public" }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# Plural de proposito: lista vazia (e nao erro) se o runner ainda nao existe.
data "aws_security_groups" "runner" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.lab.id]
  }
  filter {
    name   = "group-name"
    values = [var.runner_sg_name]
  }
}

locals {
  name = "qc-obs-tf-ai"
  # Primeira subnet em ordem: o plan nao troca de subnet entre execucoes.
  subnet_id = sort(data.aws_subnets.public.ids)[0]
}
