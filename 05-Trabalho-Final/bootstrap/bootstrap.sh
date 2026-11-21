#!/usr/bin/env bash
# make bootstrap: deixa o projeto GitLab do aluno conectado a um GitLab Runner
# próprio na AWS e a uma stack de observabilidade (OTel Collector, Prometheus,
# Loki, Jaeger e Grafana), a partir só do REPO e de um Personal Access Token
# Legacy com escopo api.
#
# IDEMPOTENTE E RESUMÍVEL: cada etapa confere o que já existe e pula. Rodar duas
# vezes seguidas não cria nada novo; se uma etapa falhou, a próxima execução
# continua dali. Nada aqui destrói a rede fiap-lab.
#
# Uso (via make, a partir de 05-Trabalho-Final/):
#   make bootstrap REPO=trabalho-final-ai      # o token é pedido sem eco
#   make bootstrap REPO=trabalho-final-ai < arquivo-com-o-token   # sem terminal
#   bash bootstrap/bootstrap.sh kit            # só (re)copia o kit para o clone
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

KIT_COMMIT_MSG="chore: kit de validacao do trabalho final"
RUNNER_CRIADO=0
INSTANCE_NOVA=0
ETAPAS=11
T0=$(date +%s)

# --- 1. Preflight -------------------------------------------------------------
etapa_preflight() {
  log "1/$ETAPAS Preflight: ferramentas, AWS, rede $NET_NAME e token do GitLab..."
  need_cmd aws terraform jq curl git ssh-keygen ssh-keyscan python3 openssl
  load_env
  require_repo
  parse_repo "$REPO"

  # Credencial AWS primeiro: no Academy ela expira a cada 4 h, e é o erro mais comum.
  local conta
  conta="$(aws_check)"
  obter_token
  ok "AWS conta $conta"
  BUCKET="$(find_bucket)"
  ok "bucket $BUCKET"

  # Só leitura aqui: a rede é criada (se faltar) depois de validar o token, para
  # um token errado não deixar nada criado pela metade.
  NET_VPCS="$(aws ec2 describe-vpcs --region "$AWS_REGION" --filters "Name=tag:Name,Values=$NET_NAME" \
    --query 'length(Vpcs)' --output text)" || die "não consegui consultar as VPCs da conta." \
    "Confira as credenciais AWS: aws sts get-caller-identity"
  case "$NET_VPCS" in
    0) ok "rede $NET_NAME ausente (será criada a seguir)" ;;
    1) ok "rede $NET_NAME encontrada" ;;
    *) die "existem $NET_VPCS VPCs com a tag Name=$NET_NAME; o Terraform exige exatamente uma." \
         "Liste com: aws ec2 describe-vpcs --filters Name=tag:Name,Values=$NET_NAME --query 'Vpcs[].VpcId'" \
         "e apague a duplicada no console da AWS (VPC > Your VPCs) antes de rodar de novo." ;;
  esac

  gitlab_whoami
  gitlab_check_scope
  ok "token GitLab $(mask "$GITLAB_TOKEN") de @$GITLAB_USERNAME com escopo api"
  resolve_project_path
  save_env
  ok "REPO e token salvos em $ENV_FILE (chmod 600)"
}

# --- 2. Rede ----------------------------------------------------------------------
# Só cria o que falta: a VPC (vpc-call) e a rota para o Internet Gateway (RT-call).
# Sem a rota, a EC2 do runner não alcança o SSM nem o gitlab.com.
etapa_rede() {
  log "2/$ETAPAS Rede $NET_NAME..."
  if [ "$NET_VPCS" = 0 ]; then
    log "    criando a VPC (01-Terraform/demos/02-Modules/vpc-call, state local)..."
    { terraform -chdir="$NET_VPC_DIR" init -input=false >&2 && \
      terraform -chdir="$NET_VPC_DIR" apply -auto-approve -input=false >&2; } || \
      die "não consegui criar a VPC $NET_NAME." "Veja o erro acima e rode de novo: make bootstrap"
  fi
  local vpc_id gateways
  vpc_id="$(aws ec2 describe-vpcs --region "$AWS_REGION" --filters "Name=tag:Name,Values=$NET_NAME" \
    --query 'Vpcs[0].VpcId' --output text)"
  gateways="$(aws ec2 describe-route-tables --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query "RouteTables[].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId[]" --output text)"
  if ! printf '%s' "$gateways" | grep -q 'igw-'; then
    log "    criando a rota para a internet (01-Terraform/demos/02-Modules/RT-call, state local)..."
    { terraform -chdir="$NET_RT_DIR" init -input=false >&2 && \
      terraform -chdir="$NET_RT_DIR" apply -auto-approve -input=false >&2; } || \
      die "não consegui criar a route table to-igw-$NET_NAME." "Veja o erro acima e rode de novo: make bootstrap"
  fi
  ok "rede $NET_NAME pronta ($vpc_id, com rota para a internet)"
}

