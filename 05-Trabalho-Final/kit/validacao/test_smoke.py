"""Smoke do Trabalho Final: o assistente responde de verdade pelo ALB do ambiente.

Roda nos jobs smoke-dev e smoke-prod, depois do deploy do ambiente:

    pytest validacao/test_smoke.py --ambiente dev --alb-dns "$ALB_DNS" --release "$RELEASE_ID"

Faz HTTP no ALB (sem SSH, sem console) e le o Target Group e as EC2 com boto3,
usando a LabRole do runner. Na primeira vez a EC2 ainda esta instalando o
assistente (user_data: apt, venv e pip), e numa troca de release o
create_before_destroy sobe as EC2 novas antes de derrubar as antigas: por isso
o primeiro teste espera ate 300 s pelo Target Group saudavel.
"""

import time
import uuid

import pytest

from conftest import esperar, esperar_tg_healthy, falhar, http_json, log, na_aws

PERGUNTA_FONE = "Meu fone de ouvido parou de funcionar, tem garantia?"
DOC_GARANTIA = "politica-garantia-eletronicos"
ESPERA_TG = 300
ESPERA_HTTP = 180


@pytest.fixture(scope="module")
def url(alb_dns):
    return f"http://{alb_dns}"


@pytest.fixture(scope="module")
def tg_saudavel(request, alb_dns, ambiente, nos_esperados):
    if not na_aws(alb_dns):
        return None
    aws = request.getfixturevalue("aws")
    ok, estados = esperar_tg_healthy(aws, ambiente, nos_esperados, ESPERA_TG)
    return ok, estados


def test_target_group_com_alvos_healthy(tg_saudavel, ambiente, nos_esperados, alb_dns):
    if tg_saudavel is None:
        pytest.skip(f"{alb_dns} não é um ALB da AWS: sem Target Group para conferir")
    ok, estados = tg_saudavel
    if ok:
        return
    resumo = "; ".join(f"{estado}: {', '.join(ids)}" for estado, ids in sorted(estados.items())) or "nenhum alvo"
    falhar(
        f"Depois de {ESPERA_TG} s, o Target Group qc-rag-tg-{ambiente} tem {len(estados.get('healthy', []))} "
        f"alvo(s) healthy, mas {ambiente} precisa de {nos_esperados}. Estado dos alvos: {resumo}. "
        "Se está unhealthy (Target.FailedHealthChecks), o assistente não respondeu GET /health na porta "
        "8080: confira no módulo rag-cluster o health_check { path = \"/health\" } e port = 8080 do "
        "aws_lb_target_group.web, a regra de entrada 8080 do aws_security_group.web e se o user_data "
        "é o templatefile do kit (na EC2, o log fica em /var/log/qc-rag-bootstrap.log). Se não há alvo, "
        "falta o aws_lb_target_group_attachment.web com count = length(aws_instance.web)."
    )


@pytest.fixture(scope="module")
def health(url, tg_saudavel):
    def pedir():
        status, corpo, _ = http_json(f"{url}/health", timeout=8)
        return corpo if status == 200 and isinstance(corpo, dict) else None

    corpo, erro = esperar("GET /health no ALB", pedir, ESPERA_HTTP, intervalo=5)
    return corpo, erro


def test_health_responde(health, url):
    corpo, erro = health
    if corpo is None:
        falhar(
            f"GET {url}/health não respondeu 200 em {ESPERA_HTTP} s (último erro: {erro or 'resposta não JSON'}). "
            "HTTP 502/503 = o ALB não tem alvo saudável (veja o teste do Target Group); timeout = o "
            "listener da porta 80 ou a entrada 80 do Security Group faltam no módulo rag-cluster."
        )
    if corpo.get("status") != "ok":
        falhar(f"O /health respondeu {corpo}, sem \"status\": \"ok\".")


def test_health_mostra_o_ambiente_certo(health, ambiente):
    corpo, _ = health
    if corpo is None:
        pytest.skip("o /health não respondeu (ver test_health_responde)")
    if corpo.get("ambiente") != ambiente:
        falhar(
            f"O /health do ALB de {ambiente} diz ambiente = \"{corpo.get('ambiente')}\". O raiz precisa passar "
            "ambiente = terraform.workspace para o module rag_cluster, e o módulo precisa repassar "
            "var.ambiente ao templatefile do user_data (main.tf do módulo)."
        )


def test_health_mostra_o_release_publicado(health, release_esperado):
    corpo, _ = health
    if corpo is None:
        pytest.skip("o /health não respondeu (ver test_health_responde)")
    if not corpo.get("release") or corpo.get("release") == "local":
        falhar(
            f"O /health não informa o release (veio \"{corpo.get('release')}\"). O módulo rag-cluster precisa "
            "passar release_id = var.release_id ao templatefile, e o raiz release_id = "
            "module.app_release.release_id."
        )
    if release_esperado and corpo["release"] != release_esperado:
        falhar(
            f"O ALB responde o release {corpo['release']}, mas o Terraform publicou {release_esperado}. "
            "As EC2 antigas não foram trocadas: o aws_instance.web precisa de "
            "user_data_replace_on_change = true e lifecycle { create_before_destroy = true }."
        )


