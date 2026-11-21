"""Observabilidade do Trabalho Final: uma pergunta real vira metrica, log e trace.

Roda nos jobs observabilidade-dev e observabilidade-prod, depois do deploy:

    pytest validacao/test_observabilidade.py --ambiente dev --alb-dns "$ALB_DNS"

1. Manda N perguntas ao ALB do ambiente, cada uma com um request_id unico.
2. Le do SSM /fiap/trabalho-final-ai/obs-query as URLs de consulta do palco de
   observabilidade (Prometheus, Loki e Jaeger, criados pelo make bootstrap). So
   o runner alcanca essas portas: o teste roda no pipeline, nao no Codespaces.
3. Procura cada sinal com retry (ate 120 s): o SDK envia em lotes de ~1 a 5 s e
   o Prometheus raspa o coletor a cada ~15 s.

E o pedido do Diego: "se eu nao acho o trace de uma pergunta real, a
observabilidade nao existe".
"""

import json
import time
import urllib.parse
import uuid

import pytest

from conftest import (PARAM_OBS_QUERY, esperar, esperar_tg_healthy, falhar, http_json, ler_parametro, log,
                      na_aws)

N_PERGUNTAS = 5
PERGUNTAS = [
    "Meu fone de ouvido parou de funcionar, tem garantia?",
    "Qual o prazo para trocar uma roupa que comprei?",
    "A partir de qual valor o frete é grátis?",
    "Paguei com Pix, em quanto tempo recebo o reembolso?",
    "Quais formas de pagamento a loja aceita?",
]
METRICA = 'qc_rag_perguntas(_total)?'
SERVICO = "qc-rag"


@pytest.fixture(scope="module")
def consultas(request):
    """URLs de consulta: --obs-query (JSON) ou o parametro SSM do palco."""
    bruto = request.config.getoption("--obs-query")
    if not bruto:
        bruto = ler_parametro(request.getfixturevalue("aws"), PARAM_OBS_QUERY)
    try:
        urls = json.loads(bruto)
    except json.JSONDecodeError:
        falhar(f"O parâmetro {PARAM_OBS_QUERY} não é um JSON: rode make bootstrap de novo para recriá-lo.")
    faltando = [k for k in ("prometheus", "loki", "jaeger") if not urls.get(k)]
    if faltando:
        falhar(f"O parâmetro {PARAM_OBS_QUERY} não tem {', '.join(faltando)}: rode make bootstrap de novo.")
    return {k: urls[k].rstrip("/") for k in ("prometheus", "loki", "jaeger")}


def _soma_metrica(prometheus, ambiente):
    consulta = f'sum({{__name__=~"{METRICA}", ambiente="{ambiente}"}})'
    status, corpo, texto = http_json(f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": consulta}),
                                     timeout=10)
    if status != 200 or not isinstance(corpo, dict) or corpo.get("status") != "success":
        raise RuntimeError(f"Prometheus respondeu HTTP {status}: {texto[:200]}")
    resultado = corpo["data"]["result"]
    return float(resultado[0]["value"][1]) if resultado else None


@pytest.fixture(scope="module")
def antes(consultas, ambiente):
    try:
        valor = _soma_metrica(consultas["prometheus"], ambiente)
    except Exception as erro:  # noqa: BLE001
        falhar(
            f"Não consegui consultar o Prometheus em {consultas['prometheus']} a partir do runner: {erro}. "
            "As portas de consulta só abrem para o Security Group do runner: este job roda com tags: [shell]? "
            "O palco qc-obs-tf-ai está ligado (make status)?"
        )
    log(f"  {METRICA}{{ambiente={ambiente}}} antes das perguntas: {valor}")
    return valor or 0.0


@pytest.fixture(scope="module")
def perguntas(request, alb_dns, ambiente, nos_esperados, antes):
    if na_aws(alb_dns):
        esperar_tg_healthy(request.getfixturevalue("aws"), ambiente, nos_esperados, timeout=300)
    url = f"http://{alb_dns}/assistant/ask"
    enviadas = []
    for i in range(N_PERGUNTAS):
        request_id = f"obs-{ambiente}-{uuid.uuid4().hex[:16]}"

        def perguntar(rid=request_id, q=PERGUNTAS[i % len(PERGUNTAS)]):
            status, corpo, texto = http_json(url, {"question": q, "request_id": rid}, timeout=15)
            if status != 200 or not isinstance(corpo, dict):
                raise RuntimeError(f"HTTP {status}: {texto[:150]}")
            return corpo

        corpo, erro = esperar(f"resposta da pergunta {i + 1}", perguntar, 60, intervalo=5)
        if corpo is None:
            falhar(
                f"O ALB {alb_dns} não respondeu POST /assistant/ask em 60 s ({erro}). Antes da observabilidade, "
                f"o smoke-{ambiente} precisa estar verde: veja a aba Tests."
            )
        enviadas.append({"request_id": request_id, "trace_id": corpo.get("trace_id"),
                         "respondido": corpo.get("request_id")})
    log("  perguntas enviadas: " + ", ".join(f"{p['request_id']} (trace {p['trace_id']})" for p in enviadas))
    return enviadas