# --- 3. Tooling do Codespaces -------------------------------------------------
etapa_tooling() {
  log "3/$ETAPAS Ferramentas do Ansible via SSM no Codespaces..."
  if command -v session-manager-plugin >/dev/null 2>&1; then
    ok "session-manager-plugin já instalado"
  else
    local pacote tmp
    case "$(uname -m)" in
      x86_64)        pacote="ubuntu_64bit" ;;
      aarch64|arm64) pacote="ubuntu_arm64" ;;
      *) die "arquitetura $(uname -m) sem pacote do session-manager-plugin." \
           "Rode o bootstrap no Codespaces (x86_64)." ;;
    esac
    tmp="$(mktemp -d)"
    curl -fsSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/$pacote/session-manager-plugin.deb" \
      -o "$tmp/session-manager-plugin.deb" || die "não consegui baixar o session-manager-plugin ($pacote)." \
      "Confira a internet do Codespaces e rode de novo: make bootstrap"
    sudo dpkg -i "$tmp/session-manager-plugin.deb" >/dev/null || \
      die "não consegui instalar o session-manager-plugin." "Rode de novo: make bootstrap"
    rm -rf "$tmp"
    ok "session-manager-plugin instalado ($pacote)"
  fi

  # Ansible e boto3 precisam estar no MESMO Python para o plugin aws_ssm funcionar;
  # o devcontainer já prepara ~/venv assim, aqui só conferimos.
  if [ -x "$HOME/venv/bin/ansible-playbook" ] && "$HOME/venv/bin/python" -c 'import boto3, botocore' 2>/dev/null && \
     "$HOME/venv/bin/ansible-galaxy" collection list --format json 2>/dev/null | \
       jq -e '[.[] | keys[]] | (index("community.aws") != null) and (index("amazon.aws") != null)' >/dev/null; then
    ok "venv ~/venv com ansible, boto3 e collections"
  else
    log "    preparando ~/venv (ansible, boto3, community.aws, amazon.aws)..."
    [ -d "$HOME/venv" ] || python3 -m venv "$HOME/venv"
    { "$HOME/venv/bin/pip" install --quiet ansible boto3 botocore >&2 && \
      "$HOME/venv/bin/ansible-galaxy" collection install community.aws amazon.aws >&2; } || \
      die "não consegui preparar o ~/venv do Ansible." "Rode de novo: make bootstrap"
    ok "venv ~/venv pronto"
  fi
}

# --- 4. Chave SSH ---------------------------------------------------------------
gerar_chave_ssh() {
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  if [ -f "$SSH_KEY" ] && [ -f "$SSH_KEY.pub" ]; then
    ok "chave $SSH_KEY já existe"
  else
    ssh-keygen -q -t ed25519 -N '' -C "qc-trabalho-final-ai-codespaces" -f "$SSH_KEY" </dev/null || \
      die "não consegui gerar a chave SSH $SSH_KEY."
    ok "chave $SSH_KEY gerada"
  fi
}

# Compara pelo material da chave (tipo + base64), ignorando o comentário: é o que
# o GitLab usa para dizer que duas chaves são a mesma.
cadastrar_chave_gitlab() {
  local local_key body
  local_key="$(awk '{print $1" "$2}' "$SSH_KEY.pub")"
  gitlab_api GET "/user/keys?per_page=100"
  [ "$GL_STATUS" = 200 ] || die "não consegui listar suas chaves SSH no GitLab (HTTP $GL_STATUS): $(gl_err)"
  if printf '%s' "$GL_BODY" | jq -e --arg k "$local_key" \
      'any(.[]; (.key | split(" ") | .[0:2] | join(" ")) == $k)' >/dev/null; then
    ok "chave pública já cadastrada no GitLab"
    return 0
  fi
  body="$(jq -n --arg t "qc-trabalho-final-ai ($(hostname))" --rawfile k "$SSH_KEY.pub" \
    '{title:$t, key:($k|rtrimstr("\n"))}')"
  gitlab_api POST /user/keys "$body"
  [ "$GL_STATUS" = 201 ] || die "o GitLab recusou a chave SSH (HTTP $GL_STATUS): $(gl_err)" \
    "Se a mensagem fala em 'fingerprint has already been taken', esta chave está em outra conta GitLab:" \
    "apague-a lá ou gere outra com: rm -f $SSH_KEY $SSH_KEY.pub && make bootstrap"
  ok "chave pública cadastrada no GitLab (User Settings > SSH Keys)"
}

configurar_ssh() {
  local cfg="$HOME/.ssh/config" marca="# >>> trabalho-final-ai (make bootstrap)" chaves
  touch "$cfg"
  chmod 600 "$cfg"
  # Um bloco Host igual (deste trabalho ou de outro que usa a mesma chave) basta.
  if grep -qF "$marca" "$cfg" || grep -qF "IdentityFile $SSH_KEY" "$cfg"; then
    ok "$HOME/.ssh/config já aponta $GITLAB_SSH_HOST para $SSH_KEY"
  else
    {
      printf '\n%s\n' "$marca"
      printf 'Host %s\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$GITLAB_SSH_HOST" "$SSH_KEY"
      printf '# <<< trabalho-final-ai\n'
    } >> "$cfg"
    ok "$HOME/.ssh/config: Host $GITLAB_SSH_HOST usa $SSH_KEY (sem ssh-agent)"
  fi
  touch "$HOME/.ssh/known_hosts"
  if ssh-keygen -F "$GITLAB_SSH_HOST" -f "$HOME/.ssh/known_hosts" >/dev/null 2>&1; then
    ok "$GITLAB_SSH_HOST já está em ~/.ssh/known_hosts"
  else
    chaves="$(ssh-keyscan -T 10 "$GITLAB_SSH_HOST" 2>/dev/null || true)"
    [ -n "$chaves" ] || die "não consegui ler a chave de host de $GITLAB_SSH_HOST (ssh-keyscan)." \
      "Confira a internet do Codespaces e rode de novo: make bootstrap"
    printf '%s\n' "$chaves" >> "$HOME/.ssh/known_hosts"
    ok "$GITLAB_SSH_HOST adicionado a ~/.ssh/known_hosts"
  fi
}