def test_health_telemetria_ligada(health):
    corpo, _ = health
    if corpo is None:
        pytest.skip("o /health não respondeu (ver test_health_responde)")
    if corpo.get("telemetria") != "otlp":
        falhar(
            f"O /health diz telemetria = \"{corpo.get('telemetria')}\": o assistente responde, mas não envia "
            "trace, métrica nem log. O raiz lê /fiap/trabalho-final-ai/otlp-endpoint do SSM e passa "
            "otlp_endpoint ao module rag_cluster? O módulo repassa var.otlp_endpoint ao templatefile?"
        )


def test_pergunta_do_fone_com_citacao(url, health):
    corpo_health, _ = health
    if corpo_health is None:
        pytest.skip("o /health não respondeu (ver test_health_responde)")
    request_id = f"smoke-{uuid.uuid4().hex[:12]}"
    status, corpo, texto = http_json(f"{url}/assistant/ask", {"question": PERGUNTA_FONE, "request_id": request_id},
                                     timeout=15)
    if status != 200 or not isinstance(corpo, dict):
        falhar(f"POST {url}/assistant/ask respondeu HTTP {status}: {texto[:300]}")
    citados = [c.get("doc_id") for c in corpo.get("citations") or []]
    if corpo.get("abstained") or DOC_GARANTIA not in citados:
        falhar(
            f"Para \"{PERGUNTA_FONE}\" o assistente respondeu \"{(corpo.get('answer') or '')[:160]}\" citando "
            f"{citados or 'nada'}; o esperado é citar {DOC_GARANTIA}. O app/corpus publicado é o do kit? "
            "O rag-quality-gate passou neste pipeline?"
        )
    if corpo.get("request_id") != request_id or not corpo.get("trace_id"):
        falhar(
            f"A resposta veio com request_id = {corpo.get('request_id')} e trace_id = {corpo.get('trace_id')}. "
            f"O request_id enviado foi {request_id} e o trace_id precisa existir (telemetria ligada)."
        )


def test_alb_distribui_entre_as_instancias(url, health, nos_esperados, ambiente):
    corpo_health, _ = health
    if corpo_health is None:
        pytest.skip("o /health não respondeu (ver test_health_responde)")
    hosts = set()
    for _ in range(12 * nos_esperados):
        status, corpo, _ = http_json(f"{url}/health", timeout=8)
        if status == 200 and isinstance(corpo, dict):
            hosts.add(corpo.get("host"))
        if len(hosts) >= nos_esperados:
            break
        time.sleep(0.2)
    log(f"  hosts vistos atrás do ALB: {sorted(map(str, hosts))}")
    if len(hosts) < nos_esperados:
        falhar(
            f"Em {12 * nos_esperados} chamadas, o ALB de {ambiente} respondeu de {len(hosts)} EC2 "
            f"({sorted(map(str, hosts))}), mas {ambiente} tem {nos_esperados}. Alguma EC2 está fora do Target "
            "Group ou unhealthy: veja o teste do Target Group."
        )


def test_ec2_running_com_os_nomes_certos(request, alb_dns, ambiente, nos_esperados):
    if not na_aws(alb_dns):
        pytest.skip(f"{alb_dns} não é um ALB da AWS")
    aws = request.getfixturevalue("aws")
    ec2 = aws.client("ec2")
    resposta = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [f"qc-rag-{ambiente}-*"]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    nomes = sorted(
        next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "?")
        for r in resposta["Reservations"] for i in r["Instances"]
    )
    esperados = [f"qc-rag-{ambiente}-{i:03d}" for i in range(1, nos_esperados + 1)]
    if sorted(set(nomes)) != esperados:
        falhar(
            f"EC2 running com tag qc-rag-{ambiente}-*: {nomes or 'nenhuma'}; esperado {esperados}. "
            "Logo depois de uma troca de release, as antigas podem ainda estar desligando: rode o job de "
            'novo. Se persistir, confira Name = format("qc-rag-%s-%03d", var.ambiente, count.index + 1).'
        )


def test_dns_do_alb_confere(request, alb_dns, ambiente):
    if not na_aws(alb_dns):
        pytest.skip(f"{alb_dns} não é um ALB da AWS")
    elb = request.getfixturevalue("aws").client("elbv2")
    try:
        alb = elb.describe_load_balancers(Names=[f"qc-rag-alb-{ambiente}"])["LoadBalancers"][0]
    except elb.exceptions.LoadBalancerNotFoundException:
        falhar(f'O ALB qc-rag-alb-{ambiente} não existe: o name do aws_lb.web deve ser "qc-rag-alb-${{var.ambiente}}".')
    if alb["DNSName"].lower() != alb_dns.split(":")[0].lower():
        falhar(
            f"O output alb_dns ({alb_dns}) não é o DNS do qc-rag-alb-{ambiente} ({alb['DNSName']}). "
            "O output alb_dns da raiz deve ser module.rag_cluster.alb_dns."
        )
