variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "config_bucket" {
  type        = string
  description = "Bucket base-config-<RM>. A EC2 baixa a configuracao do compose de s3://<bucket>/trabalho-final-ai/obs-config/."
}

variable "instance_type" {
  type = string
  # t3.medium (4 GiB): medido na Aula 5, a stack (Grafana ~740 MiB com paineis
  # abertos + Loki, Collector, Prometheus e Jaeger) usa ~1,2 GiB; t3.small nao cabe.
  default = "t3.medium"
}

variable "runner_sg_name" {
  type        = string
  description = "SG do runner do Trabalho Final, unica origem das consultas (9090, 3100, 16686)."
  default     = "qc-runner-tf-ai"
}

variable "grafana_cidrs" {
  type        = list(string)
  description = "Quem abre o Grafana (3000). Aberto com login obrigatorio: o IP do Codespaces muda a cada sessao."
  default     = ["0.0.0.0/0"]
}