etapa_chave_ssh() {
  log "4/$ETAPAS Chave SSH para clone e push sem senha..."
  gerar_chave_ssh
  cadastrar_chave_gitlab
  configurar_ssh
}

# --- 5. Projeto ---------------------------------------------------------------
criar_projeto() {
  local ns_id="" body
  if [ "${PROJECT_NS,,}" != "${GITLAB_USERNAME,,}" ]; then
    gitlab_api GET "/namespaces/$(urlencode "$PROJECT_NS")"
    [ "$GL_STATUS" = 200 ] || die "o namespace '$PROJECT_NS' não existe ou o seu token não tem acesso a ele." \
      "Use o seu usuário: make bootstrap REPO=git@$GITLAB_SSH_HOST:$GITLAB_USERNAME/$PROJECT_NAME.git"
    ns_id="$(printf '%s' "$GL_BODY" | jq -r '.id')"
  fi
  body="$(jq -n --arg n "$PROJECT_NAME" --arg ns "$ns_id" \
    '{name:$n, path:$n, visibility:"public", initialize_with_readme:false}
     + (if $ns == "" then {} else {namespace_id:($ns|tonumber)} end)')"
  gitlab_api POST /projects "$body"
  [ "$GL_STATUS" = 201 ] || die "o GitLab não criou o projeto $PROJECT_PATH (HTTP $GL_STATUS): $(gl_err)" \
    "Se o nome já está em uso (por exemplo, projeto apagado há pouco), escolha outro: make bootstrap REPO=<outro-nome>"
  PROJECT_ID="$(printf '%s' "$GL_BODY" | jq -r '.id')"
  ok "projeto $PROJECT_PATH criado (id $PROJECT_ID, público, vazio)"
}

etapa_projeto() {
  log "5/$ETAPAS Projeto GitLab $PROJECT_PATH..."
  if gitlab_find_project; then
    ok "projeto já existe (id $PROJECT_ID)"
  else
    criar_projeto
  fi
  # Com os instance runners ligados, os jobs podem cair nos runners SaaS do
  # GitLab, que não têm credencial AWS, e o pipeline quebra no terraform init.
  if [ "$(printf '%s' "$GL_BODY" | jq -r '.shared_runners_enabled')" = "false" ]; then
    ok "instance runners já desligados"
  else
    gitlab_api PUT "/projects/$PROJECT_ID" '{"shared_runners_enabled":false}'
    [ "$GL_STATUS" = 200 ] || die "não consegui desligar os instance runners do projeto (HTTP $GL_STATUS): $(gl_err)" \
      "Desligue na UI: Settings > CI/CD > Runners > Instance runners (desmarcar) e rode de novo: make bootstrap"
    ok "instance runners desligados (os jobs só rodam no runner da AWS)"
  fi
}

# --- 6. Runner no GitLab + token no SSM ---------------------------------------
gitlab_runner_ids() {
  gitlab_api GET "/projects/$PROJECT_ID/runners?type=project_type&per_page=100"
  [ "$GL_STATUS" = 200 ] || die "falha ao listar os runners do projeto (HTTP $GL_STATUS): $(gl_err)"
  printf '%s' "$GL_BODY" | jq -r --arg d "$RUNNER_NAME" '.[] | select(.description == $d) | .id'
}

