"""Contrato do Terraform do Trabalho Final, verificado sobre o plan em JSON.

Roda no job contrato-<env> do pipeline, depois do terraform init e do plan:

    terraform show -json plan-dev.tfplan > plan-dev.json
    pytest validacao/test_contrato.py --ambiente dev --plan plan-dev.json

Nada e criado na AWS: o teste le o plan (configuration + planned_values) e o
.terraform/terraform.tfstate que o init grava com o backend em uso. Le tambem o
.gitlab-ci.yml da raiz: o pipeline verde so prova o trabalho se os jobs de teste
estiverem nele e se cada job do aluno fizer o minimo. E le o git ls-files: state
e plano nao podem estar versionados. Cada funcao e uma linha na aba Tests do
GitLab e a mensagem diz o que corrigir.
"""

import fnmatch
import re
import shutil
import subprocess

import pytest

from conftest import RAIZ_TERRAFORM, falhar

# Sem PyYAML, os testes do plan continuam rodando; so os do pipeline reprovam,
# com a mensagem dizendo o que instalar.
try:
    import yaml
except ImportError:
    yaml = None

FONTE_MODULO = "./modules/rag-cluster"
FONTE_RELEASE = "./modules/app-release"
PARAM_OTLP = "/fiap/trabalho-final-ai/otlp-endpoint"
MARCADOR_TEMPLATE = "qc-rag-kit-user-data"
PREFIXO_BUCKET = "base-config-"
KEY_STATE = "trabalho-final-ai/terraform.tfstate"
REGIAO = "us-east-1"

# Nomes locais da demo Count: os outros testes (e o kit) procuram por eles.
RECURSOS_DA_DEMO = [
    "aws_instance.web",
    "aws_lb.web",
    "aws_lb_target_group.web",
    "aws_lb_listener.web",
    "aws_security_group.web",
    "aws_lb_target_group_attachment.web",
]


# ---------- leitura do plan ----------


def _modulo_chamado(plano):
    """(nome, bloco) do module call com source ./modules/rag-cluster no raiz."""
    chamadas = plano.get("configuration", {}).get("root_module", {}).get("module_calls", {})
    for nome, chamada in chamadas.items():
        if chamada.get("source", "").rstrip("/") == FONTE_MODULO:
            return nome, chamada
    fontes = ", ".join(f'{n} (source = "{c.get("source")}")' for n, c in chamadas.items())
    falhar(
        f'O raiz não chama o módulo com source = "{FONTE_MODULO}". '
        f"Encontrado: {fontes or 'nenhum bloco module'}. "
        f'Esperado: module "rag_cluster" {{ source = "{FONTE_MODULO}" ... }} no main.tf do raiz. '
        "Corrija no main.tf/outputs.tf da raiz."
    )


def _config_do_modulo(plano):
    return _modulo_chamado(plano)[1].get("module", {})


def _recurso_config(plano, endereco):
    for recurso in _config_do_modulo(plano).get("resources", []):
        if recurso.get("address") == endereco:
            return recurso
    falhar(
        f"O módulo {FONTE_MODULO} não tem o recurso {endereco}. Mantenha os nomes locais "
        "dos recursos da demo Count (aws_instance.web, aws_lb.web, ...). Corrija no módulo (modules/rag-cluster/*.tf)."
    )


def _recursos_planejados(plano):
    """Recursos gerenciados do modulo em planned_values: o estado apos o apply,
    incluindo o que ja existe e nao muda (pipelines seguintes no mesmo ambiente)."""
    nome, _ = _modulo_chamado(plano)
    endereco = f"module.{nome}"
    recursos = []
    pendentes = list(plano.get("planned_values", {}).get("root_module", {}).get("child_modules", []))
    while pendentes:
        filho = pendentes.pop()
        if filho.get("address") == endereco or filho.get("address", "").startswith(endereco + "["):
            recursos += [r for r in filho.get("resources", []) if r.get("mode") == "managed"]
        pendentes += filho.get("child_modules", [])
    return recursos


def _planejados(plano, tipo, nome="web"):
    return sorted(
        (r for r in _recursos_planejados(plano) if r.get("type") == tipo and r.get("name") == nome),
        key=lambda r: r.get("index") if isinstance(r.get("index"), int) else -1,
    )


def _unico(plano, tipo, rotulo):
    encontrados = _planejados(plano, tipo)
    if len(encontrados) != 1:
        falhar(
            f"O plan deveria ter exatamente 1 {rotulo} ({tipo}.web), mas tem {len(encontrados)}. "
            "Mantenha o recurso da demo Count, sem count. Corrija no módulo (modules/rag-cluster/*.tf)."
        )
    return encontrados[0]["values"]


def _origem_do_nome(plano, endereco, atributo):
    """None se a expressao do nome acompanha o workspace; senao, de onde ela vem.
    local.* passa sem conferir: o plan nao traz a expressao dos locals."""
    recurso = next(
        (r for r in _config_do_modulo(plano).get("resources", []) if r.get("address") == endereco),
        None,
    )
    expressao = (recurso or {}).get("expressions", {}).get(atributo)
    if not expressao:
        # Recurso ou atributo ausente: os testes de nome ja reprovam.
        return None
    if "constant_value" in expressao:
        return f"é o valor fixo {expressao['constant_value']}"

    def segue(referencias):
        return "terraform.workspace" in referencias or any(r.startswith("local.") for r in referencias)

    referencias = _referencias(expressao)
    if segue(referencias):
        return None
    _, chamada = _modulo_chamado(plano)
    variaveis = _config_do_modulo(plano).get("variables", {})
    descricoes = []
    for ref in referencias:
        if not ref.startswith("var."):
            continue
        variavel = ref.split(".")[1]
        passado = chamada.get("expressions", {}).get(variavel)
        if passado is not None and segue(_referencias(passado)):
            return None
        if passado is not None:
            vindo = passado.get("constant_value", _referencias(passado))
            descricoes.append(f"{ref}, que o raiz passa como {vindo}")
        elif variaveis.get(variavel, {}).get("default") is not None:
            default = variaveis[variavel]["default"]
            descricoes.append(f'{ref}, com default "{default}" e que o raiz não passa')
        else:
            descricoes.append(ref)
    return f"usa {', '.join(descricoes) or ', '.join(referencias) or 'outra expressão'}"


def _dica_workspace(plano, endereco, atributo, encontrado, ambiente):
    """Complemento da mensagem quando um nome por workspace saiu errado. Se a
    expressao do nome nao segue o workspace, o culpado e o codigo, e nao o
    workspace em que o plan rodou."""
    if not isinstance(encontrado, str):
        return ""
    origem = _origem_do_nome(plano, endereco, atributo)
    if origem:
        campo = "A tag Name" if atributo == "tags" else "O name"
        return (
            f" {campo} de {endereco} não usa terraform.workspace: {origem}. Por isso o nome não "
            f"acompanha o workspace {ambiente} selecionado no plan-{ambiente}. Troque por "
            "${terraform.workspace} no próprio recurso."
        )
    outro = "prod" if ambiente == "dev" else "dev"
    if f"-{outro}" in encontrado:
        return (
            f" O nome tem '{outro}': o job plan-{ambiente} gerou o plan em outro workspace. "
            f"Ele precisa rodar terraform workspace select {ambiente} || terraform workspace new "
            f"{ambiente} antes do plan (no .gitlab-ci.yml)."
        )
    if "-default" in encontrado:
        return f" O nome tem 'default': o plan rodou no workspace default, não em {ambiente} (na seleção de workspace dos jobs plan-<env>)."
    return ""


def _referencias(expressao):
    return (expressao or {}).get("references", [])


# ---------- backend.tf da raiz ----------


def test_backend_e_s3(backend):
    tipo = backend.get("type")
    if tipo != "s3":
        falhar(
            f'O backend em uso é "{tipo}", mas o state do trabalho precisa ficar no S3. '
            'Esperado: terraform { backend "s3" { ... } } no backend.tf do raiz, seguido de '
            "terraform init. Corrija no backend.tf da raiz."
        )


