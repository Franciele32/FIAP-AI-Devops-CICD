# shellcheck shell=bash
# Funções comuns aos scripts do bootstrap do Trabalho Final (AI-Devops-CICD).
# Carregado com "source" por bootstrap.sh, status.sh, links.sh, validar.sh e destroy.sh.
#
# Convenção de saída: stdout = só o resultado; progresso, avisos e erros em stderr.
#
# As variáveis daqui são usadas pelos scripts que carregam este arquivo:
# shellcheck disable=SC2034

set -euo pipefail

# --- Caminhos e nomes fixos (contrato do Trabalho Final) ---------------------
BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "$BOOTSTRAP_DIR/.." && pwd)"
COURSE_DIR="$(cd "$TF_DIR/.." && pwd)"
RUNNER_TF_DIR="$BOOTSTRAP_DIR/runner"
OBS_TF_DIR="$BOOTSTRAP_DIR/obs"
OBS_STACK_DIR="$BOOTSTRAP_DIR/obs/stack"
KIT_DIR="$TF_DIR/kit"
ANSIBLE_DIR="$COURSE_DIR/02-Ansible/01-provisionando-gitlab-runner/ansible-gitlab-runner"
NET_VPC_DIR="$COURSE_DIR/01-Terraform/demos/02-Modules/vpc-call"
NET_RT_DIR="$COURSE_DIR/01-Terraform/demos/02-Modules/RT-call"

AWS_REGION="us-east-1"
NET_NAME="fiap-lab"
# Nomes PRÓPRIOS deste trabalho: na mesma conta do Academy pode existir o
# Trabalho Final de outra disciplina (qc-runner-trabalho-final, /fiap/trabalho-final/*,
# trabalho-final/*.tfstate) e a Aula 5 (/qc-assistant/*). Nada aqui lê ou apaga esses.
PROJETO_PADRAO="trabalho-final-ai"
SSM_PREFIX="/fiap/trabalho-final-ai"
STATE_PREFIX="trabalho-final-ai"
RUNNER_NAME="qc-runner-tf-ai"
RUNNER_TOKEN_SSM="$SSM_PREFIX/runner-token"
OBS_NAME="qc-obs-tf-ai"
SSM_OTLP="$SSM_PREFIX/otlp-endpoint"
SSM_OBS_QUERY="$SSM_PREFIX/obs-query"
SSM_GRAFANA="$SSM_PREFIX/grafana-admin"
OBS_CONFIG_PREFIX="$STATE_PREFIX/obs-config"
ENV_FILE="$HOME/.trabalho-final-ai.env"
SSH_KEY="$HOME/.ssh/gitlab"
VENV_TESTES="$HOME/.venv-trabalho-final-ai"
STATE_KEY_ALUNO="$STATE_PREFIX/terraform.tfstate"
# Venv do pipeline no runner: o mesmo /opt/venv do playbook do Módulo 02 (Checkov),
# completado pelo bootstrap com o que os testes do kit importam.
RUNNER_VENV="/opt/venv"
KIT_PIP="pytest==8.4.2 boto3==1.35.49 requests pyyaml"

# Cache do Terraform FORA do repositório e um por stack: o provider AWS tem
# ~600 MB, e dois stacks no mesmo .terraform brigam pelo backend ("Backend
# configuration changed"). Os providers baixados são compartilhados.
TF_CACHE_DIR="$HOME/.cache/trabalho-final-ai"
export TF_PLUGIN_CACHE_DIR="$TF_CACHE_DIR/plugins"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
# tf_stack <pasta-do-stack> <nome-do-cache> args...: terraform com o cache certo.
tf_stack() { local dir="$1" nome="$2"; shift 2; TF_DATA_DIR="$TF_CACHE_DIR/tf-$nome" terraform -chdir="$dir" "$@"; }

# Guardados antes de ler o ~/.trabalho-final-ai.env: o que vem da linha de comando
# (make ... REPO=x) vence o que está salvo.
_CLI_REPO="${REPO:-}"
_CLI_TOKEN="${GITLAB_TOKEN:-}"
_CLI_URL="${GITLAB_URL:-}"

