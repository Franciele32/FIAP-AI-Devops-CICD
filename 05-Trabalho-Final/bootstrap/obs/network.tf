# SG da observabilidade do Trabalho Final:
#   4318 (OTLP HTTP)          -> do CIDR da VPC fiap-lab (motivo abaixo)
#   9090/3100/16686 (consulta) -> so do SG do runner (os testes do kit consultam daqui)
#   3000 (Grafana)            -> internet, com login obrigatorio e anonimo negado
# Sem SSH e sem key pair: administracao so via SSM.
resource "aws_security_group" "obs" {
  name        = local.name
  description = "Observabilidade do Trabalho Final: OTLP da VPC, consultas do runner, Grafana com login"
  vpc_id      = data.aws_vpc.lab.id
  tags        = { Name = local.name }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.obs.id
  description       = "Saida para docker pull, S3 e SSM"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# Por que o CIDR da VPC e nao o SG qc-rag-<env>: esses SGs sao criados DEPOIS,
# pelo pipeline do aluno, e somem e voltam a cada destroy. Uma regra por SG
# exigiria rodar este apply de novo depois de cada deploy. O CIDR da fiap-lab
# cobre as EC2 qc-rag-* de dev e prod e o runner, e nada de fora da VPC chega aqui.
resource "aws_vpc_security_group_ingress_rule" "otlp_vpc" {
  security_group_id = aws_security_group.obs.id
  description       = "OTLP HTTP (4318) de dentro da VPC fiap-lab (apps qc-rag-* e runner)"
  ip_protocol       = "tcp"
  from_port         = 4318
  to_port           = 4318
  cidr_ipv4         = data.aws_vpc.lab.cidr_block
}

locals {
  portas_consulta = { prometheus = 9090, loki = 3100, jaeger = 16686 }
  # Cada par (porta, SG do runner) vira uma regra; sem runner, nenhuma.
  consulta = {
    for par in setproduct(keys(local.portas_consulta), data.aws_security_groups.runner.ids) :
    "${par[0]}-${par[1]}" => { porta = local.portas_consulta[par[0]], sg = par[1], nome = par[0] }
  }
}

resource "aws_vpc_security_group_ingress_rule" "consulta_runner" {
  for_each                     = local.consulta
  security_group_id            = aws_security_group.obs.id
  description                  = "Consulta ${each.value.nome} (${each.value.porta}) so do runner do Trabalho Final"
  ip_protocol                  = "tcp"
  from_port                    = each.value.porta
  to_port                      = each.value.porta
  referenced_security_group_id = each.value.sg
}

# O aluno de negocio precisa de um link pronto, sem tunel SSM: o Grafana fica
# publico na 3000, mas com login obrigatorio, anonimo desligado e sem cadastro.
resource "aws_vpc_security_group_ingress_rule" "grafana" {
  for_each          = toset(var.grafana_cidrs)
  security_group_id = aws_security_group.obs.id
  description       = "Grafana (3000) com login obrigatorio"
  ip_protocol       = "tcp"
  from_port         = 3000
  to_port           = 3000
  cidr_ipv4         = each.value
}