def test_respostas_trazem_request_id_e_trace_id(perguntas):
    sem_trace = [p["request_id"] for p in perguntas if not p["trace_id"]]
    trocados = [p["request_id"] for p in perguntas if p["respondido"] != p["request_id"]]
    if trocados:
        falhar(f"O assistente devolveu outro request_id para {trocados}: o app/server.py do kit foi alterado?")
    if sem_trace:
        falhar(
            f"As respostas de {sem_trace} vieram sem trace_id: a telemetria do assistente está desligada. "
            "O user_data recebeu otlp_endpoint? Confira no raiz o data aws_ssm_parameter "
            "/fiap/trabalho-final-ai/otlp-endpoint e o otlp_endpoint passado ao module rag_cluster, e no "
            "módulo o otlp_endpoint = var.otlp_endpoint no templatefile."
        )


def test_metrica_no_prometheus(consultas, ambiente, antes, perguntas, request):
    timeout = request.config.getoption("--timeout-obs")
    vistos = {}

    def cresceu():
        vistos["valor"] = _soma_metrica(consultas["prometheus"], ambiente)
        return vistos["valor"] is not None and vistos["valor"] >= antes + len(perguntas)

    ok, erro = esperar(f"{METRICA}{{ambiente={ambiente}}} crescer", cresceu, timeout, intervalo=5)
    if ok:
        return
    if vistos.get("valor") is None:
        falhar(
            f"Depois de {timeout} s, o Prometheus ({consultas['prometheus']}) não tem a métrica "
            f'qc_rag_perguntas_total{{ambiente="{ambiente}"}}{f" (último erro: {erro})" if erro else ""}. '
            "Nenhuma métrica do assistente chegou ao coletor: o user_data recebeu otlp_endpoint? O /health "
            "mostra telemetria = otlp? O Security Group do palco libera a 4318 para a VPC fiap-lab?"
        )
    falhar(
        f'A métrica qc_rag_perguntas_total{{ambiente="{ambiente}"}} existe, mas foi de {antes} para '
        f"{vistos['valor']} depois de {len(perguntas)} perguntas em {timeout} s. As perguntas chegaram a "
        "outra EC2 sem telemetria? Veja o test_respostas_trazem_request_id_e_trace_id."
    )


def _loki_tem(loki, request_id):
    agora = time.time_ns()
    parametros = {
        "query": f'{{service_name="{SERVICO}"}} |= "{request_id}"',
        "start": str(agora - 30 * 60 * 10**9),
        "end": str(agora + 60 * 10**9),
        "limit": "5",
    }
    status, corpo, texto = http_json(f"{loki}/loki/api/v1/query_range?" + urllib.parse.urlencode(parametros),
                                     timeout=10)
    if status != 200 or not isinstance(corpo, dict):
        raise RuntimeError(f"Loki respondeu HTTP {status}: {texto[:200]}")
    return any(stream.get("values") for stream in corpo.get("data", {}).get("result", []))


def test_log_no_loki(consultas, perguntas, request):
    timeout = request.config.getoption("--timeout-obs")
    alvo = perguntas[0]["request_id"]
    ok, erro = esperar(f"log com {alvo} no Loki", lambda: _loki_tem(consultas["loki"], alvo), timeout, intervalo=5)
    if not ok:
        falhar(
            f"Depois de {timeout} s, o Loki ({consultas['loki']}) não tem nenhum log do serviço {SERVICO} com o "
            f"request_id {alvo}{f' (último erro: {erro})' if erro else ''}. O assistente só manda log se a "
            "telemetria estiver ligada: o user_data recebeu otlp_endpoint? Se a métrica chegou e o log não, "
            "o coletor do palco perdeu o pipeline de logs: rode make bootstrap de novo."
        )


def _jaeger_tem(jaeger, trace_id, request_id):
    if trace_id:
        status, corpo, _ = http_json(f"{jaeger}/api/traces/{trace_id}", timeout=10)
        if status == 200 and isinstance(corpo, dict) and corpo.get("data"):
            spans = corpo["data"][0].get("spans", [])
            if spans:
                return True
    tags = json.dumps({"qc.request_id": request_id})
    status, corpo, texto = http_json(
        f"{jaeger}/api/traces?" + urllib.parse.urlencode({"service": SERVICO, "tags": tags, "lookback": "1h",
                                                          "limit": "5"}), timeout=10)
    if status not in (200, 404):
        raise RuntimeError(f"Jaeger respondeu HTTP {status}: {texto[:200]}")
    return bool(isinstance(corpo, dict) and corpo.get("data"))


def test_trace_no_jaeger(consultas, perguntas, request):
    timeout = request.config.getoption("--timeout-obs")
    alvo = perguntas[0]
    ok, erro = esperar(f"trace {alvo['trace_id']} no Jaeger",
                       lambda: _jaeger_tem(consultas["jaeger"], alvo["trace_id"], alvo["request_id"]),
                       timeout, intervalo=5)
    if not ok:
        falhar(
            f"Depois de {timeout} s, nenhum trace com o request_id {alvo['request_id']} (trace_id "
            f"{alvo['trace_id']}) no Jaeger ({consultas['jaeger']}){f' (último erro: {erro})' if erro else ''}. "
            "O user_data recebeu otlp_endpoint? Se a resposta trouxe trace_id, o assistente gerou o trace, "
            "mas ele não chegou ao coletor: confira se o Security Group do palco libera a 4318 para as EC2 "
            "qc-rag-* e se o parâmetro /fiap/trabalho-final-ai/otlp-endpoint aponta para o IP atual do palco."
        )
