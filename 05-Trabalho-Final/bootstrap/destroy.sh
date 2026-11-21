#!/usr/bin/env bash
# make destroy / destroy-ambientes / destroy-runner / destroy-obs.
#
#   ambientes: no clone do aluno, terraform destroy nos workspaces dev e prod.
#   runner:    terraform destroy da EC2 do runner (bootstrap/runner), apaga o runner
#              no GitLab e o parâmetro SSM com o token.
#   obs:       terraform destroy da EC2 de observabilidade (bootstrap/obs) e os
#              parâmetros SSM otlp-endpoint, obs-query e grafana-admin.
#   tudo:      ambientes, depois obs, depois runner.
#
# Só destrói o que o próprio make criou, pelos states trabalho-final-ai/* e pelos
# parâmetros /fiap/trabalho-final-ai/*. NUNCA toca: a rede fiap-lab (vpc-call /
# RT-call), o bucket base-config-*, o projeto GitLab, a chave SSH, o clone, o
# Trabalho Final de outra disciplina (qc-runner-trabalho-final, trabalho-final/*)
# nem a Aula 5 (/qc-assistant/*).
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

destroy_ambientes() {
  local clone env
  need_cmd terraform aws
  load_env
  clone="$(clone_dir_offline)"
  require_clone "$clone"
  aws_check >/dev/null
  log "Destruindo os ambientes dev e prod do seu projeto ($clone)..."
  tf_init_clone "$clone"
  for env in dev prod; do
    if ! tf_workspace_exists "$clone" "$env"; then
      ok "workspace $env não existe; nada a destruir"
      continue
    fi
    log "terraform destroy no workspace $env..."
    terraform -chdir="$clone" workspace select "$env" >/dev/null
    terraform -chdir="$clone" destroy -auto-approve -input=false >&2 || \
      die "o terraform destroy do workspace $env falhou." \
        "Veja o erro acima e rode de novo: make destroy-ambientes" \
        "(se for 'Error acquiring the state lock' de um pipeline cancelado: terraform -chdir=$clone force-unlock -force <ID>, com o ID que aparece no erro)"
    ok "$env destruído"
  done
  terraform -chdir="$clone" workspace select default >/dev/null 2>&1 || true
}

destroy_runner() {
  local bucket desc runner_id instance_id
  need_cmd terraform aws jq curl
  load_env
  aws_check >/dev/null
  bucket="$(find_bucket)"

  log "Destruindo a EC2 do runner $RUNNER_NAME (bootstrap/runner, state s3://$bucket/$STATE_PREFIX/runner.tfstate)..."
  # As regras de consulta (9090/3100/16686) do SG da observabilidade apontam para
  # o SG do runner; com elas de pé, a AWS recusa apagar o SG do runner
  # (DependencyViolation). Saem antes; o próximo make bootstrap recria.
  obs_tf_init "$bucket"
  if [ "$(obs_state_count)" -gt 0 ]; then
    tf_stack "$OBS_TF_DIR" obs destroy -auto-approve -input=false -var "config_bucket=$bucket" \
      -target=aws_vpc_security_group_ingress_rule.consulta_runner >&2 || \
      die "não consegui remover as regras do SG da observabilidade que apontam para o runner." "Rode de novo: make destroy-runner"
  fi
  runner_tf_init "$bucket"
  instance_id="$(runner_instance_id)"
  # Pelo state, e não só pelo instance_id: um apply que falhou no meio pode ter
  # deixado só o Security Group.
  if [ "$(runner_state_count)" -gt 0 ]; then
    tf_stack "$RUNNER_TF_DIR" runner destroy -auto-approve -input=false >&2 || \
      die "o terraform destroy da EC2 do runner falhou." "Veja o erro acima e rode de novo: make destroy-runner"
    ok "EC2 ${instance_id:-(não havia)} e Security Group $RUNNER_NAME destruídos"
  else
    ok "state do runner vazio; nada a destruir na AWS"
  fi

  # O runner no GitLab: id guardado na descrição do parâmetro SSM; sem ele, busca
  # pela descrição no projeto (precisa do token do GitLab).
  desc="$(ssm_token_description)"
  runner_id="$(printf '%s' "$desc" | sed -n 's/.*runner_id=\([0-9][0-9]*\).*/\1/p')"
  if [ -n "$GITLAB_TOKEN" ]; then
    if [ -z "$runner_id" ] && [ -n "$REPO" ]; then
      gitlab_whoami
      resolve_project_path
      if gitlab_find_project; then
        runner_id="$(gitlab_find_runner)"
      fi
    fi
    if [ -n "$runner_id" ]; then
      gitlab_api DELETE "/runners/$runner_id"
      case "$GL_STATUS" in
        204) ok "runner $runner_id apagado no GitLab" ;;
        404) ok "runner $runner_id já não existia no GitLab" ;;
        *) warn "não consegui apagar o runner $runner_id no GitLab (HTTP $GL_STATUS): $(gl_err). Apague na UI: Settings > CI/CD > Runners." ;;
      esac
    else
      ok "nenhum runner $RUNNER_NAME no GitLab"
    fi
  else
    warn "sem token do GitLab salvo: o runner ${runner_id:-$RUNNER_NAME} não foi apagado no GitLab. Apague na UI: Settings > CI/CD > Runners."
  fi

  if [ -n "$desc" ] || aws ssm get-parameter --name "$RUNNER_TOKEN_SSM" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws ssm delete-parameter --name "$RUNNER_TOKEN_SSM" --region "$AWS_REGION" >/dev/null || \
      die "não consegui apagar o parâmetro SSM $RUNNER_TOKEN_SSM." "Apague com: aws ssm delete-parameter --name $RUNNER_TOKEN_SSM --region $AWS_REGION"
    ok "parâmetro SSM $RUNNER_TOKEN_SSM apagado"
  else
    ok "parâmetro SSM $RUNNER_TOKEN_SSM não existia"
  fi
}