etapa_runner() {
  log "6/$ETAPAS Runner $RUNNER_NAME no GitLab e token no SSM ($RUNNER_TOKEN_SSM)..."
  local ids desc ssm_rid ssm_pid id body token
  ids="$(gitlab_runner_ids)"
  desc="$(ssm_token_description)"
  ssm_rid="$(printf '%s' "$desc" | sed -n 's/.*runner_id=\([0-9][0-9]*\).*/\1/p')"
  ssm_pid="$(printf '%s' "$desc" | sed -n 's/.*project_id=\([0-9][0-9]*\).*/\1/p')"
  # O SSM e a EC2 têm nome fixo: um runner por conta AWS. Trocar de projeto deixa
  # os pipelines do projeto antigo em 'pending', sem erro nenhum na tela dele.
  if [ -n "$ssm_pid" ] && [ "$ssm_pid" != "$PROJECT_ID" ]; then
    warn "o runner $RUNNER_NAME está ligado ao projeto id $ssm_pid; ele passa agora para $PROJECT_PATH (id $PROJECT_ID). Pipelines do projeto antigo ficam 'pending' (um runner por conta AWS)."
  fi

  # O token glrt- só é mostrado na criação. Se o SSM guarda o token deste mesmo
  # runner (runner_id na descrição do parâmetro), reaproveita; senão o runner que
  # existe não tem token utilizável e é trocado por um novo.
  if [ -n "$ssm_rid" ] && printf '%s\n' "$ids" | grep -qx "$ssm_rid"; then
    RUNNER_ID="$ssm_rid"
    ok "runner já existe (id $RUNNER_ID) e o token dele está no SSM"
    return 0
  fi
  for id in $ids; do
    gitlab_api DELETE "/runners/$id"
    case "$GL_STATUS" in
      204|404) log "    runner antigo $id (sem token no SSM) removido" ;;
      *) die "não consegui remover o runner antigo $id (HTTP $GL_STATUS): $(gl_err)" ;;
    esac
  done

  body="$(jq -n --argjson p "$PROJECT_ID" --arg d "$RUNNER_NAME" \
    '{runner_type:"project_type", project_id:$p, description:$d, tag_list:["shell","terraform"], run_untagged:true}')"
  gitlab_api POST /user/runners "$body"
  [ "$GL_STATUS" = 201 ] || die "o GitLab não criou o runner (HTTP $GL_STATUS): $(gl_err)" \
    "Confira se o token tem escopo 'api' e se você é Maintainer/Owner do projeto $PROJECT_PATH."
  RUNNER_ID="$(printf '%s' "$GL_BODY" | jq -r '.id')"
  token="$(printf '%s' "$GL_BODY" | jq -r '.token')"
  GL_BODY=""
  ssm_put_secret "$RUNNER_TOKEN_SSM" "$token" \
    "runner_id=$RUNNER_ID project_id=$PROJECT_ID - token do $RUNNER_NAME (make bootstrap do Trabalho Final)"
  token=""
  RUNNER_CRIADO=1
  ok "runner criado (id $RUNNER_ID, tags shell,terraform) e token glrt-**** gravado no SSM"
}

# --- 7. EC2 do runner -------------------------------------------------------------
etapa_ec2() {
  log "7/$ETAPAS EC2 $RUNNER_NAME (Terraform em bootstrap/runner, state s3://$BUCKET/$STATE_PREFIX/runner.tfstate)..."
  local antes
  runner_tf_init "$BUCKET"
  antes="$(runner_instance_id)"
  tf_stack "$RUNNER_TF_DIR" runner apply -auto-approve -input=false -compact-warnings >&2 || \
    die "o terraform apply da EC2 do runner falhou." \
      "Veja o erro acima e rode de novo: make bootstrap (o que já foi criado é reaproveitado)"
  INSTANCE_ID="$(runner_instance_id)"
  [ -n "$INSTANCE_ID" ] || die "o Terraform não devolveu o instance_id do runner." \
    "Confira com: TF_DATA_DIR=$TF_CACHE_DIR/tf-runner terraform -chdir=$RUNNER_TF_DIR output"
  [ "$antes" = "$INSTANCE_ID" ] || INSTANCE_NOVA=1

  # O Terraform não religa uma EC2 parada (o AWS Academy para as instâncias quando
  # a sessão do lab termina).
  ec2_ligar_se_parada "$INSTANCE_ID" "$RUNNER_NAME"
  ok "EC2 $INSTANCE_ID no ar"
}

esperar_ssm_online() { esperar_ssm "$INSTANCE_ID"; }

runner_status() {
  gitlab_api GET "/runners/$RUNNER_ID"
  if [ "$GL_STATUS" = 200 ]; then
    printf '%s' "$GL_BODY" | jq -r '.status // "desconhecido"'
  else
    printf 'desconhecido'
  fi
}

# --- 9. Configuração com o playbook do Módulo 02 -----------------------------
rodar_playbook() {
  local hosts rc=0
  hosts="$(mktemp)"
  cat > "$hosts" <<EOF
[runner]
$INSTANCE_ID

[all:vars]
ansible_connection=community.aws.aws_ssm
ansible_aws_ssm_bucket_name=$BUCKET
ansible_aws_ssm_region=$AWS_REGION
ansible_become=true
EOF
  ( cd "$ANSIBLE_DIR" && "$HOME/venv/bin/ansible-playbook" -i "$hosts" \
      --extra-vars "gitlab_runner_name=$RUNNER_NAME gitlab_runner_token_ssm_path=$RUNNER_TOKEN_SSM" \
      play.yaml >&2 ) || rc=$?
  rm -f "$hosts"
  [ "$rc" = 0 ] || die "o playbook que configura o runner falhou." \
    "Veja a task que falhou acima e rode de novo: make bootstrap (o playbook é idempotente)"
  ok "playbook aplicado"
}

etapa_ansible() {
  log "9/$ETAPAS Configuração da EC2 como GitLab Runner (Ansible via SSM) e venv do kit..."
  # Online com o token atual na mesma EC2 = já configurado. Runner novo (token
  # novo no SSM) ou EC2 nova exigem o playbook.
  if [ "$RUNNER_CRIADO" = 0 ] && [ "$INSTANCE_NOVA" = 0 ] && [ "$(runner_status)" = "online" ]; then
    ok "runner já registrado e online; o playbook não precisa rodar"
    return 0
  fi
  esperar_ssm_online
  rodar_playbook
}