# --- Log e erro ----------------------------------------------------------------
log()  { printf '>> %s\n' "$*" >&2; }
ok()   { printf '   ok: %s\n' "$*" >&2; }
warn() { printf 'AVISO: %s\n' "$*" >&2; }

# die "o que deu errado" ["como corrigir" ...]: cada argumento extra vira uma
# linha de orientação, para o aluno saber o próximo comando sem procurar no README.
die() {
  printf '\nERRO: %s\n' "$1" >&2
  shift
  local linha
  for linha in "$@"; do printf '  %s\n' "$linha" >&2; done
  exit 1
}

need_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "o comando '$c' não foi encontrado neste ambiente." \
      "Rode no Codespaces do repositório FIAP-AI-Devops-CICD (o devcontainer já traz as ferramentas)."
  done
}

# --- Configuração persistida (~/.trabalho-final-ai.env) ---------------------
load_env() {
  REPO="" GITLAB_TOKEN="" GITLAB_URL=""
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
  fi
  [ -n "$_CLI_REPO" ] && REPO="$_CLI_REPO"
  [ -n "$_CLI_TOKEN" ] && GITLAB_TOKEN="$_CLI_TOKEN"
  [ -n "$_CLI_URL" ] && GITLAB_URL="$_CLI_URL"
  GITLAB_URL="${GITLAB_URL:-https://gitlab.com}"
  GITLAB_URL="${GITLAB_URL%/}"
  GITLAB_HOST="${GITLAB_URL#*://}"
  GITLAB_HOST="${GITLAB_HOST%%/*}"
  GITLAB_SSH_HOST="${GITLAB_HOST%%:*}"
  # O token chega pelo ambiente do make (e dentro do MAKEFLAGS). Tirá-lo do
  # ambiente impede que terraform, ansible, git e aws o herdem; o curl recebe o
  # header por descritor de arquivo.
  export -n GITLAB_TOKEN REPO 2>/dev/null || true
  unset MAKEFLAGS MFLAGS
}

# Sem token salvo: pergunta sem eco no terminal, o que também evita o token no
# histórico do shell e no 'ps' do make. Sem terminal (bloco colado com bash -s,
# pipeline de teste), lê UMA linha do stdin: make bootstrap < arquivo-com-o-token.
obter_token() {
  if [ -z "$GITLAB_TOKEN" ]; then
    if [ -t 0 ] && [ -t 2 ]; then
      printf 'Cole o seu GitLab Personal Access Token (Legacy, escopo api; não aparece na tela): ' >&2
      IFS= read -rs GITLAB_TOKEN || true
      printf '\n' >&2
    elif [ ! -t 0 ]; then
      IFS= read -r GITLAB_TOKEN || true
      # Num bloco colado com bash -s, a "próxima linha" seria um comando, e não
      # o token: recusa sem mostrar o que leu.
      case "$GITLAB_TOKEN" in
        gl*-*) ;;
        *) GITLAB_TOKEN=""
           die "o que chegou pelo stdin não parece um token do GitLab (esperado glpat-...)." \
             "Rode make bootstrap REPO=${REPO:-$PROJETO_PADRAO} num terminal (o token é pedido sem eco)" \
             "ou mande só o token pelo stdin: make bootstrap REPO=${REPO:-$PROJETO_PADRAO} < arquivo-com-o-token" ;;
      esac
    fi
    GITLAB_TOKEN="$(printf '%s' "$GITLAB_TOKEN" | tr -d '[:space:]')"
  fi
  require_token
}

# glpat-abc123... -> glpat-****: o suficiente para o aluno saber qual token está em uso.
mask() { local s="$1"; case "$s" in *-*) printf '%s-****' "${s%%-*}" ;; *) printf '****' ;; esac; }