def test_backend_bucket(backend):
    bucket = (backend.get("config") or {}).get("bucket") or ""
    if not bucket.startswith(PREFIXO_BUCKET):
        falhar(
            f'O bucket do backend é "{bucket}", mas deveria ser o seu {PREFIXO_BUCKET}<RM> '
            "(o mesmo das demos de state). Corrija no backend.tf da raiz."
        )


def test_backend_key(backend):
    key = (backend.get("config") or {}).get("key")
    if key != KEY_STATE:
        falhar(
            f'A key do backend é "{key}", mas deveria ser "{KEY_STATE}". Com essa key, os '
            "workspaces dev e prod ficam em env:/dev/... e env:/prod/... no bucket. "
            "Corrija no backend.tf da raiz."
        )


def test_backend_regiao(backend):
    regiao = (backend.get("config") or {}).get("region")
    if regiao != REGIAO:
        falhar(f'A region do backend é "{regiao}", mas deveria ser "{REGIAO}". Corrija no backend.tf da raiz.')


def test_backend_use_lockfile(backend):
    lock = (backend.get("config") or {}).get("use_lockfile")
    if lock is not True:
        falhar(
            f"O backend está com use_lockfile = {lock}, mas deveria ter use_lockfile = true "
            "para dois pipelines não aplicarem no mesmo state ao mesmo tempo. Corrija no backend.tf da raiz."
        )


# ---------- nada de state nem plano no Git ----------


def _versionado_proibido(caminho):
    partes = caminho.split("/")
    arquivo = partes[-1]
    return (
        ".terraform" in partes[:-1]
        or arquivo.endswith((".tfstate", ".tfplan"))
        or ".tfstate." in arquivo
        or fnmatch.fnmatch(arquivo, "plan-*.json")
    )


def test_git_sem_state_nem_plano_versionado():
    # Um plan-<env>.tfplan versionado deixaria o pipeline verde mesmo com o job
    # plan-<env> vazio: o contrato e o deploy leriam o arquivo do checkout.
    git = shutil.which("git")
    if git is None:
        pytest.skip("o git não está instalado aqui; sem ele não há como listar os arquivos versionados")
    resultado = subprocess.run(
        [git, "ls-files", "-z"], cwd=RAIZ_TERRAFORM, capture_output=True, text=True
    )
    if resultado.returncode != 0:
        pytest.skip(f"{RAIZ_TERRAFORM} não é um repositório git ({' '.join(resultado.stderr.split())})")
    proibidos = sorted(c for c in resultado.stdout.split("\0") if c and _versionado_proibido(c))
    if proibidos:
        lista = ", ".join(proibidos[:10]) + (f" e mais {len(proibidos) - 10}" if len(proibidos) > 10 else "")
        falhar(
            f"Estão versionados no Git: {lista}. Esperado: nenhum *.tfstate, *.tfplan, plan-*.json "
            "nem arquivo de .terraform/ no git ls-files. O state vive no S3 (e guarda dados "
            "sensíveis em texto claro), o plano é artefato do pipeline e a .terraform/ é recriada "
            "por cada terraform init. Tire do Git sem apagar a cópia local com "
            "git rm -r --cached <caminho>, confira o .gitignore do kit e faça "
            "um novo commit. Corrija no backend.tf da raiz."
        )


# ---------- raiz (main.tf e outputs.tf) ----------


def test_provider_aws_no_raiz(plano):
    providers = plano.get("configuration", {}).get("provider_config", {})
    aws = providers.get("aws")
    if not aws or aws.get("module_address") or not aws.get("expressions"):
        falhar(
            'O raiz não declara o provider "aws" (ou o bloco está vazio). Esperado no main.tf '
            'do raiz: provider "aws" { region = "us-east-1" }. Corrija no main.tf/outputs.tf da raiz.'
        )


def test_provider_regiao(plano):
    aws = plano.get("configuration", {}).get("provider_config", {}).get("aws") or {}
    regiao = aws.get("expressions", {}).get("region")
    if regiao is None:
        falhar(
            'O provider "aws" do raiz não define region. Esperado: provider "aws" { region = '
            f'"{REGIAO}" }} no main.tf do raiz. Corrija no main.tf/outputs.tf da raiz.'
        )
    if regiao.get("constant_value") != REGIAO:
        encontrado = regiao.get("constant_value") or f"a expressão {_referencias(regiao)}"
        falhar(
            f"A region do provider aws no raiz é {encontrado}, mas deveria ser fixa: "
            f'region = "{REGIAO}". Corrija no main.tf/outputs.tf da raiz.'
        )


def test_modulo_sem_bloco_provider(plano):
    nome, _ = _modulo_chamado(plano)
    providers = plano.get("configuration", {}).get("provider_config", {})
    # Entradas do modulo aparecem so pelo required_providers (sem expressions);
    # com expressions, ha um bloco provider configurado dentro do modulo.
    dentro = [
        f'provider "{p.get("name")}"'
        for p in providers.values()
        if p.get("module_address") and p.get("expressions")
    ]
    if dentro:
        falhar(
            f"O módulo {FONTE_MODULO} (module.{nome}) tem bloco {', '.join(dentro)}. "
            'O provider fica só no raiz; apague o bloco provider "aws" do main.tf do módulo '
            "(o versions.tf com required_providers pode ficar). Corrija no módulo (modules/rag-cluster/*.tf)."
        )


def test_raiz_chama_o_modulo(plano):
    _modulo_chamado(plano)


def test_raiz_sem_recursos_fora_do_modulo(plano):
    # Com a Count copiada tambem para o raiz (e nomes diferentes), o contrato do
    # modulo passaria e o pipeline publicaria uma segunda frota fora dele.
    nome, _ = _modulo_chamado(plano)
    raiz = plano.get("configuration", {}).get("root_module", {})
    recursos = {r.get("address") for r in raiz.get("resources", []) if r.get("mode") == "managed"}
    recursos |= {
        f"{r.get('type')}.{r.get('name')}"
        for r in plano.get("planned_values", {}).get("root_module", {}).get("resources", [])
        if r.get("mode") == "managed"
    }
    outros = [
        f'module "{n}" (source = "{c.get("source")}")'
        for n, c in raiz.get("module_calls", {}).items()
        if n != nome and c.get("source", "").rstrip("/") != FONTE_RELEASE
    ]
    if recursos or outros:
        encontrado = ", ".join(sorted(recursos) + outros)
        falhar(
            f"O raiz declara, além dos modules {nome} e app-release: {encontrado}. A frota (EC2, ALB, TG, SG) "
            "fica dentro de modules/rag-cluster; no raiz ficam só o provider, o data aws_ssm_parameter do "
            f'OTLP, o module "{nome}", o module do kit {FONTE_RELEASE}, os outputs e o backend.tf. '
            "Apague esses blocos do main.tf da raiz: o módulo já cria cada um deles."
        )


def _locals_do_raiz(plano):
    """{nome: (texto, referencias)} dos locals do raiz. O show -json do Terraform
    1.10 nao exporta locals; sem eles no plan, le os blocos locals dos .tf do raiz."""
    no_plan = plano.get("configuration", {}).get("root_module", {}).get("locals")
    if no_plan:
        return {n: ("", _referencias(e)) for n, e in no_plan.items()}
    encontrados = {}
    for arquivo in sorted(RAIZ_TERRAFORM.glob("*.tf")):
        # Comentario so depois de espaco: "https://..." dentro de string fica.
        texto = re.sub(r"(?m)(^|\s)(#|//)[^\n]*", "", arquivo.read_text(encoding="utf-8"))
        for inicio in re.finditer(r"(?m)^\s*locals\s*\{", texto):
            nivel, atual = 0, None
            for linha in texto[inicio.end() :].split("\n"):
                if nivel == 0 and linha.strip().startswith("}"):
                    break
                # Nome novo so no primeiro nivel: chaves de um mapa nao sao locals.
                nome = re.match(r"\s*(\w+)\s*=(?!=)", linha) if nivel == 0 else None
                if nome:
                    atual = nome.group(1)
                    encontrados[atual] = linha[nome.end() :]
                elif atual:
                    encontrados[atual] += "\n" + linha
                nivel += sum(linha.count(c) for c in "{[(") - sum(linha.count(c) for c in "}])")
                if nivel < 0:
                    break
    refs = re.compile(r"\bterraform\.workspace|\blocal\.\w+|\bvar\.\w+")
    return {nome: (" ".join(texto.split()), refs.findall(texto)) for nome, texto in encontrados.items()}