# O /opt/venv do playbook traz Checkov, pytest e boto3; os testes do kit também
# importam requests e yaml. Confere e completa sem tocar no playbook do Módulo 02.
etapa_venv_runner() {
  local f out
  esperar_ssm "$INSTANCE_ID"
  f="$(mktemp)"
  cat > "$f" <<EOS
set -eu
if $RUNNER_VENV/bin/python -c 'import pytest, boto3, requests, yaml' 2>/dev/null; then echo venv=ok; exit 0; fi
$RUNNER_VENV/bin/pip install --quiet $KIT_PIP
$RUNNER_VENV/bin/python -c 'import pytest, boto3, requests, yaml' && echo venv=instalado
EOS
  out="$(ssm_run "$INSTANCE_ID" 600 "$f")" || { rm -f "$f"; die "não consegui completar o venv $RUNNER_VENV do runner ($KIT_PIP)." "Rode de novo: make bootstrap"; }
  rm -f "$f"
  case "$out" in
    *venv=ok*)        ok "venv $RUNNER_VENV do runner já tem pytest, boto3, requests e pyyaml" ;;
    *venv=instalado*) ok "venv $RUNNER_VENV do runner completado ($KIT_PIP)" ;;
    *) die "o venv $RUNNER_VENV do runner não ficou pronto (saída: $out)." "Rode de novo: make bootstrap" ;;
  esac
}

etapa_esperar_online() {
  local i st=""
  for i in $(seq 1 30); do
    st="$(runner_status)"
    if [ "$st" = "online" ]; then
      ok "runner $RUNNER_NAME online no GitLab"
      return 0
    fi
    [ "$i" = 1 ] && log "    aguardando o runner ficar online no GitLab (até 5 min)..."
    sleep 10
  done
  die "o runner $RUNNER_ID não ficou online no GitLab (status: $st)." \
    "Confira em $PROJECT_WEB_URL/-/settings/ci_cd (Runners) e rode de novo: make bootstrap"
}

# --- 8. Observabilidade (EC2 qc-obs-tf-ai) ----------------------------------
# Cria a stack ANTES de qualquer plan do aluno: o raiz dele lê o SSM otlp-endpoint.
# A EC2 sobe aqui e instala o Docker em segundo plano (user_data); a etapa 10
# só confere se ficou pronta, enquanto a 9 configura o runner.
etapa_obs() {
  log "8/$ETAPAS Observabilidade $OBS_NAME (Terraform em bootstrap/obs, state s3://$BUCKET/$STATE_PREFIX/obs.tfstate)..."
  local senha antes otlp query
  # Senha do Grafana: gerada uma única vez, direto no SSM, nunca impressa.
  if ssm_exists "$SSM_GRAFANA"; then
    ok "senha do Grafana já existe no SSM ($SSM_GRAFANA)"
  else
    senha="$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 24)"
    ssm_put_secret "$SSM_GRAFANA" "$senha" "Senha do usuario admin do Grafana $OBS_NAME (make bootstrap do Trabalho Final AI)"
    senha=""
    ok "senha do Grafana gerada e gravada no SSM ($SSM_GRAFANA, SecureString)"
  fi

  # Configuração do compose no S3 (a EC2 baixa de lá). sync só envia o que mudou.
  aws s3 sync --only-show-errors --delete --exclude '.DS_Store' \
    "$OBS_STACK_DIR/" "s3://$BUCKET/$OBS_CONFIG_PREFIX/" >&2 || \
    die "não consegui enviar a configuração da observabilidade para s3://$BUCKET/$OBS_CONFIG_PREFIX/." \
      "Confira as credenciais AWS (aws sts get-caller-identity) e rode de novo: make bootstrap"

  obs_tf_init "$BUCKET"
  antes="$(obs_output instance_id)"
  tf_stack "$OBS_TF_DIR" obs apply -auto-approve -input=false -compact-warnings \
    -var "config_bucket=$BUCKET" >&2 || \
    die "o terraform apply da observabilidade falhou." \
      "Veja o erro acima e rode de novo: make bootstrap (o que já foi criado é reaproveitado)"
  OBS_ID="$(obs_output instance_id)"
  [ -n "$OBS_ID" ] || die "o Terraform não devolveu o instance_id da observabilidade." \
    "Confira com: TF_DATA_DIR=$TF_CACHE_DIR/tf-obs terraform -chdir=$OBS_TF_DIR output"
  OBS_NOVA=0
  [ "$antes" = "$OBS_ID" ] || OBS_NOVA=1
  ec2_ligar_se_parada "$OBS_ID" "$OBS_NAME"

  # Só grava quando o valor muda: a 2ª execução não cria versão nova no SSM.
  otlp="$(obs_output otlp_endpoint)"
  query="$(obs_output obs_query)"
  if [ "$(ssm_get "$SSM_OTLP")" != "$otlp" ]; then
    ssm_put_string "$SSM_OTLP" "$otlp" "OTLP HTTP do $OBS_NAME (make bootstrap do Trabalho Final AI)"
    ok "SSM $SSM_OTLP = $otlp"
  else
    ok "SSM $SSM_OTLP já aponta para $otlp"
  fi
  if [ "$(ssm_get "$SSM_OBS_QUERY")" != "$query" ]; then
    ssm_put_string "$SSM_OBS_QUERY" "$query" "Consultas Prometheus/Loki/Jaeger do $OBS_NAME (so o runner alcanca)"
    ok "SSM $SSM_OBS_QUERY gravado"
  else
    ok "SSM $SSM_OBS_QUERY já atualizado"
  fi
  ok "EC2 $OBS_ID no ar (a stack termina de subir em segundo plano)"
}

