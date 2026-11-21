#!/usr/bin/env bash
# make status: o que está de pé do Trabalho Final. Runner online no GitLab?, URL
# do projeto, observabilidade no ar (Grafana /api/health), status do último
# pipeline e DNS do ALB de dev e prod (lidos do state no S3).
# Só leitura: não cria nem altera nada.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd aws terraform jq curl
load_env
require_repo
require_token
gitlab_whoami
resolve_project_path

# Alinha pelo número de caracteres (o printf %-Ns conta bytes e desalinha o "Ú").
linha() { printf '  %s%*s %s\n' "$1" $((16 - ${#1})) '' "$2"; }

printf 'Trabalho Final AI-Devops-CICD - status\n'
if ! gitlab_find_project; then
  linha "Projeto" "$PROJECT_WEB_URL (não existe; rode make bootstrap)"
  exit 0
fi
linha "Projeto" "$PROJECT_WEB_URL (id $PROJECT_ID)"

gitlab_api GET "/projects/$PROJECT_ID/runners?type=project_type&per_page=100"
runner="$(printf '%s' "$GL_BODY" | jq -r --arg d "$RUNNER_NAME" \
  '[.[] | select(.description == $d)] | first | if . == null then "" else "\(.id) \(.status)" end' 2>/dev/null || true)"
if [ -n "$runner" ]; then
  linha "Runner" "$RUNNER_NAME (id ${runner%% *}): ${runner#* }"
else
  linha "Runner" "$RUNNER_NAME não existe no projeto (rode make bootstrap)"
fi

gitlab_api GET "/projects/$PROJECT_ID/pipelines?per_page=1"
pipe="$(printf '%s' "$GL_BODY" | jq -r 'first | if . == null then "" else "#\(.id) \(.status) (\(.ref), \(.created_at[0:16] | sub("T"; " "))) \(.web_url)" end' 2>/dev/null || true)"
linha "Último pipeline" "${pipe:-nenhum pipeline ainda (faça o primeiro push com o .gitlab-ci.yml)}"

# Observabilidade e ALBs vêm da AWS; consultar não exige nada além da credencial.
if aws sts get-caller-identity >/dev/null 2>&1; then
  obs="$(aws ec2 describe-instances --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=$OBS_NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].[InstanceId,State.Name,PublicIpAddress]' --output text 2>/dev/null || true)"
  if [ -z "$obs" ] || [ "$obs" = "None" ]; then
    linha "Observabilidade" "$OBS_NAME não existe (rode make bootstrap)"
  else
    read -r obs_id obs_estado obs_ip <<<"$obs"
    if [ "$obs_estado" != "running" ]; then
      linha "Observabilidade" "$OBS_NAME ($obs_id) $obs_estado (rode make bootstrap para religar)"
    else
      # /api/health não exige login: só diz se o Grafana está de pé.
      hc="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "http://$obs_ip:3000/api/health" || true)"
      if [ "$hc" = 200 ]; then
        linha "Observabilidade" "$OBS_NAME ($obs_id) no ar; Grafana http://$obs_ip:3000 (health 200)"
      else
        linha "Observabilidade" "$OBS_NAME ($obs_id) running, mas o Grafana não respondeu (HTTP ${hc:-000}); rode make bootstrap"
      fi
    fi
  fi
  linha "OTLP (SSM)" "$(ssm_get "$SSM_OTLP" | sed 's/^$/(sem parâmetro; rode make bootstrap)/')"

  bucket="$(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null | tr '\t' '\n' | grep '^base-config' | head -1 || true)"
  for env in dev prod; do
    dns=""
    if [ -n "$bucket" ]; then
      dns="$(aws s3 cp "s3://$bucket/env:/$env/$STATE_KEY_ALUNO" - 2>/dev/null | \
        jq -r '.outputs.alb_dns.value // empty' 2>/dev/null || true)"
    fi
    if [ -n "$dns" ]; then
      linha "ALB $env" "http://$dns"
    else
      linha "ALB $env" "sem state ou sem output alb_dns (o deploy-$env ainda não rodou)"
    fi
  done
else
  linha "AWS" "credenciais AWS inválidas ou expiradas (aws sts get-caller-identity)"
fi