def _depende_do_workspace(referencias, locais, vistos=()):
    """True/False se a expressao segue (ou nao) o workspace, direto ou por locals;
    None se cita um local que o teste nao achou (o efeito no plan decide)."""
    if "terraform.workspace" in referencias:
        return True
    resultado = False
    for ref in referencias:
        if not ref.startswith("local.") or ref in vistos:
            continue
        nome = ref.split(".")[1]
        if nome not in locais:
            resultado = None
            continue
        if _depende_do_workspace(locais[nome][1], locais, vistos + (ref,)):
            return True
    return resultado


def test_node_count_do_raiz_usa_workspace(plano):
    # Confere a origem da expressao; o efeito (dev 1, prod 2) e o test_quantidade_de_ec2.
    nome, chamada = _modulo_chamado(plano)
    expressao = chamada.get("expressions", {}).get("node_count")
    if expressao is None:
        falhar(
            f"O module {nome} não passa node_count. Esperado no raiz: "
            'node_count = terraform.workspace == "prod" ? 2 : 1 (direto ou por um local). '
            "Corrija no main.tf/outputs.tf da raiz."
        )
    referencias = _referencias(expressao)
    locais = _locals_do_raiz(plano)
    if _depende_do_workspace(referencias, locais) is False:
        if "constant_value" in expressao:
            encontrado = f"o valor fixo {expressao['constant_value']}"
        else:
            via = [
                f"{r} = {locais[r.split('.')[1]][0] or locais[r.split('.')[1]][1]}"
                for r in referencias
                if r.startswith("local.") and r.split(".")[1] in locais
            ]
            encontrado = ", ".join(via) or ", ".join(referencias)
        falhar(
            f"O node_count do module {nome} é {encontrado}, mas precisa depender do workspace. "
            'Use node_count = terraform.workspace == "prod" ? 2 : 1, direto no bloco module ou '
            "num local (locals { nos = terraform.workspace == \"prod\" ? 2 : 1 } e "
            "node_count = local.nos). Variável e tfvars não servem: o workspace selecionado "
            "é que decide o ambiente. Corrija no main.tf/outputs.tf da raiz."
        )


def test_output_alb_dns_no_raiz(plano):
    nome, _ = _modulo_chamado(plano)
    saidas = plano.get("configuration", {}).get("root_module", {}).get("outputs", {})
    if "alb_dns" not in saidas:
        falhar(
            f"O raiz não declara o output alb_dns. Encontrado: {sorted(saidas) or 'nenhum output'}. "
            f"Esperado: output \"alb_dns\" {{ value = module.{nome}.alb_dns }}. O smoke do "
            "pipeline lê esse output. Corrija no main.tf/outputs.tf da raiz."
        )
    referencias = _referencias(saidas["alb_dns"].get("expression"))
    if f"module.{nome}.alb_dns" not in referencias:
        falhar(
            f"O output alb_dns do raiz aponta para {referencias or 'um valor fixo'}, mas deveria "
            f"ser value = module.{nome}.alb_dns. Corrija no main.tf/outputs.tf da raiz."
        )


# ---------- modulo rag-cluster ----------


@pytest.mark.parametrize("endereco", RECURSOS_DA_DEMO)
def test_modulo_mantem_recurso_da_demo(plano, endereco):
    _recurso_config(plano, endereco)


def test_modulo_declara_node_count(plano):
    variaveis = _config_do_modulo(plano).get("variables", {})
    if "node_count" not in variaveis:
        falhar(
            f"O módulo {FONTE_MODULO} não declara a variável node_count. Esperado no "
            'variables.tf do módulo: variable "node_count" { type = number }. '
            f"Variáveis encontradas: {sorted(variaveis)}. Corrija no módulo (modules/rag-cluster/*.tf)."
        )


def test_node_count_sem_default(plano):
    variavel = _config_do_modulo(plano).get("variables", {}).get("node_count")
    # Sem a variavel, test_modulo_declara_node_count ja reprova.
    if variavel is not None and variavel.get("default") is not None:
        falhar(
            f"A variável node_count do módulo tem default = {variavel['default']}, mas deveria "
            "ser declarada sem default. Com default, um raiz que esquece de passar node_count "
            "planeja a frota errada em silêncio; sem default, o Terraform reclama. Apague a linha "
            "default do variable \"node_count\" no variables.tf do módulo. Corrija no módulo (modules/rag-cluster/*.tf)."
        )


def test_count_da_ec2_usa_node_count(plano):
    recurso = _recurso_config(plano, "aws_instance.web")
    contagem = recurso.get("count_expression")
    if contagem is None or "var.node_count" not in _referencias(contagem):
        encontrado = "sem count" if contagem is None else contagem.get("constant_value", _referencias(contagem))
        falhar(
            f"O count de aws_instance.web é {encontrado}, mas deveria ser count = var.node_count "
            "(a demo 01-Terraform/demos/03-Count tinha count = 2 fixo). Corrija no módulo (modules/rag-cluster/*.tf)."
        )


def test_output_alb_dns_no_modulo(plano):
    saidas = _config_do_modulo(plano).get("outputs", {})
    if "alb_dns" not in saidas:
        falhar(
            f"O módulo não declara o output alb_dns. Encontrado: {sorted(saidas)}. Renomeie o "
            "output alb_public da demo para alb_dns. Corrija no módulo (modules/rag-cluster/*.tf)."
        )
    referencias = _referencias(saidas["alb_dns"].get("expression"))
    if "aws_lb.web.dns_name" not in referencias:
        falhar(
            f"O output alb_dns do módulo aponta para {referencias or 'um valor fixo'}, mas deveria "
            "ser value = aws_lb.web.dns_name. Corrija no módulo (modules/rag-cluster/*.tf)."
        )


def test_attachments_acompanham_a_frota(plano):
    # Compara com as EC2 do proprio plan (nao com o tamanho esperado do ambiente):
    # um node_count errado ja reprova test_quantidade_de_ec2, sem repetir aqui.
    instancias = len(_planejados(plano, "aws_instance"))
    attachments = len(_planejados(plano, "aws_lb_target_group_attachment"))
    if attachments != instancias:
        falhar(
            f"O plan registra {attachments} EC2 no Target Group, mas cria {instancias} EC2. "
            "O count de aws_lb_target_group_attachment.web deve ser length(aws_instance.web), "
            "como na demo Count. Corrija no módulo (modules/rag-cluster/*.tf)."
        )


# ---------- frota por workspace (dev 1, prod 2) ----------


def test_quantidade_de_ec2(plano, ambiente, nos_esperados):
    instancias = _planejados(plano, "aws_instance")
    if len(instancias) != nos_esperados:
        falhar(
            f"O plan de {ambiente} tem {len(instancias)} EC2, mas {ambiente} deveria ter "
            f"{nos_esperados} (dev = 1, prod = 2). Confira o workspace selecionado antes do plan "
            '(na seleção de workspace dos jobs plan-<env>) e a condicional node_count = terraform.workspace == "prod" ? 2 : 1 '
            "no raiz (no main.tf/outputs.tf da raiz)."
        )