# --- 10. Observabilidade pronta + prova de ponta a ponta ---------------------
# Roda o bootstrap da EC2 de novo via SSM (aplica configuração nova e o IP
# público atual) e confere, DO RUNNER, que um span OTLP chega até o Jaeger:
# é o mesmo caminho que o app qc-rag e os testes do kit vão usar.
etapa_obs_pronta() {
  log "10/$ETAPAS Observabilidade pronta e prova OTLP -> Jaeger a partir do runner..."
  local f out ip trace span agora
  esperar_ssm "$OBS_ID"
  f="$(mktemp)"
  cat > "$f" <<EOS
set -eu
cloud-init status --wait >/dev/null 2>&1 || true
test -f /opt/qc-obs/instance.env
aws s3 cp --only-show-errors --region $AWS_REGION s3://$BUCKET/$OBS_CONFIG_PREFIX/bootstrap.sh /opt/qc-obs/bootstrap.sh
bash /opt/qc-obs/bootstrap.sh >/dev/null 2>&1 || { tail -30 /var/log/qc-obs-bootstrap.log; exit 1; }
curl -fsS -o /dev/null -w 'grafana_health=%{http_code}\n' http://127.0.0.1:3000/api/health
EOS
  [ "$OBS_NOVA" = 1 ] && log "    primeira subida: Docker + docker pull das 5 imagens (~4 a 6 min)..."
  out="$(ssm_run "$OBS_ID" 1200 "$f")" || { rm -f "$f"; printf '%s\n' "$out" >&2; die "a stack de observabilidade não subiu na EC2 $OBS_ID." \
    "Veja o log na EC2: aws ssm start-session --target $OBS_ID  e depois: sudo tail -50 /var/log/qc-obs-bootstrap.log" \
    "Depois rode de novo: make bootstrap"; }
  rm -f "$f"
  printf '%s\n' "$out" | grep -q 'grafana_health=200' || die "o Grafana não respondeu /api/health na EC2 $OBS_ID." \
    "Rode de novo: make bootstrap"
  ok "OTel Collector, Prometheus, Loki, Jaeger e Grafana no ar"

  ip="$(obs_output private_ip)"
  trace="$(openssl rand -hex 16)"
  span="$(openssl rand -hex 8)"
  agora="$(date +%s)"
  f="$(mktemp)"
  cat > "$f" <<EOS
set -eu
cat > /tmp/qc-span.json <<'JSON'
{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"qc-bootstrap-check"}},{"key":"deployment.environment","value":{"stringValue":"bootstrap"}}]},"scopeSpans":[{"scope":{"name":"make-bootstrap"},"spans":[{"traceId":"$trace","spanId":"$span","name":"bootstrap-otlp-check","kind":1,"startTimeUnixNano":"${agora}000000000","endTimeUnixNano":"${agora}500000000"}]}]}]}
JSON
curl -fsS -o /dev/null -w 'otlp=%{http_code}\n' -H 'Content-Type: application/json' --data-binary @/tmp/qc-span.json http://$ip:4318/v1/traces
for i in \$(seq 1 30); do
  if curl -fsS http://$ip:16686/api/traces/$trace 2>/dev/null | grep -q "$span"; then echo jaeger=ok; exit 0; fi
  sleep 2
done
echo jaeger=ausente; exit 1
EOS
  out="$(ssm_run "$INSTANCE_ID" 120 "$f")" || true
  rm -f "$f"
  printf '%s\n' "$out" | grep -q 'jaeger=ok' || die "o span de teste enviado do runner não apareceu no Jaeger (saída: $(printf '%s' "$out" | tr '\n' ' '))." \
    "otlp=000 ou timeout = o Security Group do $OBS_NAME não libera o runner; rode de novo: make bootstrap" \
    "Se persistir: make status (mostra o estado do runner e da observabilidade)"
  ok "span de teste (trace $trace) saiu do runner, passou pelo Collector :4318 e foi achado no Jaeger :16686"
}

# --- 11. Clone + kit ------------------------------------------------------------
listar_kit() {
  [ -d "$KIT_DIR" ] || die "kit não encontrado em $KIT_DIR." \
    "Atualize o repositório do curso: git -C $COURSE_DIR pull"
  ( cd "$KIT_DIR" && find . -type f ! -path '*/__pycache__/*' ! -path '*/.pytest_cache/*' \
      ! -name '*.pyc' ! -name '.DS_Store' | sed 's|^\./||' | LC_ALL=C sort )
}

