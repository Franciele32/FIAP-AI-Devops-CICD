"""Opcoes de linha de comando e fixtures compartilhadas pelos testes do kit.

    pytest validacao/test_contrato.py --ambiente dev --plan plan-dev.json
    pytest validacao/test_rag_gate.py
    pytest validacao/test_smoke.py --ambiente dev --alb-dns <DNS> --release <release_id>
    pytest validacao/test_observabilidade.py --ambiente dev --alb-dns <DNS>
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Tamanho da frota por workspace.
FROTA = {"dev": 1, "prod": 2}
REGIAO = "us-east-1"
PARAM_OBS_QUERY = "/fiap/trabalho-final-ai/obs-query"
PARAM_OTLP = "/fiap/trabalho-final-ai/otlp-endpoint"

# validacao/ fica na raiz do repositorio do aluno, ao lado do Terraform raiz e de app/.
RAIZ_TERRAFORM = Path(__file__).resolve().parent.parent


def pytest_addoption(parser):
    grupo = parser.getgroup("trabalho-final-ai")
    grupo.addoption("--ambiente", choices=sorted(FROTA), help="workspace testado: dev (1 EC2) ou prod (2 EC2)")
    grupo.addoption("--plan", help="plan em JSON: terraform show -json plan-<env>.tfplan > plan-<env>.json")
    grupo.addoption("--alb-dns", help="DNS do ALB do ambiente: terraform output -raw alb_dns")
    grupo.addoption("--release", help="release esperado no /health: terraform output -raw release_id")
    grupo.addoption("--app-dir", help="pasta do app para o gate RAG (padrao: app/ na raiz)")
    grupo.addoption("--obs-query", help="JSON com as URLs de consulta; padrao: le o SSM " + PARAM_OBS_QUERY)
    grupo.addoption("--timeout-obs", type=int, default=120,
                    help="segundos de espera pela telemetria no coletor (padrao 120)")


def log(*args):
    # stderr: progresso e espera nao sao resultado; com --capture=tee-sys aparecem no log do job.
    print(*args, file=sys.stderr, flush=True)


def falhar(mensagem):
    # Sem traceback: a aba Tests do GitLab mostra so a mensagem para o aluno.
    pytest.fail(mensagem, pytrace=False)


@pytest.fixture(scope="session")
def ambiente(request):
    valor = request.config.getoption("--ambiente")
    if not valor:
        falhar(
            "Faltou a opção --ambiente (dev ou prod) na chamada do pytest. Copie o job do "
            "kit (ci/kit-jobs.yml) sem alterar a linha do pytest."
        )
    return valor


@pytest.fixture(scope="session")
def nos_esperados(ambiente):
    return FROTA[ambiente]


@pytest.fixture(scope="session")
def plano(request):
    caminho = request.config.getoption("--plan")
    if not caminho:
        falhar(
            "Faltou a opção --plan com o plan em JSON. O job contrato-<env> do kit gera o "
            "arquivo com terraform show -json plan-<env>.tfplan > plan-<env>.json."
        )
    arquivo = Path(caminho)
    if not arquivo.is_file():
        falhar(
            f"O arquivo do plan {caminho} não existe. O job contrato-<env> precisa de "
            "needs: [plan-<env>], e o seu job plan-<env> precisa publicar plan-<env>.tfplan "
            "(e a pasta build/) em artifacts: paths no .gitlab-ci.yml."
        )
    try:
        return json.loads(arquivo.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        falhar(
            f"O arquivo {caminho} não é um plan em JSON (parece o .tfplan binário). "
            "Converta com terraform show -json plan-<env>.tfplan > plan-<env>.json."
        )


@pytest.fixture(scope="session")
def backend():
    # O terraform init grava aqui o backend ja resolvido, inclusive o que veio por
    # -backend-config: e a fonte mais fiel do backend em uso.
    arquivo = RAIZ_TERRAFORM / ".terraform" / "terraform.tfstate"
    if not arquivo.is_file():
        falhar(
            f"Não encontrei {arquivo.relative_to(RAIZ_TERRAFORM)}: ou o terraform init não "
            "rodou antes do pytest (o job contrato-<env> do kit roda o init), ou o backend.tf "
            'da raiz não tem o bloco terraform { backend "s3" { ... } } e o state ficou local.'
        )
    return json.loads(arquivo.read_text(encoding="utf-8")).get("backend") or {}


@pytest.fixture(scope="session")
def alb_dns(request):
    valor = (request.config.getoption("--alb-dns") or "").strip()
    valor = valor.removeprefix("http://").removeprefix("https://").rstrip("/")
    # Aceita host ou host:porta (localhost:8080 serve para testar o app na sua maquina).
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", valor or ""):
        falhar(
            f'A opção --alb-dns chegou como "{valor}", e não como um DNS de ALB '
            "(...elb.amazonaws.com). O job pega o DNS com terraform output -raw alb_dns no "
            "workspace do ambiente: confira se o outputs.tf da raiz declara "
            'output "alb_dns" { value = module.rag_cluster.alb_dns } e se o deploy-<env> rodou.'
        )
    return valor


def na_aws(alb_dns):
    """Os testes que consultam a AWS (Target Group, EC2) so fazem sentido num ALB real."""
    return alb_dns.split(":")[0].endswith(".elb.amazonaws.com")


@pytest.fixture(scope="session")
def release_esperado(request):
    return (request.config.getoption("--release") or "").strip() or None


# ---------------------------------------------------------------- HTTP

def http_json(url, corpo=None, timeout=10, headers=None):
    """(status, json|None, texto). Nunca levanta por HTTP 4xx/5xx: quem decide e o teste."""
    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    cab = {"Content-Type": "application/json"} if corpo is not None else {}
    cab.update(headers or {})
    req = urllib.request.Request(url, data=dados, headers=cab, method="POST" if corpo is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            texto = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as erro:
        texto = erro.read().decode("utf-8", "replace")
        status = erro.code
    try:
        return status, json.loads(texto), texto
    except json.JSONDecodeError:
        return status, None, texto


def esperar(descricao, funcao, timeout, intervalo=5):
    """Repete funcao() ate devolver algo verdadeiro ou estourar o timeout.

    Devolve (valor, ultimo_erro). O ultimo erro vai na mensagem de falha: e ele
    que diz ao aluno o que estava acontecendo quando o tempo acabou.
    """
    inicio = time.monotonic()
    ultimo = None
    tentativa = 0
    while True:
        tentativa += 1
        try:
            valor = funcao()
            if valor:
                return valor, None
        except Exception as erro:  # noqa: BLE001 - qualquer falha vira "ainda nao"
            ultimo = f"{type(erro).__name__}: {erro}"
        decorrido = time.monotonic() - inicio
        if decorrido >= timeout:
            return None, ultimo
        log(f"  aguardando {descricao} ({int(decorrido)}/{timeout} s, tentativa {tentativa})"
            + (f": {ultimo}" if ultimo else ""))
        time.sleep(intervalo)


# ---------------------------------------------------------------- AWS

@pytest.fixture(scope="session")
def aws():
    try:
        import boto3
    except ImportError:
        falhar(
            "O Python que roda o pytest não tem o boto3. No runner ele vem no /opt/venv: "
            "o job do kit faz source /opt/venv/bin/activate antes do pytest. Não troque essa linha."
        )
    sessao = boto3.session.Session(region_name=os.environ.get("AWS_REGION", REGIAO))
    return sessao


def ler_parametro(aws, nome):
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return aws.client("ssm").get_parameter(Name=nome)["Parameter"]["Value"]
    except ClientError as erro:
        codigo = erro.response.get("Error", {}).get("Code")
        if codigo == "ParameterNotFound":
            falhar(
                f"O parâmetro SSM {nome} não existe. Ele é criado pelo make bootstrap (palco de "
                "observabilidade). Rode make bootstrap de novo e confira com make status."
            )
        falhar(f"Não consegui ler o parâmetro SSM {nome}: {codigo}. A credencial do runner (LabRole) expirou?")
    except BotoCoreError as erro:
        falhar(f"Não consegui falar com o SSM para ler {nome}: {erro}.")


def alvos_do_tg(aws, ambiente):
    """{estado: [ids]} dos alvos do Target Group qc-rag-tg-<ambiente>."""
    elb = aws.client("elbv2")
    nome = f"qc-rag-tg-{ambiente}"
    try:
        tg = elb.describe_target_groups(Names=[nome])["TargetGroups"][0]
    except elb.exceptions.TargetGroupNotFoundException:
        falhar(
            f"O Target Group {nome} não existe na conta. O deploy-{ambiente} rodou? O name do "
            'aws_lb_target_group.web no módulo rag-cluster precisa ser "qc-rag-tg-${var.ambiente}".'
        )
    estados = {}
    for alvo in elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]:
        estado = alvo["TargetHealth"]["State"]
        motivo = alvo["TargetHealth"].get("Reason")
        estados.setdefault(estado, []).append(alvo["Target"]["Id"] + (f" ({motivo})" if motivo else ""))
    return estados


def esperar_tg_healthy(aws, ambiente, nos, timeout=300):
    """Espera ate `nos` alvos healthy (create_before_destroy troca as EC2 no deploy)."""
    estados = {}

    def pronto():
        estados.clear()
        estados.update(alvos_do_tg(aws, ambiente))
        return len(estados.get("healthy", [])) >= nos

    ok, _ = esperar(f"{nos} alvo(s) healthy em qc-rag-tg-{ambiente}", pronto, timeout, intervalo=10)
    return bool(ok), dict(estados)