def test_nomes_das_ec2(plano, ambiente, nos_esperados):
    nomes = [(r["values"].get("tags") or {}).get("Name") for r in _planejados(plano, "aws_instance")]
    esperados = [f"qc-rag-{ambiente}-{i:03d}" for i in range(1, nos_esperados + 1)]
    # A quantidade e cobrada em test_quantidade_de_ec2; aqui so o padrao do nome,
    # para um erro de count nao aparecer duas vezes na aba Tests.
    errados = [n for i, n in enumerate(nomes) if n != f"qc-rag-{ambiente}-{i + 1:03d}"]
    if not nomes or errados:
        dica = _dica_workspace(plano, "aws_instance.web", "tags", errados[0] if errados else None, ambiente)
        falhar(
            f"As tags Name das EC2 são {nomes}, mas deveriam ser {esperados}. Use "
            'Name = format("qc-rag-%s-%03d", var.ambiente, count.index + 1). '
            f"Corrija no main.tf do módulo rag-cluster.{dica}"
        )


# ---------- ALB, Target Group e Security Group ----------


def test_nome_do_alb(plano, ambiente):
    nome = _unico(plano, "aws_lb", "ALB").get("name")
    if nome != f"qc-rag-alb-{ambiente}":
        dica = _dica_workspace(plano, "aws_lb.web", "name", nome, ambiente)
        falhar(
            f'O ALB (aws_lb.web) se chama "{nome}", mas deveria ser "qc-rag-alb-{ambiente}". Use '
            'name = "qc-rag-alb-${var.ambiente}". '
            f"Corrija no módulo rag-cluster (main.tf e securitygroup.tf).{dica}"
        )


def test_nome_do_target_group(plano, ambiente):
    nome = _unico(plano, "aws_lb_target_group", "Target Group").get("name")
    if nome != f"qc-rag-tg-{ambiente}":
        dica = _dica_workspace(plano, "aws_lb_target_group.web", "name", nome, ambiente)
        falhar(
            f'O Target Group (aws_lb_target_group.web) se chama "{nome}", mas deveria ser '
            f'"qc-rag-tg-{ambiente}". Use name = "qc-rag-tg-${{var.ambiente}}". '
            f"Corrija no módulo rag-cluster (main.tf e securitygroup.tf).{dica}"
        )


def test_nome_do_security_group(plano, ambiente):
    nome = _unico(plano, "aws_security_group", "Security Group").get("name")
    if nome != f"qc-rag-{ambiente}":
        dica = _dica_workspace(plano, "aws_security_group.web", "name", nome, ambiente)
        falhar(
            f'O Security Group (aws_security_group.web) se chama "{nome}", mas deveria ser '
            f'"qc-rag-{ambiente}". Use name = "qc-rag-${{var.ambiente}}". '
            f"Corrija no módulo rag-cluster (main.tf e securitygroup.tf).{dica}"
        )


def test_tag_name_do_security_group(plano, ambiente):
    tag = (_unico(plano, "aws_security_group", "Security Group").get("tags") or {}).get("Name")
    if tag != f"qc-rag-{ambiente}":
        falhar(
            f'A tag Name do Security Group é "{tag}", mas deveria ser "qc-rag-{ambiente}", '
            "igual ao nome (é o que aparece na coluna Name do console). Corrija no módulo rag-cluster (main.tf e securitygroup.tf)."
        )


NOMES_POR_WORKSPACE = [
    ("aws_instance.web", "tags", "a tag Name"),
    ("aws_lb.web", "name", "o name"),
    ("aws_lb_target_group.web", "name", "o name"),
    ("aws_security_group.web", "name", "o name"),
    ("aws_security_group.web", "tags", "a tag Name"),
]


def test_nomes_seguem_o_workspace(plano):
    # Os testes de nome conferem o valor; este confere de onde ele vem. Um var.env
    # com -var env=prod no plan-prod acertaria os nomes sem seguir o workspace.
    errados = [
        f"{campo} de {endereco} {origem}"
        for endereco, atributo, campo in NOMES_POR_WORKSPACE
        if (origem := _origem_do_nome(plano, endereco, atributo))
    ]
    if errados:
        falhar(
            f"Nomes que não seguem o workspace: {'; '.join(errados)}. Esperado: terraform.workspace "
            'direto no nome, como format("qc-rag-%s-%03d", var.ambiente, count.index + 1) e '
            '"qc-rag-alb-${var.ambiente}" (ou uma variável do módulo que o raiz preencha com '
            "terraform.workspace). Assim o workspace selecionado decide o nome, sem -var nem "
            "tfvars. Corrija no main.tf do módulo rag-cluster."
        )


def test_security_group_sem_prefixo_sg(plano):
    nome = _unico(plano, "aws_security_group", "Security Group").get("name") or ""
    if nome.startswith("sg-"):
        falhar(
            f'O Security Group se chama "{nome}": a AWS reserva o prefixo "sg-" para os IDs e '
            "recusa esse nome no apply. Use qc-rag-<workspace>. Corrija no módulo rag-cluster (main.tf e securitygroup.tf)."
        )


# ---------- Trabalho Final: release do app, OTLP e user_data do kit ----------


def _modulo_release(plano):
    chamadas = plano.get("configuration", {}).get("root_module", {}).get("module_calls", {})
    for nome, chamada in chamadas.items():
        if chamada.get("source", "").rstrip("/") == FONTE_RELEASE:
            return nome, chamada
    falhar(
        f'O raiz não chama o módulo do kit {FONTE_RELEASE}. Sem ele, o app não é empacotado nem publicado '
        "no S3, e as EC2 não têm o que instalar. Esperado no main.tf da raiz: "
        f'module "app_release" {{ source = "{FONTE_RELEASE}" ... }} com ambiente = terraform.workspace '
        "e bucket = o seu base-config-<RM>."
    )


def test_raiz_chama_o_modulo_app_release(plano):
    _modulo_release(plano)


def test_app_release_segue_o_workspace(plano):
    nome, chamada = _modulo_release(plano)
    expressao = chamada.get("expressions", {}).get("ambiente")
    # Direto ou por um local (locals { ambiente = terraform.workspace }): segue o workspace.
    if expressao is None or _depende_do_workspace(_referencias(expressao), _locals_do_raiz(plano)) is False:
        encontrado = "nada" if expressao is None else expressao.get("constant_value", _referencias(expressao))
        falhar(
            f"O module {nome} recebe ambiente = {encontrado}, mas deveria ser ambiente = terraform.workspace: "
            "é o que separa o pacote de dev do de prod no S3 (releases/<env>/). Corrija no main.tf da raiz."
        )


def test_rag_cluster_recebe_o_release(plano):
    nome, chamada = _modulo_chamado(plano)
    release, _ = _modulo_release(plano)
    errados = []
    for variavel in ("bundle_uri", "release_id"):
        refs = _referencias(chamada.get("expressions", {}).get(variavel))
        if f"module.{release}.{variavel}" not in refs:
            errados.append(f"{variavel} = {refs or 'nada'} (esperado module.{release}.{variavel})")
    ambiente = _referencias(chamada.get("expressions", {}).get("ambiente"))
    if not ambiente or _depende_do_workspace(ambiente, _locals_do_raiz(plano)) is False:
        errados.append(
            f"ambiente = {ambiente or 'nada'} (esperado terraform.workspace, direto ou por um local)"
        )
    if errados:
        falhar(
            f"O module {nome} não recebe o que precisa: {'; '.join(errados)}. Corrija os argumentos do bloco "
            f'module "{nome}" no main.tf da raiz.'
        )


def _data_otlp(plano):
    raiz = plano.get("configuration", {}).get("root_module", {})
    for recurso in raiz.get("resources", []):
        if recurso.get("mode") == "data" and recurso.get("type") == "aws_ssm_parameter":
            nome = (recurso.get("expressions", {}).get("name") or {}).get("constant_value")
            if nome == PARAM_OTLP:
                return recurso["address"]
    return None