save_env() {
  local tmp
  tmp="$(mktemp "$HOME/.trabalho-final-ai.env.XXXXXX")"
  chmod 600 "$tmp"
  {
    echo "# Gerado por 'make bootstrap' (Trabalho Final AI-Devops-CICD). Não versionar: contém o token do GitLab."
    printf 'REPO=%q\n' "$REPO"
    printf 'GITLAB_TOKEN=%q\n' "$GITLAB_TOKEN"
    printf 'GITLAB_URL=%q\n' "$GITLAB_URL"
  } > "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# Sem REPO na linha de comando nem salvo: usa o nome padrão do trabalho.
require_repo() {
  [ -n "$REPO" ] || REPO="$PROJETO_PADRAO"
}

require_token() {
  [ -n "$GITLAB_TOKEN" ] || die "falta o token do GitLab (GITLAB_TOKEN vazio e nada salvo em $ENV_FILE)." \
    "Crie um Personal Access Token do tipo Legacy, com escopo 'api', em $GITLAB_URL/-/user_settings/personal_access_tokens" \
    "e rode: make bootstrap REPO=${REPO:-$PROJETO_PADRAO} (o token é pedido sem aparecer na tela)"
}

# --- Parse do REPO ------------------------------------------------------------
# Aceita git@host:ns/proj.git, https://host/ns/proj(.git) ou só "proj". Preenche
# PROJECT_NS (vazio no formato curto: o namespace vem do dono do token) e PROJECT_NAME.
parse_repo() {
  local r="$1" path
  r="${r%/}"
  r="${r%.git}"
  case "$r" in
    git@*:*)                    path="${r#*:}" ;;
    ssh://*|http://*|https://*) path="${r#*://}"
                                case "$path" in */*) path="${path#*/}" ;; *) path="" ;; esac ;;
    *)                          path="$r" ;;
  esac
  path="${path#/}"
  if [ "${path#*/}" = "$path" ]; then
    PROJECT_NS=""
    PROJECT_NAME="$path"
  else
    PROJECT_NS="${path%/*}"
    PROJECT_NAME="${path##*/}"
  fi
  if [ -z "$PROJECT_NAME" ] || ! [[ "$PROJECT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    die "não consegui entender o REPO '$1'." \
      "Formatos aceitos: trabalho-final-ai | git@gitlab.com:<usuario>/trabalho-final-ai.git | https://gitlab.com/<usuario>/trabalho-final-ai"
  fi
}

# PROJECT_PATH = ns/nome completo; no formato curto precisa do usuário do token.
resolve_project_path() {
  parse_repo "$REPO"
  if [ -z "$PROJECT_NS" ]; then
    [ -n "${GITLAB_USERNAME:-}" ] || gitlab_whoami
    PROJECT_NS="$GITLAB_USERNAME"
  fi
  PROJECT_PATH="$PROJECT_NS/$PROJECT_NAME"
  PROJECT_WEB_URL="$GITLAB_URL/$PROJECT_PATH"
  PROJECT_SSH_URL="git@$GITLAB_SSH_HOST:$PROJECT_PATH.git"
}

# Pasta do clone: /workspaces/<nome-do-projeto> no Codespaces, irmã do repositório
# do curso. CLONE_DIR sobrescreve (útil nos testes ou com outra pasta).
clone_dir() {
  if [ -n "${CLONE_DIR:-}" ]; then
    printf '%s' "${CLONE_DIR%/}"
  else
    printf '%s/%s' "$(dirname "$COURSE_DIR")" "$PROJECT_NAME"
  fi
}

# Para os targets que só mexem no clone: não precisam de token, só do nome do projeto.
clone_dir_offline() {
  if [ -z "${CLONE_DIR:-}" ]; then
    require_repo
    parse_repo "$REPO"
  fi
  clone_dir
}

urlencode() { jq -rn --arg v "$1" '$v|@uri'; }

# --- API do GitLab --------------------------------------------------------------
# gitlab_api MÉTODO /caminho [json-do-corpo]
# O token vai por descritor de arquivo (curl -H @/dev/fd/N): nunca aparece em 'ps'
# nem em log. O corpo vai pelo stdin. Resultado em GL_BODY e GL_STATUS (globais,
# porque o chamador quase sempre precisa dos dois).
gitlab_api() {
  local method="$1" path="$2" data="${3:-}" resp
  local -a body_args=()
  [ -n "$data" ] && body_args=(-H 'Content-Type: application/json' --data-binary @-)
  if ! resp="$(printf '%s' "$data" | curl -sS -X "$method" -w '\n%{http_code}' \
      --connect-timeout 10 --max-time 60 \
      -H @<(printf 'PRIVATE-TOKEN: %s\n' "$GITLAB_TOKEN") \
      "${body_args[@]}" "$GITLAB_URL/api/v4$path")"; then
    die "não consegui falar com o GitLab em $GITLAB_URL ($method $path)." \
      "Confira a internet do Codespaces e a URL do GitLab (GITLAB_URL=$GITLAB_URL)."
  fi
  GL_STATUS="${resp##*$'\n'}"
  GL_BODY="${resp%$'\n'*}"
}

# Mensagem do GitLab no corpo do erro, para o die explicar o motivo real.
gl_err() { printf '%s' "$GL_BODY" | jq -rc '.message // .error // .' 2>/dev/null || printf '%s' "$GL_BODY"; }

gitlab_whoami() {
  gitlab_api GET /user
  case "$GL_STATUS" in
    200) ;;
    401) die "o GitLab recusou o token (401 Unauthorized): ele é inválido, expirou ou foi revogado." \
           "Crie um novo Personal Access Token (Legacy, escopo 'api') em $GITLAB_URL/-/user_settings/personal_access_tokens," \
           "apague o token salvo (rm -f $ENV_FILE) e rode de novo: make bootstrap REPO=${REPO:-$PROJETO_PADRAO}" ;;
    *)   die "falha ao consultar o dono do token (GET /user -> HTTP $GL_STATUS): $(gl_err)" ;;
  esac
  GITLAB_USERNAME="$(printf '%s' "$GL_BODY" | jq -r '.username')"
  GITLAB_USER_NAME="$(printf '%s' "$GL_BODY" | jq -r '.name // .username')"
  GITLAB_USER_EMAIL="$(printf '%s' "$GL_BODY" | jq -r --arg u "$GITLAB_USERNAME" \
    '(.commit_email // .public_email // .email // "") | if . == "" then "\($u)@users.noreply.gitlab.com" else . end')"
}

