terraform {
  backend "s3" {
    # O bucket (base-config-<RM>) chega pelo make bootstrap via
    # -backend-config="bucket=...": cada aluno tem o seu e este arquivo nao muda.
    # A key e propria do Trabalho Final de AI-Devops-CICD para nunca ler nem destruir o state do
    # runner do Modulo 02 (key gitlab-runner-fleet) nem o de outra disciplina (trabalho-final/runner.tfstate), que podem existir na mesma conta.
    key    = "trabalho-final-ai/runner.tfstate"
    region = "us-east-1"
  }
}