def test_otlp_lido_do_ssm(plano):
    nome, chamada = _modulo_chamado(plano)
    endereco = _data_otlp(plano)
    if endereco is None:
        falhar(
            f'O raiz não lê o parâmetro {PARAM_OTLP}. Esperado no main.tf da raiz: data "aws_ssm_parameter" '
            f'"otlp" {{ name = "{PARAM_OTLP}" }}. O make bootstrap grava ali o endereço do coletor; um IP '
            "escrito no código quebra quando o palco de observabilidade é recriado."
        )
    refs = _referencias(chamada.get("expressions", {}).get("otlp_endpoint"))
    if not any(r == endereco or r.startswith(endereco + ".") for r in refs):
        falhar(
            f"O module {nome} recebe otlp_endpoint = {refs or 'nada'}, mas deveria receber o valor de "
            f"{endereco} (por exemplo nonsensitive({endereco}.value)). Corrija no main.tf da raiz."
        )


def test_output_release_id_no_raiz(plano):
    release, _ = _modulo_release(plano)
    saidas = plano.get("configuration", {}).get("root_module", {}).get("outputs", {})
    refs = _referencias((saidas.get("release_id") or {}).get("expression"))
    if f"module.{release}.release_id" not in refs:
        falhar(
            f"O raiz não tem output \"release_id\" {{ value = module.{release}.release_id }} (encontrado: "
            f"{refs or 'nenhum'}). O smoke compara esse valor com o release que o /health responde. "
            "Corrija no outputs.tf da raiz."
        )


def test_user_data_vem_do_template_do_kit(plano, ambiente):
    recurso = _recurso_config(plano, "aws_instance.web")
    refs = _referencias(recurso.get("expressions", {}).get("user_data"))
    instancias = _planejados(plano, "aws_instance")
    textos = [r["values"].get("user_data") for r in instancias]
    # Com o otlp sensivel, o plan esconde o user_data (valor ausente): pede o nonsensitive.
    if instancias and all(t is None for t in textos):
        falhar(
            "O plan esconde o user_data das EC2 (valor sensível), e o contrato não consegue conferir que "
            "ele veio do template do kit. Passe otlp_endpoint = nonsensitive(data.aws_ssm_parameter.otlp.value) "
            "no main.tf da raiz: o endereço do coletor não é segredo."
        )
    sem_marcador = [i + 1 for i, t in enumerate(textos) if t is not None and MARCADOR_TEMPLATE not in t]
    if sem_marcador or not instancias:
        falhar(
            f"O user_data de aws_instance.web não é o template do kit (refs: {refs or 'nenhuma'}). Esperado no "
            "main.tf do módulo rag-cluster: user_data = templatefile(\"${path.module}/../../templates/"
            "user_data.sh.tftpl\", { ambiente, release_id, bundle_uri, otlp_endpoint, aws_region })."
        )
    faltando = []
    for texto in textos:
        if f"AMBIENTE={ambiente}\n" not in texto:
            faltando.append(f"AMBIENTE={ambiente}")
        if "OTEL_EXPORTER_OTLP_ENDPOINT=http" not in texto:
            faltando.append("OTEL_EXPORTER_OTLP_ENDPOINT=http://...")
        copia = re.search(r"aws s3 cp [^\n]*", texto)
        if not copia or "s3://" not in copia.group(0):
            faltando.append("bundle_uri s3://...")
    if faltando:
        falhar(
            f"O user_data renderizado não tem {', '.join(sorted(set(faltando)))}. Confira os valores passados ao "
            "templatefile no módulo: ambiente = var.ambiente, otlp_endpoint = var.otlp_endpoint, "
            "bundle_uri = var.bundle_uri."
        )


def test_troca_de_release_recria_as_ec2(plano):
    # create_before_destroy fica como boa pratica no enunciado: o plan JSON do
    # Terraform 1.10.5 nao traz o bloco lifecycle, entao o contrato nao o exige.
    _recurso_config(plano, "aws_instance.web")
    instancias = _planejados(plano, "aws_instance")
    troca = [r["values"].get("user_data_replace_on_change") for r in instancias]
    if not instancias or not all(v is True for v in troca):
        falhar(
            f"aws_instance.web está com user_data_replace_on_change = {troca[0] if troca else 'ausente'}, mas "
            "precisa de user_data_replace_on_change = true no main.tf do módulo rag-cluster. Sem isso, um "
            "bundle novo muda só o atributo user_data e as EC2 seguem rodando a versão antiga do assistente "
            "(o user_data só roda no primeiro boot)."
        )


def test_target_group_na_8080_com_health(plano):
    tg = _unico(plano, "aws_lb_target_group", "Target Group")
    health = (tg.get("health_check") or [{}])[0]
    faltas = []
    if tg.get("port") != 8080:
        faltas.append(f"port = 8080 (está {tg.get('port')})")
    if health.get("path") != "/health":
        faltas.append(f"health_check {{ path = \"/health\" }} (está {health.get('path')})")
    if faltas:
        falhar(
            f"O aws_lb_target_group.web precisa de {' e '.join(faltas)}: o assistente escuta na 8080 e só "
            "responde /health depois de instalado. Corrija no main.tf do módulo rag-cluster."
        )


def test_ec2_com_lab_instance_profile(plano):
    errados = [r["values"].get("iam_instance_profile") for r in _planejados(plano, "aws_instance")
               if r["values"].get("iam_instance_profile") != "LabInstanceProfile"]
    if errados:
        falhar(
            f"As EC2 usam iam_instance_profile = {errados[0]}. Use \"LabInstanceProfile\": é com ele que a "
            "EC2 baixa o bundle do S3 no primeiro boot. Corrija no main.tf do módulo rag-cluster."
        )


TIPOS_PROIBIDOS = ("aws_s3_bucket", "aws_iam_")


def test_sem_bucket_nem_iam(plano):
    achados = sorted({
        r.get("address") for r in plano.get("resource_changes", [])
        if r.get("mode") == "managed" and str(r.get("type", "")).startswith(TIPOS_PROIBIDOS)
    })
    if achados:
        falhar(
            f"O plan cria {', '.join(achados)}. No Learner Lab não se cria bucket nem IAM: o bucket é o seu "
            "base-config-<RM> (já existe) e a permissão das EC2 vem do LabInstanceProfile. Apague esses "
            "recursos do Terraform."
        )


# ---------- Pipeline: o proprio .gitlab-ci.yml ----------
# O pipeline verde so prova o trabalho se os jobs de teste estiverem nele: sem
# estes testes, apagar o smoke-prod ou relaxar o contrato deixaria tudo verde.

