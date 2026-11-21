terraform {
  backend "s3" {
    # O bucket (base-config-<RM>) chega pelo make bootstrap via -backend-config.
    # Key propria: destruir a observabilidade nunca toca o state do runner, o dos
    # ambientes do aluno nem o da Aula 5 (quantum-assistant/obs/terraform.tfstate).
    key          = "trabalho-final-ai/obs.tfstate"
    region       = "us-east-1"
    use_lockfile = true
  }
}