# Hash do conteúdo (blob do git): o mesmo no Mac e no Codespaces, sem depender
# de sha256sum/shasum. Arquivo ausente = "-".
hash_arquivo() {
  if [ -f "$1" ]; then git hash-object -- "$1"; else printf -- '-\n'; fi
}

# Impressão digital do kit inteiro (caminho + hash de cada arquivo): mostra no
# log QUAL versão do kit foi copiada, para comparar com a do repositório do curso.
digital_kit() {
  local dir="$1" rel
  while IFS= read -r rel; do
    printf '%s %s\n' "$(hash_arquivo "$dir/$rel")" "$rel"
  done < <(listar_kit) | git hash-object --stdin | cut -c1-12
}

# Sempre lê o kit/ do repositório do curso no disco, na hora (nada em cache):
# arquivo do kit cujo hash difere do clone é sobrescrito; arquivo do aluno que não
# é do kit nunca é tocado. Imprime no stdout os arquivos que mudaram.
copiar_kit() {
  local dest="$1" rel n=0 h_kit h_clone
  [ -d "$KIT_DIR" ] || die "kit não encontrado em $KIT_DIR." \
    "Atualize o repositório do curso: git -C $COURSE_DIR pull"
  log "  kit do curso: $KIT_DIR (versão $(digital_kit "$KIT_DIR"), curso em $(git -C "$COURSE_DIR" log -1 --format=%h 2>/dev/null || echo '?'))"
  while IFS= read -r rel; do
    n=$((n + 1))
    h_kit="$(hash_arquivo "$KIT_DIR/$rel")"
    h_clone="$(hash_arquivo "$dest/$rel")"
    if [ "$h_kit" != "$h_clone" ]; then
      mkdir -p "$dest/$(dirname "$rel")"
      cp "$KIT_DIR/$rel" "$dest/$rel"
      printf '    kit: %s (%s -> %s)\n' "$rel" "${h_clone:0:7}" "${h_kit:0:7}" >&2
      printf '%s\n' "$rel"
    fi
  done < <(listar_kit)
  [ "$n" -gt 0 ] || die "o kit em $KIT_DIR está vazio." "Atualize o repositório do curso: git -C $COURSE_DIR pull"
}

etapa_clone() {
  CLONE="$(clone_dir)"
  log "11/$ETAPAS Clone do projeto em $CLONE e kit de validação..."
  local origem
  if [ -d "$CLONE/.git" ]; then
    origem="$(git -C "$CLONE" remote get-url origin 2>/dev/null || true)"
    case "${origem,,}" in
      *"${PROJECT_PATH,,}.git"|*"${PROJECT_PATH,,}") ok "clone já existe" ;;
      *) warn "o clone em $CLONE aponta para '${origem:-nenhum remote}', e não para $PROJECT_PATH." ;;
    esac
  elif [ -e "$CLONE" ] && [ -n "$(ls -A "$CLONE" 2>/dev/null)" ]; then
    die "a pasta $CLONE já existe, não está vazia e não é um clone git." \
      "Renomeie a pasta (mv $CLONE $CLONE.bak) ou escolha outra: make bootstrap CLONE_DIR=/workspaces/outra-pasta"
  else
    # No Codespaces, /workspaces pertence ao root: cria a pasta do clone já com
    # dono do usuário (o devcontainer tem sudo sem senha).
    mkdir -p "$CLONE" 2>/dev/null || sudo install -d -o "$(id -u)" -g "$(id -g)" "$CLONE" || \
      die "não consegui criar a pasta $CLONE." "Escolha outra: make bootstrap CLONE_DIR=$HOME/$PROJECT_NAME"
    [ -w "$CLONE" ] || sudo chown "$(id -u):$(id -g)" "$CLONE"
    git clone -q "$PROJECT_SSH_URL" "$CLONE" >&2 || \
      die "o git clone de $PROJECT_SSH_URL falhou." \
        "Teste a chave com: ssh -T git@$GITLAB_SSH_HOST (deve responder 'Welcome to GitLab')" \
        "e rode de novo: make bootstrap"
    ok "projeto clonado"
  fi

  # Repositório vazio: o primeiro push define a branch. Fixamos master porque é o
  # nome usado no README e no pipeline do curso.
  if ! git -C "$CLONE" rev-parse -q --verify HEAD >/dev/null; then
    git -C "$CLONE" symbolic-ref HEAD refs/heads/master
  fi
  # Sem identidade git o commit do kit falha; usa a do próprio GitLab, só neste clone.
  if [ -z "$(git -C "$CLONE" config user.email || true)" ]; then
    git -C "$CLONE" config user.name "$GITLAB_USER_NAME"
    git -C "$CLONE" config user.email "$GITLAB_USER_EMAIL"
  fi

  commit_push_kit
}