ARQUIVO_PIPELINE = RAIZ_TERRAFORM / ".gitlab-ci.yml"
STAGES = ["validar", "planejar", "contrato", "deploy-dev", "verificar-dev", "deploy-prod", "verificar-prod"]
STAGE_DO_JOB = {
    "fmt-validate": "validar",
    "checkov": "validar",
    "tf-test": "validar",
    "rag-quality-gate": "validar",
    "plan-dev": "planejar",
    "plan-prod": "planejar",
    "contrato-dev": "contrato",
    "contrato-prod": "contrato",
    "deploy-dev": "deploy-dev",
    "smoke-dev": "verificar-dev",
    "observabilidade-dev": "verificar-dev",
    "deploy-prod": "deploy-prod",
    "smoke-prod": "verificar-prod",
    "observabilidade-prod": "verificar-prod",
}
# Linha que cada job do kit roda (ci/kit-jobs.yml).
COMANDO_DO_KIT = {
    "tf-test": "terraform test",
    "rag-quality-gate": "pytest validacao/test_rag_gate.py",
    "contrato-dev": "pytest validacao/test_contrato.py --ambiente dev",
    "contrato-prod": "pytest validacao/test_contrato.py --ambiente prod",
    "smoke-dev": "pytest validacao/test_smoke.py --ambiente dev",
    "smoke-prod": "pytest validacao/test_smoke.py --ambiente prod",
    "observabilidade-dev": "pytest validacao/test_observabilidade.py --ambiente dev",
    "observabilidade-prod": "pytest validacao/test_observabilidade.py --ambiente prod",
}
NEEDS_OBRIGATORIOS = {
    # plan-<env> no needs e o que baixa o plan-<env>.tfplan e o build/ para o apply.
    "deploy-dev": ["plan-dev", "contrato-dev", "rag-quality-gate"],
    # A promocao: nada chega a prod sem o smoke E a observabilidade de dev verdes.
    "deploy-prod": ["plan-prod", "contrato-prod", "smoke-dev", "observabilidade-dev"],
    "smoke-dev": ["deploy-dev"],
    "observabilidade-dev": ["deploy-dev"],
    "smoke-prod": ["deploy-prod"],
    "observabilidade-prod": ["deploy-prod"],
}
JOBS_SEM_MANUAL = list(STAGE_DO_JOB)
# Relatorio JUnit de cada job: e ele que vira suite na aba Tests (print 02-tests.png).
# tf-test fica fora: o Terraform 1.10.5 do runner nao gera JUnit no terraform test.
RELATORIO_JUNIT = {
    "checkov": "checkov-report.xml",
    "rag-quality-gate": "rag-quality-gate.xml",
    "contrato-dev": "contrato-dev.xml",
    "contrato-prod": "contrato-prod.xml",
    "smoke-dev": "smoke-dev.xml",
    "smoke-prod": "smoke-prod.xml",
    "observabilidade-dev": "observabilidade-dev.xml",
    "observabilidade-prod": "observabilidade-prod.xml",
}
# Os jobs do aluno sao conferidos pelo efeito minimo (enunciado (jobs (seu))), nao pela forma.
JOBS_DO_ALUNO = ["fmt-validate", "checkov", "plan-dev", "plan-prod", "deploy-dev", "deploy-prod"]
# Comando em posicao de execucao: inicio da linha ou depois de &&, ;, (, then, do,
# com VAR=valor antes. Assim "echo terraform apply ..." ou "# terraform apply" nao
# contam; depois de || o comando so roda se o anterior falhar, entao tambem nao.
INICIO = r"(?:^|&&|;|\(|\bthen\b|\bdo\b)\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:time\s+|command\s+)?"
# Aceita opcoes globais antes do subcomando (terraform -chdir=. plan).
TERRAFORM = INICIO + r"terraform\s+(?:-\S+\s+)*"
# Chaves de topo do GitLab que nao sao jobs (so para listar os jobs encontrados).
NAO_SAO_JOBS = {
    "default", "include", "stages", "variables", "workflow",
    "image", "services", "cache", "before_script", "after_script",
}


class _Referencia(list):
    """!reference [.job, chave] do GitLab, trocado pelo valor apontado depois da leitura."""


def _ler_pipeline(texto):
    # O safe_load puro recusa a tag !reference, que o GitLab aceita; sem isto um
    # pipeline valido reprovaria como "YAML invalido".
    class Leitor(yaml.SafeLoader):
        pass

    Leitor.add_constructor(
        "!reference", lambda leitor, no: _Referencia(leitor.construct_sequence(no, deep=True))
    )
    raiz = yaml.load(texto, Loader=Leitor)

    def resolver(valor):
        if isinstance(valor, _Referencia):
            alvo = raiz
            for chave in valor:
                alvo = alvo.get(chave) if isinstance(alvo, dict) else None
            return resolver(alvo)
        if isinstance(valor, dict):
            return {chave: resolver(v) for chave, v in valor.items()}
        if isinstance(valor, list):
            return [resolver(v) for v in valor]
        return valor

    return resolver(raiz)


@pytest.fixture(scope="module")
def pipeline():
    # Antes de escrever o pipeline (rodando local): sem o arquivo, estes
    # testes ficam pulados. No pipeline o arquivo sempre existe.
    if not ARQUIVO_PIPELINE.is_file():
        pytest.skip("ainda não há .gitlab-ci.yml (no .gitlab-ci.yml)")
    if yaml is None:
        falhar(
            "O Python que roda o pytest não tem o PyYAML (import yaml), e sem ele o contrato "
            "não lê o .gitlab-ci.yml. No runner, o PyYAML vem com o Checkov no /opt/venv; "
            "no Codespaces, instale com pip install pyyaml. Corrija no .gitlab-ci.yml."
        )
    try:
        conteudo = _ler_pipeline(ARQUIVO_PIPELINE.read_text(encoding="utf-8"))
    except yaml.YAMLError as erro:
        falhar(
            f"O .gitlab-ci.yml não é um YAML válido: {' '.join(str(erro).split())}. "
            "No GitLab, Build → Pipeline editor → Validate mostra a linha do problema. "
            "Corrija no .gitlab-ci.yml."
        )
    if not isinstance(conteudo, dict):
        falhar(
            "O .gitlab-ci.yml está vazio ou não tem a forma de um pipeline (stages: e um bloco "
            "por job). Comece pelo conteúdo do ci/kit-jobs.yml do kit. Corrija no .gitlab-ci.yml."
        )
    return conteudo


def _mesclar(base, filho):
    # Regra do extends do GitLab: mapas se fundem chave a chave; listas e valores
    # simples do filho substituem os da base.
    junto = dict(base)
    for chave, valor in filho.items():
        if isinstance(valor, dict) and isinstance(junto.get(chave), dict):
            valor = _mesclar(junto[chave], valor)
        junto[chave] = valor
    return junto


def _job(pipeline, nome, _cadeia=()):
    """O job como o GitLab monta, com o que herda por extends; None se nao existe."""
    job = pipeline.get(nome)
    if not isinstance(job, dict) or nome in _cadeia:
        return None
    pais = job.get("extends") or []
    herdado = {}
    for pai in [pais] if isinstance(pais, str) else pais:
        herdado = _mesclar(herdado, _job(pipeline, pai, _cadeia + (nome,)) or {})
    return _mesclar(herdado, job)


def _nomes_dos_jobs(pipeline):
    # Chave que comeca com ponto e job oculto (base de extends): o GitLab nao o roda.
    return [
        str(nome) for nome, valor in pipeline.items()
        if isinstance(valor, dict) and nome not in NAO_SAO_JOBS and not str(nome).startswith(".")
    ]


def _linhas(valor):
    """Comandos do script, um por linha e com espacos normalizados."""
    if isinstance(valor, str):
        valor = valor.replace("\\\n", " ")
        return [" ".join(linha.split()) for linha in valor.splitlines() if linha.strip()]
    if isinstance(valor, list):
        return [linha for item in valor for linha in _linhas(item)]
    return []


def _linha_com(job, comando):
    # Em posicao de execucao: echo "pytest ..." ou um comentario nao rodam o teste.
    padrao = INICIO + r"(?:python3?\s+-m\s+)?" + re.escape(comando)
    for linha in _linhas(job.get("script")):
        if re.search(padrao, linha.replace("--ambiente=", "--ambiente ")):
            return linha
    return None


def _valores(job, chave):
    """A chave no job e em cada item de rules:, onde ela tambem vale."""
    regras = job.get("rules") if isinstance(job.get("rules"), list) else []
    return [job.get(chave)] + [r.get(chave) for r in regras if isinstance(r, dict)]


def _lista(valor):
    if valor is None:
        return []
    return [valor] if isinstance(valor, str) else list(valor)


def test_pipeline_stages_na_ordem(pipeline):
    stages = pipeline.get("stages")
    if stages != STAGES:
        encontrado = ", ".join(map(str, _lista(stages))) or "nenhum (falta a chave stages:)"
        falhar(
            f"Os stages do .gitlab-ci.yml são: {encontrado}. Esperado, nesta ordem: "
            f"{', '.join(STAGES)}. A ordem dos stages é a ordem do pipeline: o contrato "
            "precisa rodar depois do plan e antes do deploy. Corrija no .gitlab-ci.yml."
        )