gitlab_check_scope() {
  local scopes
  gitlab_api GET /personal_access_tokens/self
  [ "$GL_STATUS" = 200 ] || die "não consegui ler os escopos do token (GET /personal_access_tokens/self -> HTTP $GL_STATUS): $(gl_err)" \
    "Use um Personal Access Token (glpat-...) com escopo 'api', e não um token de projeto ou de runner."
  scopes="$(printf '%s' "$GL_BODY" | jq -r '(.scopes // []) | join(", ")')"
  printf '%s' "$GL_BODY" | jq -e '(.scopes // []) | index("api")' >/dev/null || \
    die "o token não tem o escopo 'api' (escopos atuais: ${scopes:-nenhum})." \
      "O bootstrap cria o projeto, o runner e a chave SSH pela API, e isso exige 'api'." \
      "Crie outro token do tipo Legacy token marcando 'api' em $GITLAB_URL/-/user_settings/personal_access_tokens" \
      "(o fine-grained token não tem o escopo 'api'), apague o salvo (rm -f $ENV_FILE) e rode de novo: make bootstrap REPO=${REPO:-$PROJETO_PADRAO}"
}

# Preenche PROJECT_ID se o projeto existe; devolve 1 se não existe.
gitlab_find_project() {
  gitlab_api GET "/projects/$(urlencode "$PROJECT_PATH")"
  case "$GL_STATUS" in
    200) PROJECT_ID="$(printf '%s' "$GL_BODY" | jq -r '.id')"; return 0 ;;
    404) return 1 ;;
    *)   die "falha ao procurar o projeto $PROJECT_PATH (HTTP $GL_STATUS): $(gl_err)" ;;
  esac
}

