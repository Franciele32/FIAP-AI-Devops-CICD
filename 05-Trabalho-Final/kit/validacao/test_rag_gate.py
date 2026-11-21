"""rag-quality-gate: o golden set do kit contra o app/ deste repositorio, offline.

Roda no stage validar, antes de qualquer plan ou deploy:

    pytest validacao/test_rag_gate.py --junitxml rag-quality-gate.xml

Nao precisa de AWS nem de rede: chama o nucleo do assistente (app/core) no
proprio processo, com o corpus de app/corpus, e confere cada caso de
app/evals/golden_v1.jsonl com os limiares de app/evals/thresholds.json. E o
mesmo avaliador da Aula 5 (app/evals/run_eval.py). Uma linha na aba Tests por
caso do golden e uma por limiar: se mexer no corpus derrubar uma politica, o
teste diz qual caso e qual regra.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

from conftest import RAIZ_TERRAFORM, falhar

RELATORIO = RAIZ_TERRAFORM / "rag-quality-gate.json"


def _app_dir(config):
    escolhido = config.getoption("--app-dir")
    return Path(escolhido).resolve() if escolhido else RAIZ_TERRAFORM / "app"


@pytest.fixture(scope="module")
def relatorio(request):
    app = _app_dir(request.config)
    for obrigatorio in ("core/assistant.py", "corpus", "evals/golden_v1.jsonl", "evals/thresholds.json",
                        "evals/run_eval.py"):
        if not (app / obrigatorio).exists():
            falhar(
                f"Não encontrei {app.name}/{obrigatorio}. A pasta app/ do kit precisa estar inteira na raiz "
                "do repositório (o make kit recoloca). Não renomeie nem apague arquivos de app/."
            )
    sys.path.insert(0, str(app))
    for nome in [m for m in sys.modules if m == "core" or m.startswith("core.")]:
        del sys.modules[nome]
    spec = importlib.util.spec_from_file_location("qc_run_eval", app / "evals" / "run_eval.py")
    run_eval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_eval)

    class Args:
        set = "golden_v1"
        target = "local"
        url = None
        seed = 42
        slice = None
        as_of_date = None
        expect_release_id = None
        timeout = 10.0

    try:
        rel = run_eval.run(Args())
    except Exception as erro:  # noqa: BLE001 - qualquer erro reprova com a causa
        falhar(
            f"O avaliador não conseguiu rodar o golden set: {type(erro).__name__}: {erro}. "
            "Algum arquivo de app/ (corpus, config/settings.json ou prompts/templates.json) foi "
            "alterado e ficou inválido? Desfaça a mudança com git diff -- app/."
        )
    RELATORIO.write_text(json.dumps(rel, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    run_eval.print_summary(rel)
    return rel


def _casos():
    caminho = RAIZ_TERRAFORM / "app" / "evals" / "golden_v1.jsonl"
    if not caminho.is_file():
        return ["golden_v1.jsonl ausente"]
    return [json.loads(linha)["case_id"] for linha in caminho.read_text(encoding="utf-8").splitlines()
            if linha.strip()]


@pytest.mark.parametrize("case_id", _casos())
def test_caso_do_golden(relatorio, case_id):
    caso = next((c for c in relatorio["cases"] if c["case_id"] == case_id), None)
    if caso is None:
        falhar(f"O caso {case_id} não foi avaliado: o golden_v1.jsonl de app/evals foi editado?")
    if caso["passed"]:
        return
    falhas = [c for c in caso["checks"] if not c["ok"]]
    detalhes = "; ".join(
        f"{c['name']}: {c['detail'] or 'falhou'}" + (" [REGRA DURA]" if c["hard_rule"] else "") for c in falhas
    ) or (caso.get("error") or "erro desconhecido")
    falhar(
        f"O assistente errou o caso {case_id} (slice {caso['slice']}): {detalhes}. "
        f"Resposta: \"{(caso.get('answer') or '')[:200]}\". Citou: {caso.get('citations') or 'nada'}. "
        "O golden set é o comportamento combinado com a Helena: se você mudou app/corpus, "
        "app/config ou app/core, desfaça com git diff -- app/ e git checkout -- app/<arquivo>."
    )


def _limiares():
    caminho = RAIZ_TERRAFORM / "app" / "evals" / "thresholds.json"
    if not caminho.is_file():
        return ["thresholds.json ausente"]
    metricas = json.loads(caminho.read_text(encoding="utf-8"))["metrics"]
    return [m for m in metricas if m != "slice_pass_rate"] + ["slice_pass_rate", "regras_duras"]


@pytest.mark.parametrize("limiar", _limiares())
def test_limiar_de_qualidade(relatorio, limiar):
    if limiar == "slice_pass_rate":
        ruins = [g for g in relatorio["gates"] if g["name"].startswith("slice.") and not g["ok"]]
        if ruins:
            falhar(
                "Slices abaixo do mínimo: " + "; ".join(g["detail"] for g in ruins)
                + ". Um slice (ex.: politicas, vigencia) afundou mesmo com a média boa: veja os "
                "casos reprovados desse slice nesta mesma aba Tests."
            )
        return
    nome = "regras_duras" if limiar == "regras_duras" else f"metrica.{limiar}"
    gate = next((g for g in relatorio["gates"] if g["name"] == nome), None)
    if gate is None:
        falhar(f"O avaliador não calculou {nome}: app/evals/thresholds.json foi alterado?")
    if not gate["ok"]:
        falhar(
            f"Gate {nome} reprovado: {gate['detail']}. O limiar vem de app/evals/thresholds.json "
            "(baixar o limiar para passar é esconder o problema, não resolver). Veja quais casos do "
            "golden reprovaram nesta aba Tests e corrija a causa em app/."
        )