def test_pipeline_tem_os_jobs_do_contrato(pipeline):
    faltando = [nome for nome in STAGE_DO_JOB if _job(pipeline, nome) is None]
    if faltando:
        kit = [nome for nome in faltando if nome in COMANDO_DO_KIT]
        falhar(
            f"Faltam no .gitlab-ci.yml os jobs: {', '.join(faltando)}. "
            f"Jobs encontrados: {', '.join(_nomes_dos_jobs(pipeline)) or 'nenhum'}. "
            f"Esperado: os 14 jobs com estes nomes exatos: {', '.join(STAGE_DO_JOB)}."
            + (
                f" Os jobs do kit ({', '.join(kit)}) vêm prontos no ci/kit-jobs.yml; sem eles, o "
                "pipeline fica verde sem ter testado o trabalho."
                if kit else ""
            )
            + " Corrija no .gitlab-ci.yml."
        )


def test_pipeline_jobs_no_stage_certo(pipeline):
    errados = []
    for nome, stage in STAGE_DO_JOB.items():
        job = _job(pipeline, nome)
        # Job ausente ja reprova test_pipeline_tem_os_jobs_do_contrato.
        if job is not None and job.get("stage") != stage:
            encontrado = job.get("stage") or "nenhum (o GitLab usa test)"
            errados.append(f"{nome} está no stage {encontrado}, esperado {stage}")
    if errados:
        falhar(f"Jobs no stage errado: {'; '.join(errados)}. Corrija no .gitlab-ci.yml.")


def test_pipeline_jobs_do_kit_rodam_os_testes(pipeline):
    errados = []
    for nome, comando in COMANDO_DO_KIT.items():
        job = _job(pipeline, nome)
        if job is None or _linha_com(job, comando):
            continue
        chamadas = [l for l in _linhas(job.get("script")) if "pytest" in l or "terraform test" in l]
        encontrado = " | ".join(f'"{l}"' for l in chamadas) or "nenhuma chamada de teste"
        errados.append(f'{nome} roda {encontrado}, esperado "{comando}"')
    if errados:
        falhar(
            f"Os jobs do kit não rodam os testes do kit: {'; '.join(errados)}. Sem essa linha, o "
            "job fica verde sem ter testado nada. Copie o job do ci/kit-jobs.yml do kit sem "
            "alterar o script. Corrija no .gitlab-ci.yml."
        )


def test_pipeline_needs_de_deploy_e_smoke(pipeline):
    errados = []
    for nome, obrigatorios in NEEDS_OBRIGATORIOS.items():
        job = _job(pipeline, nome)
        if job is None:
            continue
        needs = [n if isinstance(n, str) else (n or {}).get("job") for n in _lista(job.get("needs"))]
        faltando = [n for n in obrigatorios if n not in needs]
        if faltando:
            encontrado = ", ".join(map(str, needs)) or "sem needs"
            errados.append(f"{nome} tem needs [{encontrado}], falta {', '.join(faltando)}")
    if errados:
        falhar(
            f"Os needs não garantem a ordem do contrato: {'; '.join(errados)}. Esperado: "
            "deploy-dev com plan-dev, contrato-dev e rag-quality-gate; deploy-prod com plan-prod, "
            "contrato-prod, smoke-dev e observabilidade-dev (é isso que impede um release quebrado de "
            "chegar a prod); smoke-<env> e observabilidade-<env> com deploy-<env>. "
            "Corrija no .gitlab-ci.yml."
        )


def test_pipeline_jobs_do_kit_barram_na_falha(pipeline):
    errados = []
    for nome, comando in COMANDO_DO_KIT.items():
        job = _job(pipeline, nome)
        if job is None:
            continue
        for valor in _valores(job, "allow_failure"):
            # A forma {exit_codes: [...]} tambem deixa o job falhar sem barrar.
            if valor is True or isinstance(valor, dict):
                errados.append(f"{nome} tem allow_failure: {'true' if valor is True else valor}")
        linha = _linha_com(job, comando)
        normal = (linha or "").replace("--ambiente=", "--ambiente ")
        # So o || DEPOIS do teste mascara a falha dele.
        if linha and "||" in normal[normal.find(comando):]:
            errados.append(f'{nome} roda "{linha}", e o || esconde a falha do teste')
    if errados:
        falhar(
            f"Um teste do kit reprovado precisa deixar o pipeline vermelho, mas {'; '.join(errados)}. "
            "Esperado: os jobs do kit sem allow_failure e com a linha de teste do ci/kit-jobs.yml, sem ||. "
            "Corrija no .gitlab-ci.yml."
        )


def test_pipeline_sem_job_manual(pipeline):
    errados = []
    for nome in JOBS_SEM_MANUAL:
        job = _job(pipeline, nome)
        if job is None:
            continue
        if "manual" in _valores(job, "when"):
            errados.append(f"{nome} (when: manual)")
        elif job.get("when") in ("never", "on_failure"):
            errados.append(f"{nome} (when: {job['when']})")
        # rules, only e except decidem se o job entra no pipeline: com a condicao
        # errada (only: [tags], if da branch main), ele some na master sem erro.
        filtros = [chave for chave in ("rules", "only", "except") if job.get(chave) is not None]
        if filtros:
            errados.append(f"{nome} ({', '.join(c + ':' for c in filtros)})")
    if errados:
        falhar(
            f"Jobs fora do fluxo automático: {', '.join(errados)}. Um job manual espera um clique; "
            "when: never ou on_failure, rules, only e except podem tirar o job do pipeline da "
            "master. Nos dois casos o pipeline termina verde sem ele ter rodado. Esperado: sem "
            "when, rules, only nem except nos 14 jobs (cada push valida, publica e testa sozinho). "
            "Corrija no .gitlab-ci.yml."
        )


def _expansor(pipeline, job):
    """Troca $VAR e ${VAR} pelas variables do pipeline e do job: plan-${AMBIENTE}.tfplan
    num template de extends vira plan-dev.tfplan."""
    variaveis = {}
    for origem in (pipeline.get("variables"), job.get("variables")):
        if isinstance(origem, dict):
            for chave, valor in origem.items():
                valor = valor.get("value") if isinstance(valor, dict) else valor
                if isinstance(valor, (str, int, float)):
                    variaveis[str(chave)] = str(valor)

    def expandir(texto):
        return re.sub(
            r"\$\{(\w+)\}|\$(\w+)",
            lambda m: variaveis.get(m.group(1) or m.group(2), m.group(0)),
            str(texto),
        )

    return expandir


def _artefatos(pipeline, job):
    # artifacts do job substitui o do default inteiro (regra do GitLab).
    return job.get("artifacts", (pipeline.get("default") or {}).get("artifacts")) or {}


def _comandos(pipeline, job):
    """before_script (o do job ou o do default) + script, um comando por linha."""
    antes = job.get("before_script", (pipeline.get("default") or {}).get("before_script"))
    expandir = _expansor(pipeline, job)
    return [expandir(linha) for linha in _linhas(antes) + _linhas(job.get("script"))]


def _chamadas(linhas, padrao, *trechos):
    return [
        (i, linha) for i, linha in enumerate(linhas)
        if re.search(padrao, linha) and all(trecho in linha for trecho in trechos)
    ]


def _sem_mascara(chamadas, padrao):
    # So o || DEPOIS do comando mascara a falha dele.
    return [(i, l) for i, l in chamadas if "||" not in l[re.search(padrao, l).start():]]


def _encontrado(chamadas, rotulo):
    return " | ".join(f'"{linha}"' for _, linha in chamadas) or f"nenhuma linha com {rotulo}"


def _workspace_antes(job, linhas, ambiente, indice):
    if str((job.get("variables") or {}).get("TF_WORKSPACE", "")) == ambiente:
        return True
    padroes = [
        TERRAFORM + rf"workspace\s+(?:select|new)\b.*(?<![\w-]){ambiente}(?![\w-])",
        rf"(?:^|&&|;|\bexport\s+)\s*TF_WORKSPACE=[\"']?{ambiente}(?![\w-])",
    ]
    # <=: select e comando na mesma linha (select dev && terraform plan ...).
    return any(i <= indice for padrao in padroes for i, _ in _chamadas(linhas, padrao))


