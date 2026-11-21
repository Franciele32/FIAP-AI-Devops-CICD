"""Servidor HTTP do Assistente de Rastreio e Politicas (Trabalho Final, porta 8080).

E o mesmo nucleo da Aula 5 (core/: BM25 + composer deterministico, sem LLM),
exposto por http.server da biblioteca padrao: a EC2 nao precisa de framework,
de Docker nem de build. O pacote OpenTelemetry e opcional (requirements-otel.txt):
sem ele, ou sem OTEL_EXPORTER_OTLP_ENDPOINT, o assistente responde igual e so a
telemetria fica desligada.

Endpoints:
  GET  /health           {"status","ambiente","release","corpus_version","telemetria"}
  POST /assistant/ask    {"question", "request_id"?} (ou header X-Request-Id)
                         -> {"answer","citations","abstained","request_id","trace_id",...}

Variaveis de ambiente (o user_data do kit grava no servico systemd):
  AMBIENTE                     dev ou prod (label de metrica, atributo de span, /health)
  RELEASE_ID                   hash curto do bundle publicado pelo modulo app-release
  OTEL_EXPORTER_OTLP_ENDPOINT  http://<coletor>:4318 (vem do SSM, via Terraform)
  PORT                         porta HTTP (padrao 8080)
"""
import json
import os
import re
import socket
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import telemetry, versions  # noqa: E402
from core.assistant import Assistant  # noqa: E402

SERVICE_NAME = "qc-rag"
# request_id vira atributo de span e texto de log: aceitamos so um formato curto
# e previsivel para um cliente nao injetar texto livre na telemetria.
REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_BODY = 16 * 1024


def log(*args):
    print(*args, file=sys.stderr, flush=True)


class Runtime:
    def __init__(self, ambiente, release):
        self.ambiente = ambiente
        self.release = release
        # Identidade publica fixa (cliente da loja): no canal publico o corpo da
        # requisicao nao escolhe tenant, perfil nem data de vigencia.
        self.assistant = Assistant({"environment": ambiente, "release_id": release}, root=HERE)
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def health(self):
        v = self.assistant.versions
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "ambiente": self.ambiente,
            "release": self.release,
            "corpus_version": v["corpus_version"],
            # Hostname da EC2: o smoke confere que o ALB distribui entre as N instancias.
            "host": socket.gethostname(),
            "index_version": v["index_version"],
            "application_version": v["application_version"],
            "telemetria": "otlp" if telemetry.enabled() else "off",
            "started_at": self.started_at,
        }


def make_handler(runtime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuantumAssistant/1.0"

        def log_message(self, fmt, *args):
            # O log de acesso padrao vai para stderr sem request_id; o nosso log
            # por pergunta (telemetry.record_pergunta) e que interessa.
            pass

        def _send(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if isinstance(body, dict) and body.get("request_id"):
                self.send_header("X-Request-Id", body["request_id"])
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                return self._send(200, runtime.health())
            return self._send(404, {"error": "not_found"})

        def do_POST(self):
            if self.path.split("?")[0] == "/assistant/ask":
                return self._ask()
            return self._send(404, {"error": "not_found"})

        def _ler_corpo(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("corpo maior que 16 KB")
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("o corpo precisa ser um objeto JSON")
            return payload

        def _ask(self):
            started = time.monotonic()
            status, body, payload = 200, None, {}
            try:
                payload = self._ler_corpo()
            except (ValueError, UnicodeDecodeError) as err:
                status, body = 400, {"error": f"requisicao invalida: {err}"}
            pedido = str(payload.get("request_id") or self.headers.get("X-Request-Id") or "")
            request_id = pedido if REQUEST_ID_OK.match(pedido) else uuid.uuid4().hex
            with telemetry.server_span(
                "POST /assistant/ask", self.headers,
                **{"http.request.method": "POST", "http.route": "/assistant/ask",
                   "qc.request_id": request_id, "qc.ambiente": runtime.ambiente,
                   "qc.release": runtime.release},
            ) as sp:
                trace_id = sp.trace_id
                question = payload.get("question")
                if body is None and (not isinstance(question, str) or not question.strip()):
                    status, body = 400, {"error": "campo 'question' obrigatorio (texto)"}
                if body is None:
                    try:
                        out = runtime.assistant.answer(
                            question, auth_token=payload.get("auth_token"), order_id=payload.get("order_id"),
                            tenant="quantum", user_role="customer", request_id=request_id)
                        body = {
                            "answer": out["answer"],
                            "citations": out["citations"],
                            "abstained": out["abstained"],
                            "abstain_reason": out["abstain_reason"],
                            "route": out["route"],
                            "request_id": request_id,
                            "trace_id": trace_id or out.get("trace_id"),
                            "ambiente": runtime.ambiente,
                            "release": runtime.release,
                            "corpus_version": out["versions"]["corpus_version"],
                        }
                        sp.set("qc.route", out["route"])
                        sp.set("qc.abstained", out["abstained"])
                        sp.set("qc.citations", len(out["citations"]))
                    except Exception as err:  # noqa: BLE001 - falha vira 500 visivel no trace
                        status, body = 500, {"error": "erro interno", "kind": type(err).__name__}
                        sp.event("exception", type=type(err).__name__)
                body.setdefault("request_id", request_id)
                body.setdefault("trace_id", trace_id)
                sp.set("http.response.status_code", status)
                latency_ms = (time.monotonic() - started) * 1000
                # Dentro do span: o log sai com o trace_id da pergunta.
                telemetry.record_pergunta(
                    runtime.ambiente, body.get("route", "invalida"), body.get("abstained", True), latency_ms,
                    request_id=request_id, status_code=status, release=runtime.release,
                    citations=len(body.get("citations") or []))
            self._send(status, body)

    return Handler


def main():
    ambiente = os.environ.get("AMBIENTE", "local")
    release = os.environ.get("RELEASE_ID", "local")
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    telemetry.configure(SERVICE_NAME, versions.APPLICATION_VERSION, ambiente, release)
    telemetry.warn_if_disabled()
    runtime = Runtime(ambiente, release)
    httpd = ThreadingHTTPServer((host, port), make_handler(runtime))
    h = runtime.health()
    log(f"qc-rag ouvindo em http://{host}:{port} ambiente={ambiente} release={release} "
        f"corpus={h['corpus_version']} telemetria={h['telemetria']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