# Id do runner do projeto com a descrição do Trabalho Final (vazio se não há).
gitlab_find_runner() {
  gitlab_api GET "/projects/$PROJECT_ID/runners?type=project_type&per_page=100"
  [ "$GL_STATUS" = 200 ] || die "falha ao listar os runners do projeto $PROJECT_PATH (HTTP $GL_STATUS): $(gl_err)"
  printf '%s' "$GL_BODY" | jq -r --arg d "$RUNNER_NAME" '[.[] | select(.description == $d)] | first | .id // empty'
}

# --- AWS ----------------------------------------------------------------------
aws_check() {
  aws sts get-caller-identity --query Account --output text 2>/dev/null || \
    die "credenciais AWS inválidas ou expiradas (aws sts get-caller-identity falhou)." \
      "No AWS Academy, abra 'AWS Details' > 'Show' em AWS CLI, cole o bloco [default] em ~/.aws/credentials" \
      "(a sessão do Learner Lab dura 4 h; depois disso aparece ExpiredToken) e confira com: aws sts get-caller-identity"
}

find_bucket() {
  local b
  b="$(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null | tr '\t' '\n' | grep '^base-config' | head -1 || true)"
  [ -n "$b" ] || die "nenhum bucket 'base-config-*' encontrado na conta AWS." \
    "Crie o bucket base-config-<SEU-RM> como no setup do curso (00-create-codespaces, passo 16)" \
    "e confira com: aws s3 ls | grep base-config"
  printf '%s' "$b"
}

# Descrição do parâmetro do token (guarda runner_id/project_id; vazia se não existe).
ssm_token_description() {
  { aws ssm describe-parameters --region "$AWS_REGION" \
      --parameter-filters "Key=Name,Values=$RUNNER_TOKEN_SSM" \
      --query 'Parameters[0].Description' --output text 2>/dev/null || true; } | sed 's/^None$//'
}

# Grava SecureString sem o valor passar por linha de comando (visível no 'ps'):
# o valor entra no jq pelo stdin e o JSON vai num arquivo 600 apagado em seguida.
ssm_put_secret() {
  local name="$1" value="$2" description="$3" f
  f="$(mktemp)"
  chmod 600 "$f"
  printf '%s' "$value" | jq -Rs --arg n "$name" --arg d "$description" \
    '{Name:$n, Value:., Type:"SecureString", Overwrite:true, Description:$d}' > "$f"
  if ! aws ssm put-parameter --cli-input-json "file://$f" --region "$AWS_REGION" >/dev/null; then
    rm -f "$f"
    die "não consegui gravar o parâmetro SecureString no SSM ($name)." \
      "Confira as credenciais AWS (aws sts get-caller-identity) e rode make bootstrap de novo."
  fi
  rm -f "$f"
}

# --- Terraform do runner ------------------------------------------------------
runner_tf_init() {
  local bucket="$1"
  tf_stack "$RUNNER_TF_DIR" runner init -input=false -reconfigure \
    -backend-config="bucket=$bucket" >/dev/null || \
    die "terraform init do runner falhou (bucket $bucket)." \
      "Veja o detalhe com: TF_DATA_DIR=$TF_CACHE_DIR/tf-runner terraform -chdir=$RUNNER_TF_DIR init -reconfigure -backend-config=bucket=$bucket"
}

# instance_id do runner se já existe no state (vazio caso contrário).
runner_instance_id() {
  tf_stack "$RUNNER_TF_DIR" runner output -json 2>/dev/null | jq -r '.instance_id.value // empty' 2>/dev/null || true
}

# Recursos no state do runner (0 = nada criado ainda).
runner_state_count() {
  { tf_stack "$RUNNER_TF_DIR" runner state list 2>/dev/null || true; } | grep -c . || true
}