# Copia o kit, commita SÓ os arquivos do kit e envia. Usado pelo bootstrap e
# pelo make kit (o kit do curso pode ter sido atualizado depois do bootstrap).
commit_push_kit() {
  local mudou
  local -a kit_files
  mudou="$(copiar_kit "$CLONE")"
  if [ -n "$mudou" ]; then
    ok "$(printf '%s\n' "$mudou" | wc -l | tr -d ' ') arquivo(s) do kit atualizados no clone"
  else
    ok "kit já atualizado no clone (mesmo hash do curso)"
  fi
  mapfile -t kit_files < <(listar_kit)
  git -C "$CLONE" add -- "${kit_files[@]}"
  # Commit só dos arquivos do kit: o que o aluno tiver em stage fica intocado.
  if ! git -C "$CLONE" diff --cached --quiet -- "${kit_files[@]}"; then
    git -C "$CLONE" commit -q -m "$KIT_COMMIT_MSG" -- "${kit_files[@]}"
    ok "commit: $KIT_COMMIT_MSG"
  fi
  # Push só quando o último commit é o do kit e é o ÚNICO ainda não enviado (cobre
  # também um push que falhou antes). Commits locais do aluno ele mesmo envia.
  local pendentes primeiro_push=0
  if git -C "$CLONE" rev-parse -q --verify '@{u}' >/dev/null 2>&1; then
    pendentes="$(git -C "$CLONE" rev-list --count '@{u}..HEAD')"
  else
    primeiro_push=1
    pendentes="$(git -C "$CLONE" rev-list --count HEAD 2>/dev/null || echo 0)"
  fi
  if [ "$pendentes" -gt 0 ] && [ "$(git -C "$CLONE" log -1 --format=%s)" = "$KIT_COMMIT_MSG" ]; then
    if [ "$pendentes" -gt 1 ]; then
      warn "o commit do kit ficou em cima de $((pendentes - 1)) commit(s) seu(s) ainda não enviado(s); não enviei nada. Envie tudo quando quiser: git -C $CLONE push"
    elif git -C "$CLONE" push -q -u origin HEAD >&2; then
      ok "push do kit para $(git -C "$CLONE" rev-parse --abbrev-ref HEAD)"
      if [ "$primeiro_push" = 1 ] && [ -n "${PROJECT_ID:-}" ]; then
        fixar_branch_padrao
      fi
    else
      warn "o push do kit falhou. Depois de resolver, rode: git -C $CLONE push -u origin HEAD"
    fi
  fi
}

# Garante que a branch do primeiro push é a default do projeto (é nela que o
# pipeline do README roda o deploy).
fixar_branch_padrao() {
  local branch atual
  branch="$(git -C "$CLONE" rev-parse --abbrev-ref HEAD)"
  gitlab_api GET "/projects/$PROJECT_ID"
  atual="$(printf '%s' "$GL_BODY" | jq -r '.default_branch // ""')"
  [ "$atual" = "$branch" ] && return 0
  gitlab_api PUT "/projects/$PROJECT_ID" "$(jq -n --arg b "$branch" '{default_branch:$b}')"
  if [ "$GL_STATUS" = 200 ]; then
    ok "branch padrão do projeto: $branch"
  else
    warn "não consegui definir $branch como branch padrão (HTTP $GL_STATUS). Ajuste em Settings > Repository."
  fi
}

resumo() {
  local branch pub dur
  branch="$(git -C "$CLONE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo master)"
  pub="$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$OBS_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null || true)"
  dur=$(( $(date +%s) - T0 ))
  cat >&2 <<EOF

Bootstrap concluído em $((dur / 60)) min $((dur % 60)) s.
  Projeto GitLab : $PROJECT_WEB_URL
  Pasta do clone : $CLONE (branch $branch)
  Runner         : $RUNNER_NAME (id $RUNNER_ID, online) na EC2 ${INSTANCE_ID:-?}
  Observabilidade: $OBS_NAME na EC2 $OBS_ID
  Grafana        : http://$pub:3000  (usuário admin; senha: make grafana-senha)
  OTLP (apps)    : SSM $SSM_OTLP

Próximos passos:
  1. cd $CLONE
  2. escreva o módulo rag-cluster, o raiz, o backend.tf e os jobs (seu) do .gitlab-ci.yml (README do Trabalho Final)
  3. valide antes do push: make -C $TF_DIR validar
  4. git add <arquivos> && git commit -m "feat: ..."   e depois   git push
  5. acompanhe: make -C $TF_DIR status   |   links prontos: make -C $TF_DIR links
EOF
}

comando_kit() {
  load_env
  CLONE="$(clone_dir_offline)"
  require_clone "$CLONE"
  log "Copiando o kit de $KIT_DIR para $CLONE e enviando só os arquivos do kit..."
  commit_push_kit
  log "Pronto. Os seus arquivos (módulo, raiz, backend.tf, jobs do .gitlab-ci.yml) não foram tocados."
}

main() {
  case "${1:-}" in
    kit) comando_kit ;;
    "")
      etapa_preflight
      etapa_rede
      etapa_tooling
      etapa_chave_ssh
      etapa_projeto
      etapa_runner
      etapa_ec2
      etapa_obs
      etapa_ansible
      etapa_esperar_online
      etapa_venv_runner
      etapa_obs_pronta
      etapa_clone
      resumo
      ;;
    *) die "uso: bootstrap.sh [kit]" ;;
  esac
}

# Carregado com "source" (testes), só define as funções.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
