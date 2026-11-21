# Testes nativos do Terraform para o modulo rag-cluster, isolado do raiz.
# Rodam com 'terraform test' a partir da raiz do repositorio (job tf-test):
#
#   terraform workspace select dev || terraform workspace new dev
#   terraform test
#
# command = plan: nada e criado na AWS. O plan ainda le a VPC fiap-lab, a AMI e
# as subnets de verdade (data sources), por isso precisa de credenciais validas.
# As variaveis abaixo sao de mentira de proposito: o teste confere a FORMA do
# modulo (contagem, nomes, porta, health check), nao o bundle nem o coletor.

provider "aws" {
  region = "us-east-1"
}

variables {
  bundle_uri    = "s3://base-config-teste/trabalho-final-ai/releases/teste/app-0000.zip"
  release_id    = "tftest000000"
  otlp_endpoint = "http://10.0.0.10:4318"
}

run "um_no" {
  command = plan

  module {
    source = "./modules/rag-cluster"
  }

  variables {
    ambiente   = "dev"
    node_count = 1
  }

  assert {
    condition     = length(aws_instance.web) == 1
    error_message = "Com node_count = 1 o modulo deveria planejar 1 EC2 (aws_instance.web), mas planejou ${length(aws_instance.web)}. Use count = var.node_count no aws_instance.web."
  }

  assert {
    condition     = length(aws_lb_target_group_attachment.web) == 1
    error_message = "Com node_count = 1 deveria existir 1 aws_lb_target_group_attachment.web. O count do attachment deve ser length(aws_instance.web)."
  }

  assert {
    condition     = aws_instance.web[0].tags["Name"] == "qc-rag-dev-001"
    error_message = "A tag Name da EC2 deveria ser qc-rag-dev-001, mas e ${aws_instance.web[0].tags["Name"]}. Use format(\"qc-rag-%s-%03d\", var.ambiente, count.index + 1)."
  }

  assert {
    condition     = aws_lb_target_group.web.port == 8080 && aws_lb_target_group.web.health_check[0].path == "/health"
    error_message = "O aws_lb_target_group.web precisa de port = 8080 e health_check { path = \"/health\" }: e nessa porta e nesse caminho que o assistente responde."
  }

  assert {
    condition     = aws_lb.web.name == "qc-rag-alb-dev" && aws_lb_target_group.web.name == "qc-rag-tg-dev" && aws_security_group.web.name == "qc-rag-dev"
    error_message = "Nomes esperados com ambiente = dev: ALB qc-rag-alb-dev, TG qc-rag-tg-dev, SG qc-rag-dev. Use var.ambiente nos names."
  }

  assert {
    condition     = strcontains(aws_instance.web[0].user_data, "qc-rag-kit-user-data") && strcontains(aws_instance.web[0].user_data, "http://10.0.0.10:4318")
    error_message = "O user_data da EC2 nao e o template do kit com o otlp_endpoint: use templatefile(\"$${path.module}/../../templates/user_data.sh.tftpl\", { ambiente, release_id, bundle_uri, otlp_endpoint, aws_region })."
  }
}

run "dois_nos" {
  command = plan

  module {
    source = "./modules/rag-cluster"
  }

  variables {
    ambiente   = "prod"
    node_count = 2
  }

  assert {
    condition     = length(aws_instance.web) == 2
    error_message = "Com node_count = 2 o modulo deveria planejar 2 EC2, mas planejou ${length(aws_instance.web)}. Use count = var.node_count."
  }

  assert {
    condition     = length(aws_lb_target_group_attachment.web) == 2
    error_message = "Com node_count = 2 deveriam existir 2 aws_lb_target_group_attachment.web. Use count = length(aws_instance.web)."
  }

  assert {
    condition     = [for i in aws_instance.web : i.tags["Name"]] == ["qc-rag-prod-001", "qc-rag-prod-002"]
    error_message = "As tags Name deveriam ser qc-rag-prod-001 e qc-rag-prod-002. Use format(\"qc-rag-%s-%03d\", var.ambiente, count.index + 1)."
  }

  assert {
    condition     = aws_lb.web.name == "qc-rag-alb-prod"
    error_message = "Com ambiente = prod o ALB deveria se chamar qc-rag-alb-prod, mas se chama ${aws_lb.web.name}."
  }
}
