# Trabalho Final - AI-Devops-CICD (Quantum Commerce)
# Wrapper fino: a lógica vive em bootstrap/*.sh. Rode a partir desta pasta:
#   cd /workspaces/FIAP-AI-Devops-CICD/05-Trabalho-Final && make help

SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help bootstrap status links validar smoke kit grafana-senha destroy destroy-ambientes destroy-runner destroy-obs

# REPO, GITLAB_URL, CLONE_DIR e AMBIENTE chegam aos scripts pelo ambiente.
# O token do GitLab NÃO é variável do make: o bootstrap pergunta sem eco (ou lê
# do stdin) e guarda em ~/.trabalho-final-ai.env (chmod 600).
export REPO GITLAB_URL CLONE_DIR AMBIENTE

help: ## Lista os comandos disponíveis
	@printf 'Trabalho Final AI-Devops-CICD - comandos (rode nesta pasta, no Codespaces)\n\n'
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\nPrimeira vez:\n  make bootstrap REPO=trabalho-final-ai\n'
	@printf '  (o token do GitLab, Legacy com escopo api, é pedido sem aparecer na tela e fica salvo em ~/.trabalho-final-ai.env)\n'

bootstrap: ## Cria ou reaproveita projeto GitLab, runner, observabilidade (Grafana) e clone com o kit (idempotente)
	@bash bootstrap/bootstrap.sh

status: ## Projeto, runner online?, observabilidade no ar?, último pipeline e DNS do ALB de dev e prod
	@bash bootstrap/status.sh

# LINK_NAMES vem do bloco no fim do arquivo (make links grafana).
links: ## Links prontos (projeto, pipelines, runners, grafana, assistente-dev, assistente-prod); com um nome, só ele
	@bash bootstrap/links.sh $(LINK_NAMES)

grafana-senha: ## Mostra a senha do admin do Grafana (só no seu terminal; não cole em print nem em chat)
	@bash bootstrap/links.sh --senha-grafana

validar: ## No clone: fmt, validate, terraform test, gate RAG, plan dev e prod + test_contrato.py, sem apply
	@bash bootstrap/validar.sh validar

smoke: ## Roda test_smoke.py contra dev e prod já no ar (AMBIENTE=dev ou prod para só um)
	@bash bootstrap/validar.sh smoke

kit: ## (Re)copia o kit para o clone e envia (commit + push) só os arquivos do kit; os seus ficam intactos
	@bash bootstrap/bootstrap.sh kit

destroy: ## Tudo deste trabalho: ambientes dev e prod, runner e observabilidade (a rede fiap-lab e o bucket ficam)
	@bash bootstrap/destroy.sh tudo

destroy-ambientes: ## No clone: terraform destroy nos workspaces dev e prod
	@bash bootstrap/destroy.sh ambientes

destroy-runner: ## Destrói a EC2 do runner, apaga o runner no GitLab e o token no SSM
	@bash bootstrap/destroy.sh runner

destroy-obs: ## Destrói a EC2 de observabilidade e os parâmetros SSM dela
	@bash bootstrap/destroy.sh obs

# `make links grafana` é mais fácil de digitar que `make links N=grafana`, mas o
# make tomaria "grafana" por um alvo. Só quando links é o primeiro alvo, cada
# palavra depois dele vira um alvo vazio e segue como argumento do script. Fica no
# fim do arquivo para o alvo vazio ganhar de um alvo real de mesmo nome: `make
# links destroy` não pode destruir nada.
ifeq (links,$(firstword $(MAKECMDGOALS)))
LINK_NAMES := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
$(foreach n,$(LINK_NAMES),$(eval $(n):;@:))
else
# `make destroy ambientes` (com espaço) rodaria o destroy de TUDO e só depois
# reclamaria de "ambientes". O $(error) para o make na leitura do Makefile,
# antes de qualquer receita, inclusive com make -n.
DESTROY_AVISO := use o alvo com hífen: make destroy-ambientes | destroy-runner | destroy-obs; make destroy sozinho apaga TUDO
ifneq (,$(filter destroy,$(MAKECMDGOALS)))
ifneq (1,$(words $(MAKECMDGOALS)))
$(error $(DESTROY_AVISO))
endif
endif
endif