# --- Terraform da observabilidade --------------------------------------------
obs_tf_init() {
  local bucket="$1"
  tf_stack "$OBS_TF_DIR" obs init -input=false -reconfigure \
    -backend-config="bucket=$bucket" >/dev/null || \
    die "terraform init da observabilidade falhou (bucket $bucket)." \
      "Veja o detalhe com: TF_DATA_DIR=$TF_CACHE_DIR/tf-obs terraform -chdir=$OBS_TF_DIR init -reconfigure -backend-config=bucket=$bucket"
}

# obs_output <nome>: valor de um output do stack obs (vazio se não existe).
obs_output() {
  tf_stack "$OBS_TF_DIR" obs output -json 2>/dev/null | jq -r --arg k "$1" '.[$k].value // empty' 2>/dev/null || true
}

obs_state_count() {
  { tf_stack "$OBS_TF_DIR" obs state list 2>/dev/null || true; } | grep -c . || true
}

# Parâmetro String do SSM (vazio se não existe). Nunca usar para SecureString.
ssm_get() {
  { aws ssm get-parameter --region "$AWS_REGION" --name "$1" --query Parameter.Value --output text 2>/dev/null || true; } | sed 's/^None$//'
}

ssm_put_string() {
  local name="$1" value="$2" description="$3"
  aws ssm put-parameter --region "$AWS_REGION" --name "$name" --type String --overwrite \
    --value "$value" --description "$description" >/dev/null || \
    die "não consegui gravar o parâmetro $name no SSM." \
      "Confira as credenciais AWS (aws sts get-caller-identity) e rode make bootstrap de novo."
}

ssm_exists() {
  aws ssm describe-parameters --region "$AWS_REGION" --parameter-filters "Key=Name,Values=$1" \
    --query 'length(Parameters)' --output text 2>/dev/null | grep -qx 1
}

# Estado da EC2 pelo id ("" se não existe).
ec2_state() {
  { aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$1" \
      --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || true; } | sed 's/^None$//'
}

# Liga a EC2 se o Academy a parou no fim da sessão (o Terraform não religa).
ec2_ligar_se_parada() {
  local id="$1" nome="$2" estado
  estado="$(ec2_state "$id")"
  if [ "$estado" = "stopped" ] || [ "$estado" = "stopping" ]; then
    log "    EC2 $nome ($id) estava $estado; ligando..."
    aws ec2 wait instance-stopped --region "$AWS_REGION" --instance-ids "$id"
    aws ec2 start-instances --region "$AWS_REGION" --instance-ids "$id" >/dev/null
    aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$id"
  fi
}

# Espera a EC2 aparecer Online no SSM (até 5 min).
esperar_ssm() {
  local id="$1" i ping=""
  for i in $(seq 1 30); do
    ping="$(aws ssm describe-instance-information --region "$AWS_REGION" \
      --filters "Key=InstanceIds,Values=$id" \
      --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)"
    [ "$ping" = "Online" ] && return 0
    [ "$i" = 1 ] && log "    aguardando a EC2 $id aparecer no SSM (até 5 min)..."
    sleep 10
  done
  die "a EC2 $id não ficou online no SSM (PingStatus=${ping:-vazio})." \
    "Confira no console: Systems Manager > Fleet Manager. Depois rode de novo: make bootstrap"
}

# ssm_run <instance-id> <timeout-s> <script-local>: roda o script como root via
# SSM RunShellScript, com polling próprio (o waiter padrão desiste em ~100 s).
# stdout = saída do comando; rc != 0 se o comando não terminou com Success.
ssm_run() {
  local id="$1" timeout="$2" script="$3" cmd st="Pending" i n
  cmd="$(aws ssm send-command --region "$AWS_REGION" --instance-ids "$id" \
    --document-name AWS-RunShellScript --comment "make bootstrap (trabalho-final-ai)" \
    --parameters "$(jq -R . "$script" | jq -sc --arg t "$timeout" '{commands: ., executionTimeout: [$t]}')" \
    --query Command.CommandId --output text)" || return 1
  n=$(( timeout / 5 + 12 ))
  for i in $(seq 1 "$n"); do
    st="$(aws ssm get-command-invocation --region "$AWS_REGION" --command-id "$cmd" --instance-id "$id" \
      --query Status --output text 2>/dev/null || echo Pending)"
    case "$st" in Pending|InProgress|Delayed) sleep 5 ;; *) break ;; esac
  done
  aws ssm get-command-invocation --region "$AWS_REGION" --command-id "$cmd" --instance-id "$id" \
    --query StandardOutputContent --output text 2>/dev/null || true
  if [ "$st" != "Success" ]; then
    aws ssm get-command-invocation --region "$AWS_REGION" --command-id "$cmd" --instance-id "$id" \
      --query StandardErrorContent --output text >&2 2>/dev/null || true
    log "    comando SSM $cmd terminou com Status=$st"
    return 1
  fi
}