def _faltas_do_job(pipeline, nome, job):
    linhas = _comandos(pipeline, job)
    faltas = []
    ambiente = nome.rsplit("-", 1)[1] if nome.startswith(("plan-", "deploy-")) else None
    plano = f"plan-{ambiente}.tfplan"

    if nome == "fmt-validate":
        fmt = TERRAFORM + r"fmt\b"
        if not _sem_mascara(_chamadas(linhas, fmt, "-check", "-recursive"), fmt):
            faltas.append(
                f"terraform fmt -check -recursive sem || depois (encontrado: "
                f"{_encontrado(_chamadas(linhas, fmt), 'terraform fmt')})"
            )
        validate = TERRAFORM + r"validate\b"
        if not _sem_mascara(_chamadas(linhas, validate), validate):
            faltas.append(
                f"terraform validate sem || depois (encontrado: "
                f"{_encontrado(_chamadas(linhas, validate), 'terraform validate')})"
            )
    elif nome == "checkov":
        checkov = INICIO + r"checkov\b"
        if not _chamadas(linhas, checkov, "junitxml"):
            achados = _encontrado(_chamadas(linhas, checkov), "checkov")
            faltas.append(f"checkov com saída -o junitxml (encontrado: {achados})")
    elif nome.startswith("plan-"):
        comando = TERRAFORM + r"plan\b"
        certas = _sem_mascara(_chamadas(linhas, comando, "-out", plano), comando)
        if not certas:
            faltas.append(
                f"terraform plan -out={plano} sem || depois (encontrado: "
                f"{_encontrado(_chamadas(linhas, comando), 'terraform plan')})"
            )
        indice = certas[0][0] if certas else len(linhas)
        if not _workspace_antes(job, linhas, ambiente, indice):
            faltas.append(f"terraform workspace select {ambiente} (ou new {ambiente}) antes do plan")
        expandir = _expansor(pipeline, job)
        caminhos = [expandir(c).removeprefix("./") for c in _lista(_artefatos(pipeline, job).get("paths"))]
        if not any(fnmatch.fnmatch(plano, c) for c in caminhos):
            faltas.append(
                f"artifacts: paths com {plano} (encontrado: {', '.join(caminhos) or 'sem artifacts'})"
            )
    elif nome.startswith("deploy-"):
        comando = TERRAFORM + r"apply\b"
        certas = _sem_mascara(_chamadas(linhas, comando, plano), comando)
        if not certas:
            faltas.append(
                f"terraform apply do {plano} sem || depois (encontrado: "
                f"{_encontrado(_chamadas(linhas, comando), 'terraform apply')})"
            )
        indice = certas[0][0] if certas else len(linhas)
        if not _workspace_antes(job, linhas, ambiente, indice):
            faltas.append(f"terraform workspace select {ambiente} antes do apply")

    if nome != "checkov":
        for valor in _valores(job, "allow_failure"):
            if valor is True or isinstance(valor, dict):
                faltas.append(f"sem allow_failure (encontrado: allow_failure: {'true' if valor is True else valor})")
    return faltas


def test_pipeline_jobs_do_aluno_fazem_o_minimo(pipeline):
    # Sem isto, um job do aluno esvaziado (deploy-dev com echo, por exemplo) fica
    # verde, e o smoke passa se a infraestrutura ja existia de um apply anterior.
    errados = []
    for nome in JOBS_DO_ALUNO:
        job = _job(pipeline, nome)
        if job is None:
            continue
        faltas = _faltas_do_job(pipeline, nome, job)
        if faltas:
            errados.append(f"{nome}: falta {' e '.join(faltas)}")
    if errados:
        falhar(
            f"Os seus jobs não fazem o mínimo do enunciado (jobs (seu)). {'; '.join(errados)}. A forma é livre "
            "(select || new ou -or-create, -out=arquivo ou -out arquivo), mas cada job precisa ter "
            "o efeito da tabela do enunciado (jobs (seu)), sem || que esconda a falha do comando e sem "
            "allow_failure (só o checkov reporta sem barrar). Corrija no .gitlab-ci.yml."
        )


def test_pipeline_relatorios_junit(pipeline):
    errados = []
    for nome, arquivo in RELATORIO_JUNIT.items():
        job = _job(pipeline, nome)
        if job is None:
            continue
        artefatos = _artefatos(pipeline, job)
        junit = [str(c).removeprefix("./") for c in _lista((artefatos.get("reports") or {}).get("junit"))]
        if not any(fnmatch.fnmatch(arquivo, c) for c in junit):
            if junit:
                errados.append(f"{nome} publica {', '.join(junit)} em reports: junit, esperado {arquivo}")
            else:
                errados.append(f"{nome} não publica nada em reports: junit, esperado {arquivo}")
        elif not any(arquivo in linha for linha in _linhas(job.get("script"))):
            errados.append(f"{nome} declara {arquivo} em reports: junit, mas nenhuma linha do script gera esse arquivo")
        elif nome in COMANDO_DO_KIT and artefatos.get("when") != "always":
            errados.append(
                f"{nome} sem artifacts: when: always (encontrado: {artefatos.get('when') or 'sem when'}), "
                "e o relatório some justamente quando um teste reprova"
            )
    if errados:
        falhar(
            f"Cada suíte da aba Tests vem de um reports: junit, e faltou: {'; '.join(errados)}. "
            "Esperado: o checkov com checkov-report.xml e os jobs do kit como no ci/kit-jobs.yml "
            "(--junitxml <job>.xml, artifacts: when: always e reports: junit). Sem isso a suíte "
            "não aparece na aba Tests e o print 02-tests.png da entrega fica sem o que mostrar. "
            "Corrija no .gitlab-ci.yml."
        )


def test_pipeline_jobs_com_tag_shell(pipeline):
    # tags no default: vale para quem nao declara as proprias.
    padrao = (pipeline.get("default") or {}).get("tags")
    errados = []
    for nome in _nomes_dos_jobs(pipeline):
        tags = _lista(_job(pipeline, nome).get("tags", padrao))
        if "shell" not in tags:
            errados.append(f"{nome} ({'tags: ' + ', '.join(map(str, tags)) if tags else 'sem tags'})")
    if errados:
        falhar(
            f"Jobs sem a tag shell: {'; '.join(errados)}. Esperado: tags: [shell] em cada job, como "
            "nos labs 03.1 e 03.2: é a tag do seu runner qc-runner-tf-ai. Corrija no .gitlab-ci.yml."
        )


# Jobs que leem ou gravam o state de cada ambiente: com resource_group, o GitLab
# roda um de cada vez, e dois pipelines nao disputam o mesmo state.
RESOURCE_GROUP = {"plan-dev": "dev", "deploy-dev": "dev", "plan-prod": "prod", "deploy-prod": "prod"}


def test_pipeline_resource_group_por_ambiente(pipeline):
    errados = []
    for nome, grupo in RESOURCE_GROUP.items():
        job = _job(pipeline, nome)
        if job is not None and str(job.get("resource_group") or "") != grupo:
            errados.append(f"{nome} (resource_group: {job.get('resource_group') or 'ausente'}, esperado {grupo})")
    if errados:
        falhar(
            f"Jobs sem o resource_group do ambiente: {'; '.join(errados)}. Dois pipelines ao mesmo tempo "
            "disputariam o mesmo state: o segundo apply para com Error acquiring the state lock. Com "
            "resource_group, o GitLab enfileira os jobs do mesmo ambiente. Esperado: resource_group: dev em "
            "plan-dev e deploy-dev, e resource_group: prod em plan-prod e deploy-prod. Corrija no .gitlab-ci.yml."
        )
