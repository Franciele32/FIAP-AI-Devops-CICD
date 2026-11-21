#!/bin/bash
# Bootstrap da EC2 qc-obs-tf-ai (roda como root, no host). Chamado pelo user_data
# no primeiro boot e pelo make bootstrap (via SSM) a cada execucao. Idempotente:
# so recria o conteiner cuja configuracao mudou.
#   1. instala Docker + compose plugin em versoes pinadas (se faltarem);
#   2. baixa do S3 a configuracao do compose;
#   3. gera obs.env (IP publico atual) e grafana.env (senha do admin lida do SSM, chmod 600);
#   4. sobe o compose e espera o Grafana responder.
set -euo pipefail
exec > >(tee -a /var/log/qc-obs-bootstrap.log) 2>&1
echo "[bootstrap] inicio $(date -u +%FT%TZ)"

# shellcheck disable=SC1091
source /opt/qc-obs/instance.env
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"
STACK=/opt/qc-obs/stack

# Mesmas versoes da Aula 5 (repositorio oficial da Docker para Ubuntu 22.04).
DOCKER_VERSION="5:29.9.3-1~ubuntu.22.04~jammy"
CONTAINERD_VERSION="2.4.2-4~ubuntu.22.04~jammy"
COMPOSE_VERSION="5.7.1-1~ubuntu.22.04~jammy"
BUILDX_VERSION="0.39.0-1~ubuntu.22.04~jammy"

if ! docker compose version >/dev/null 2>&1; then
  echo "[bootstrap] instalando Docker $DOCKER_VERSION"
  export DEBIAN_FRONTEND=noninteractive
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu jammy stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q "docker-ce=$DOCKER_VERSION" "docker-ce-cli=$DOCKER_VERSION" \
    "containerd.io=$CONTAINERD_VERSION" "docker-compose-plugin=$COMPOSE_VERSION" \
    "docker-buildx-plugin=$BUILDX_VERSION"
  # Sem isto um `apt upgrade` futuro trocaria a versao no meio do trabalho.
  apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin
  systemctl enable --now docker
fi

echo "[bootstrap] sincronizando s3://$QC_CONFIG_BUCKET/$QC_CONFIG_PREFIX/"
mkdir -p "$STACK"
# --exact-timestamps: sem isso o sync ignora arquivo de mesmo tamanho com data diferente.
aws s3 sync --only-show-errors --delete --exact-timestamps "s3://$QC_CONFIG_BUCKET/$QC_CONFIG_PREFIX/" "$STACK/"

# IP publico muda quando o Academy para e liga a EC2: o root_url e refeito a cada boot.
tok=$(curl -fsS -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
pub=$(curl -fsS -H "X-aws-ec2-metadata-token: $tok" http://169.254.169.254/latest/meta-data/public-ipv4 || true)
umask 022
echo "GF_SERVER_ROOT_URL=http://${pub:-localhost}:3000/" > /opt/qc-obs/obs.env

# Senha do admin: SecureString criada pelo make bootstrap. Vai para um arquivo
# 600 do root, nunca para o log nem para o S3.
umask 077
pw=$(aws ssm get-parameter --name /fiap/trabalho-final-ai/grafana-admin --with-decryption \
  --query Parameter.Value --output text)
printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$pw" > /opt/qc-obs/grafana.env.new
unset pw
chmod 600 /opt/qc-obs/grafana.env.new
# Só conta como "mudou" se já havia uma senha antes (na 1ª subida o env basta).
senha_mudou=0
if [ -f /opt/qc-obs/grafana.env ] && ! cmp -s /opt/qc-obs/grafana.env.new /opt/qc-obs/grafana.env; then
  senha_mudou=1
fi
mv /opt/qc-obs/grafana.env.new /opt/qc-obs/grafana.env
umask 022

cd "$STACK"
docker compose up -d --remove-orphans

# Arquivo montado (bind mount) fica preso ao inode antigo quando o sync troca o
# arquivo: reinicia so o servico cuja configuracao mudou desde o ultimo bootstrap.
declare -A cfg=([otel-collector]="otel-collector.yaml" [prometheus]="prometheus.yml"
                [jaeger]="jaeger.yaml" [loki]="loki.yaml"
                [grafana]="grafana/provisioning/datasources/datasources.yaml grafana/provisioning/dashboards/trabalho-final.yaml grafana/dashboards/trabalho-final-assistente.json /opt/qc-obs/obs.env")
mkdir -p /opt/qc-obs/state
for svc in "${!cfg[@]}"; do
  # shellcheck disable=SC2086
  sum=$(cat ${cfg[$svc]} 2>/dev/null | sha256sum | cut -c1-16)
  old=$(cat "/opt/qc-obs/state/$svc.sha" 2>/dev/null || true)
  if [ -n "$old" ] && [ "$sum" != "$old" ]; then
    echo "[bootstrap] configuracao de $svc mudou: reiniciando"
    docker compose restart "$svc" || true
  fi
  echo "$sum" > "/opt/qc-obs/state/$svc.sha"
done
# A senha do admin so e aplicada pelo env na PRIMEIRA subida do Grafana (fica no
# banco dele). Se o SSM mudou, o grafana-cli troca a senha no banco.
if [ "$senha_mudou" = 1 ] && docker compose ps --status running grafana | grep -q grafana; then
  sleep 5
  # shellcheck disable=SC1091
  . /opt/qc-obs/grafana.env
  # Pelo stdin, e não no argv: o argv aparece no 'ps' do host para qualquer usuário.
  printf '%s\n' "$GF_SECURITY_ADMIN_PASSWORD" | \
    docker compose exec -T grafana grafana cli admin reset-admin-password --password-from-stdin >/dev/null 2>&1 || true
  unset GF_SECURITY_ADMIN_PASSWORD
fi

for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
    echo "[bootstrap] grafana ok"
    break
  fi
  sleep 3
done
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
echo "[bootstrap] fim $(date -u +%FT%TZ)"
