#!/usr/bin/env bash
# make links [nome]: links já montados, para ninguém trocar <seu-usuario> à mão
# nem catar IP e DNS no meio do log.
#   sem nome: stdout = "nome  url", um por linha; stderr = o que ainda não existe;
#   com nome: stdout = só a URL desse link; rc != 0 se o nome não existe ou o
#             recurso ainda não foi criado.
# make grafana-senha (links.sh --senha-grafana): imprime a senha do admin do Grafana.
# Só leitura: não cria nem altera nada.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export AWS_PAGER=""

NOMES="projeto pipelines runners grafana assistente-dev assistente-prod"

if [ "${1:-}" = "--senha-grafana" ]; then
  aws_check >/dev/null
  aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_GRAFANA" --with-decryption \
    --query Parameter.Value --output text 2>/dev/null || \
    die "a senha do Grafana não existe no SSM ($SSM_GRAFANA)." "Rode primeiro: make bootstrap"
  exit 0
fi

[ $# -le 1 ] || die "uso: make links [nome]   (um nome por vez: $NOMES)"
WANT="${1:-}"
if [ -n "$WANT" ]; then
  case " $NOMES " in
    *" $WANT "*) ;;
    *) die "link desconhecido: $WANT." "Disponíveis: $NOMES" ;;
  esac
fi
want() { [ -z "$WANT" ] || [ "$WANT" = "$1" ]; }
FOUND=""
emit() {
  if [ -z "$WANT" ]; then printf '%-16s %s\n' "$1" "$2"
  elif [ "$1" = "$WANT" ]; then FOUND="$2"; fi
}

if want projeto || want pipelines || want runners; then
  load_env
  require_repo
  require_token
  resolve_project_path
  emit projeto   "$PROJECT_WEB_URL"
  emit pipelines "$PROJECT_WEB_URL/-/pipelines"
  emit runners   "$PROJECT_WEB_URL/-/settings/ci_cd#js-runners-settings"
fi

if want grafana || want assistente-dev || want assistente-prod; then
  aws_check >/dev/null
fi

if want grafana; then
  ip="$(aws ec2 describe-instances --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=$OBS_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null || true)"
  if [ -n "$ip" ] && [ "$ip" != "None" ]; then
    # uid fixo do dashboard provisionado em bootstrap/obs/stack/grafana/dashboards/.
    emit grafana "http://$ip:3000/d/qc-tf-assistente"
  else
    log "links: a EC2 $OBS_NAME não está no ar (rode make bootstrap)."
  fi
fi

for env in dev prod; do
  want "assistente-$env" || continue
  bucket="$(find_bucket)"
  dns="$(aws s3 cp "s3://$bucket/env:/$env/$STATE_KEY_ALUNO" - 2>/dev/null | \
    jq -r '.outputs.alb_dns.value // empty' 2>/dev/null || true)"
  if [ -n "$dns" ]; then
    emit "assistente-$env" "http://$dns/health"
  else
    log "links: o assistente de $env ainda não existe (o deploy-$env do pipeline ainda não rodou)."
  fi
done

if [ -n "$WANT" ]; then
  [ -n "$FOUND" ] || exit 1
  printf '%s\n' "$FOUND"
fi