destroy_obs() {
  local bucket p
  need_cmd terraform aws jq
  aws_check >/dev/null
  bucket="$(find_bucket)"
  log "Destruindo a observabilidade $OBS_NAME (bootstrap/obs, state s3://$bucket/$STATE_PREFIX/obs.tfstate)..."
  obs_tf_init "$bucket"
  if [ "$(obs_state_count)" -gt 0 ]; then
    tf_stack "$OBS_TF_DIR" obs destroy -auto-approve -input=false -var "config_bucket=$bucket" >&2 || \
      die "o terraform destroy da observabilidade falhou." "Veja o erro acima e rode de novo: make destroy-obs"
    ok "EC2 e Security Group $OBS_NAME destruídos"
  else
    ok "state da observabilidade vazio; nada a destruir na AWS"
  fi
  for p in "$SSM_OTLP" "$SSM_OBS_QUERY" "$SSM_GRAFANA"; do
    if ssm_exists "$p"; then
      aws ssm delete-parameter --region "$AWS_REGION" --name "$p" >/dev/null && ok "parâmetro SSM $p apagado"
    fi
  done
  # Só a pasta de configuração deste trabalho no bucket; o bucket fica.
  aws s3 rm --only-show-errors --recursive "s3://$bucket/$OBS_CONFIG_PREFIX/" >&2 || true
  ok "configuração s3://$bucket/$OBS_CONFIG_PREFIX/ removida (o bucket fica)"
}

# Os ambientes do aluno só existem com clone; sem clone (bootstrap pela metade),
# segue para o runner e a observabilidade em vez de parar.
destroy_ambientes_se_houver_clone() {
  local clone
  load_env
  if [ -z "${CLONE_DIR:-}" ]; then require_repo; parse_repo "$REPO"; fi
  clone="$(clone_dir)"
  if [ -d "$clone/.git" ]; then
    destroy_ambientes
  else
    warn "clone $clone não existe; pulei o destroy dos ambientes dev e prod."
  fi
}

case "${1:-}" in
  ambientes) destroy_ambientes ;;
  runner)    destroy_runner ;;
  obs)       destroy_obs ;;
  tudo)      destroy_ambientes_se_houver_clone; destroy_obs; destroy_runner ;;
  *) die "uso: destroy.sh ambientes|runner|obs|tudo" ;;
esac
log "Pronto. A rede $NET_NAME, o bucket, o projeto GitLab e o seu clone foram mantidos."