# --- venv dos testes (Codespaces) ---------------------------------------------
# O /opt/venv do pipeline só existe no runner. No Codespaces os testes do kit
# usam um venv próprio, criado na primeira vez que alguém precisa dele.
# O pyyaml é para o test_contrato.py ler o .gitlab-ci.yml. O 'import yaml' na
# checagem faz um venv criado antes disso ganhar o pacote no próximo make validar.
ensure_venv_testes() {
  if [ -x "$VENV_TESTES/bin/pytest" ] && "$VENV_TESTES/bin/python" -c 'import boto3, yaml, requests' 2>/dev/null; then
    return 0
  fi
  log "Preparando o venv dos testes em $VENV_TESTES (pytest, boto3, requests, pyyaml; só na primeira vez)..."
  [ -d "$VENV_TESTES" ] || python3 -m venv "$VENV_TESTES"
  "$VENV_TESTES/bin/pip" install --quiet --upgrade pip >&2
  # shellcheck disable=SC2086
  "$VENV_TESTES/bin/pip" install --quiet $KIT_PIP >&2 || \
    die "não consegui instalar $KIT_PIP no venv $VENV_TESTES." \
      "Apague o venv e tente de novo: rm -rf $VENV_TESTES && make validar"
}

# --- Clone do aluno -----------------------------------------------------------
require_clone() {
  local dir="$1"
  [ -d "$dir/.git" ] || die "o clone do seu projeto não existe em $dir." \
    "Rode primeiro: make bootstrap (ele clona o projeto nessa pasta)" \
    "ou aponte outra pasta: make <target> CLONE_DIR=/caminho/do/clone"
}

# Os testes só valem com o kit no clone: sem ele, 'terraform test' passa com
# "0 passed" e daria a falsa impressão de trabalho certo.
require_kit_no_clone() {
  local dir="$1" f
  for f in tests/modulo.tftest.hcl validacao/conftest.py validacao/test_contrato.py validacao/test_smoke.py \
           validacao/test_rag_gate.py modules/app-release/main.tf templates/user_data.sh.tftpl; do
    [ -f "$dir/$f" ] || die "o kit de validação não está no clone (falta $f em $dir)." \
      "Copie o kit com: make kit"
  done
}

# Clone do aluno inicializado (backend S3 do backend.tf dele).
tf_init_clone() {
  local dir="$1"
  log "terraform init em $dir..."
  terraform -chdir="$dir" init -input=false >&2 || \
    die "terraform init falhou no seu projeto ($dir)." \
      "Confira o backend.tf (bucket base-config-<RM>, key $STATE_KEY_ALUNO, region us-east-1, use_lockfile = true)" \
      "e as credenciais AWS (aws sts get-caller-identity)."
}

tf_workspace_exists() {
  local dir="$1" ws="$2"
  terraform -chdir="$dir" workspace list 2>/dev/null | sed 's/^[* ]*//' | grep -qx "$ws"
}
