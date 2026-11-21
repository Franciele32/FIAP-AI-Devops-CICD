#!/usr/bin/env python3
"""Gera os diagramas do Trabalho Final (AI-Devops-CICD, Quantum Commerce).

Uso:     python3 gerar.py
Saida:   arquitetura-trabalho-final.excalidraw   (visao geral)
         pipeline-trabalho-final.excalidraw      (stages, kit x voce)
Render:  bash ~/.claude/skills/excalidraw-aulas/scripts/render.sh \
             arquitetura-trabalho-final.excalidraw /tmp/arq-2x.png
         (render 2x) e reduza com LANCZOS para 2400 px de largura em
         ../img/arquitetura-trabalho-final.png (idem para o pipeline)

Icones OFICIAIS em assets/ (SVG fonte + PNG 160px embutido, porque SVG
embutido nao rasteriza no render headless). Mesmos arquivos de
04-GenAIOps/diagramas/assets:
  - ec2, elb, s3, ssm-parameter-store, vpc-group, aws-cloud: AWS
    Architecture Icons, pacote oficial 04/2026
    (https://aws.amazon.com/architecture/icons/)
  - gitlab, github (Codespaces), otel, prometheus, loki, jaeger, grafana:
    marcas oficiais dos projetos

Regra unica de rotulo (L54): rotulo de seta horizontal fica ACIMA dela; de
seta vertical, ao lado. Licoes de UX aplicadas (skill excalidraw-aulas): L1,
L3, L5, L6, L8 apoio tracejado, L11 fan-out de um ponto, L13 magenta so no
titulo, L18 rotulo com fundo opaco, L20 corpo >= 18 px logicos, L21 S3 e SSM
fora da VPC, L22 smoke chega nos DOIS ALBs, L27 nenhuma borda tracejada,
L36/L44 folgas verificadas por script, L42 telemetria sai de cada EC2,
L48 toda acao na AWS sai do runner, L50 B1..B4 x stages 1..7, L52 seta
longe do titulo, L59/L61 coletor dos jobs, L60 margem externa.
"""
import base64
import json
import os
import sys

from PIL import ImageFont

DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(DIR, "assets")

MAGENTA = "#B80D47"     # magenta-deep Quantum Commerce: so no titulo (L13)
TEXTO = "#1e1e1e"
ESCURO = "#343a40"      # texto secundario escuro (L32)
CINZA = "#495057"
AWS = "#232F3E"
GITLAB = "#E24329"
VPC_ROXO = "#8C4FFF"
ALERTA = "#C92A2A"      # so o caminho de reprovacao (L10)
FUNDO = "#f1f3f5"
PAINEL = "#f8f9fa"

PISO = 17               # L20: piso; o corpo usa 18 (~14 px em 1366)

_FONTES = {}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def larg(t, size):
    """Largura real do glifo (Helvetica = fontFamily 2 do Excalidraw)."""
    if size not in _FONTES:
        _FONTES[size] = ImageFont.truetype(
            "/System/Library/Fonts/Helvetica.ttc", size)
    return max(_FONTES[size].getlength(l) for l in t.split("\n"))


class Diagrama:
    def __init__(self):
        self.elements = []
        self.files = {}
        self._n = 0

    def _id(self, p):
        self._n += 1
        return f"{p}{self._n}"

    def _base(self, tipo, x, y, w, h, **kw):
        el = {
            "id": self._id(tipo[:3]), "type": tipo, "x": x, "y": y,
            "width": w, "height": h, "angle": 0, "strokeColor": TEXTO,
            "backgroundColor": "transparent", "fillStyle": "solid",
            "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1,
            "opacity": 100, "groupIds": [], "frameId": None, "roundness": None,
            "seed": 1000 + self._n * 7, "version": 1,
            "versionNonce": 2000 + self._n * 11, "isDeleted": False,
            "boundElements": [], "updated": 1, "link": None, "locked": False,
        }
        el.update(kw)
        self.elements.append(el)
        return el

    def texto(self, t, x, y, w, size=18, cor=TEXTO, align="center"):
        size = max(size, PISO)
        linhas = t.count("\n") + 1
        h = linhas * size * 1.25
        self._base("text", x, y, w, h, strokeColor=cor, strokeWidth=1,
                   fontSize=size, fontFamily=2, text=t, originalText=t,
                   textAlign=align, verticalAlign="top", containerId=None,
                   lineHeight=1.25, baseline=int(size * 0.8))
        return h

    def rotulo(self, t, cx, y, size=18, cor=ESCURO, bg="#ffffff"):
        """Rotulo de seta centrado em cx, com fundo opaco atras (L18)."""
        w = larg(t, size) + 4
        h = (t.count("\n") + 1) * size * 1.25
        self._base("rectangle", cx - w / 2 - 6, y - 4, w + 12, h + 8,
                   strokeColor="transparent", backgroundColor=bg,
                   strokeWidth=1, roughness=0)
        self.texto(t, cx - w / 2, y, w, size=size, cor=cor)

    def rotulo_esq(self, t, x, y, **kw):
        """Rotulo que comeca em x."""
        self.rotulo(t, x + (larg(t, kw.get("size", 18)) + 4) / 2, y, **kw)

    def rotulo_dir(self, t, x, y, **kw):
        """Rotulo que termina em x."""
        self.rotulo(t, x - (larg(t, kw.get("size", 18)) + 4) / 2, y, **kw)

    def imagem(self, nome, x, y, s):
        fid = self._id("file")
        with open(os.path.join(ASSETS, f"{nome}.png"), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        self.files[fid] = {"mimeType": "image/png", "id": fid,
                           "dataURL": f"data:image/png;base64,{b64}",
                           "created": 1}
        self._base("image", x, y, s, s, strokeColor="transparent",
                   roughness=0, status="saved", fileId=fid, scale=[1, 1])

    def ret(self, x, y, w, h, cor=TEXTO, bg="#ffffff", largura=2,
            sem_borda=False):
        self._base("rectangle", x, y, w, h,
                   strokeColor="transparent" if sem_borda else cor,
                   backgroundColor=bg, strokeWidth=largura)

    def caixa(self, x, y, w, h, titulo, cor, bg="transparent", icone=None,
              size=18, largura=2, sub=None):
        """Container com icone opcional e titulo no topo interno."""
        self.ret(x, y, w, h, cor=cor, bg=bg, largura=largura)
        tx = x + 14
        if icone:
            self.imagem(icone, x + 10, y + 8, 34)
            tx = x + 54
        self.texto(titulo, tx, y + 13, w - (tx - x) - 14, size=size,
                   cor=cor, align="left")
        if sub:
            self.texto(sub, tx, y + 13 + size * 1.25 + 4, w - (tx - x) - 14,
                       size=18, cor=TEXTO, align="left")

    def cartao(self, x, y, w, h, titulo, corpo, icone=None, s=44,
               tsize=19, csize=18, cor=TEXTO, bg="#ffffff", abaixo=False):
        """Cartao padrao (L40): icone + titulo + linhas. abaixo=True poe o
        corpo na largura toda, sob o icone."""
        self.ret(x, y, w, h, cor=cor, bg=bg)
        tx = x + 14
        if icone:
            self.imagem(icone, x + 12, y + 12, s)
            tx = x + 24 + s
        th = self.texto(titulo, tx, y + 11, w - (tx - x) - 10, size=tsize,
                        cor=cor, align="left")
        if corpo:
            cx, cy = (x + 14, y + 20 + s) if abaixo else (tx, y + 11 + th + 5)
            self.texto(corpo, cx, cy, w - (cx - x) - 10, size=csize,
                       cor=ESCURO, align="left")

    def no(self, icone, cx, cy, s=64):
        self.imagem(icone, cx - s / 2, cy - s / 2, s)
        return {"l": cx - s / 2, "r": cx + s / 2, "t": cy - s / 2,
                "b": cy + s / 2, "cx": cx, "cy": cy}

    def seta(self, pontos, cor=TEXTO, tracejada=False, largura=2,
             ponta=True, roughness=1):
        x0, y0 = pontos[0]
        rel = [[px - x0, py - y0] for px, py in pontos]
        xs = [p[0] for p in rel]
        ys = [p[1] for p in rel]
        self._base("arrow" if ponta else "line", x0, y0,
                   max(xs) - min(xs), max(ys) - min(ys),
                   strokeColor=cor, strokeWidth=largura,
                   strokeStyle="dashed" if tracejada else "solid",
                   roughness=roughness, points=rel, lastCommittedPoint=None,
                   startBinding=None, endBinding=None, startArrowhead=None,
                   endArrowhead="arrow" if ponta else None)

    def salvar(self, caminho):
        doc = {"type": "excalidraw", "version": 2,
               "source": "quantum-commerce-gerador",
               "elements": self.elements,
               "appState": {"gridSize": None,
                            "viewBackgroundColor": "#ffffff"},
               "files": self.files}
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        log(f"gerado: {caminho} ({len(self.elements)} elementos)")


def folgas(d, minimo=15, ponta=30):
    """L36/L44: rotulo a >= 15 px de qualquer borda de caixa e ponta de seta
    longe de borda que nao e o destino. Devolve as violacoes (vazio = ok)."""
    caixas = [e for e in d.elements if e["type"] == "rectangle"
              and e["strokeColor"] != "transparent"]
    rotulos = [e for e in d.elements if e["type"] == "rectangle"
               and e["strokeColor"] == "transparent"
               and e["backgroundColor"] == "#ffffff"]
    ruins = []
    for r in rotulos:
        rl, rr = r["x"], r["x"] + r["width"]
        rt, rb = r["y"], r["y"] + r["height"]
        for c in caixas:
            cl, cr = c["x"], c["x"] + c["width"]
            ct, cb = c["y"], c["y"] + c["height"]
            for nome, borda, vert in (("esq", cl, True), ("dir", cr, True),
                                      ("topo", ct, False), ("base", cb, False)):
                if vert:
                    if not (rt < cb and rb > ct):
                        continue
                    dist = 0 if rl < borda < rr else min(abs(rl - borda),
                                                         abs(rr - borda))
                else:
                    if not (rl < cr and rr > cl):
                        continue
                    dist = 0 if rt < borda < rb else min(abs(rt - borda),
                                                         abs(rb - borda))
                if dist < minimo:
                    ruins.append(f"rotulo@({rl:.0f},{rt:.0f}) a {dist:.0f}px "
                                 f"da borda {nome} de caixa@({cl:.0f},{ct:.0f})")
    for e in d.elements:
        if e["type"] != "arrow":
            continue
        px = e["x"] + e["points"][-1][0]
        py = e["y"] + e["points"][-1][1]
        perto = []
        for c in caixas:
            cl, cr = c["x"], c["x"] + c["width"]
            ct, cb = c["y"], c["y"] + c["height"]
            for dist, dentro in ((abs(px - cl), ct <= py <= cb),
                                 (abs(px - cr), ct <= py <= cb),
                                 (abs(py - ct), cl <= px <= cr),
                                 (abs(py - cb), cl <= px <= cr)):
                if dentro and dist < ponta:
                    perto.append(dist)
        perto.sort()
        extras = perto[1:] if perto and perto[0] <= 12 else perto
        for dist in extras:
            ruins.append(f"ponta em ({px:.0f},{py:.0f}) a {dist:.0f}px "
                         "de outra borda")
    return ruins


def sobreposicoes(d):
    """Texto x texto e texto x icone, com a largura real do glifo."""
    caixas = []
    for e in d.elements:
        if e["type"] == "text":
            w = larg(e["text"], e["fontSize"])
            x = e["x"] + (e["width"] - w) / 2 if e["textAlign"] == "center" \
                else e["x"]
            caixas.append(("txt", e["text"][:30], x, e["y"], w, e["height"]))
        elif e["type"] == "image":
            caixas.append(("img", e["fileId"], e["x"], e["y"], e["width"],
                           e["height"]))
    ruins = []
    for i, a in enumerate(caixas):
        for b in caixas[i + 1:]:
            if a[0] == "img" and b[0] == "img":
                continue
            if (a[2] < b[2] + b[4] and b[2] < a[2] + a[4]
                    and a[3] < b[3] + b[5] and b[3] < a[3] + a[5]):
                ruins.append(f"{a[1]!r} x {b[1]!r}")
    return ruins


OTLP = "#3B5BDB"        # telemetria: tracejado azul, distinto do apoio
KIT_BG = "#CED4DA"
NEEDS = "#5F3DC4"      # needs: roxo, cor que nenhum ambiente usa      # job pronto no kit
SEU_BG = "#FFF3BF"      # job que o aluno escreve
DEV_BG, DEV_COR = "#E7F5FF", "#1864AB"
PROD_BG, PROD_COR = "#EBFBEE", "#2B8A3E"


def checar(d, nome):
    for v in folgas(d):
        log(f"  [{nome}] FOLGA:", v)
    for v in sobreposicoes(d):
        log(f"  [{nome}] SOBREPOSICAO:", v)


def arquitetura():
    d = Diagrama()
    # Ordem de desenho: containers, depois setas, depois cartoes (o cartao de
    # origem cobre o inicio da seta, L44).

    d.texto("Quantum Commerce · Trabalho Final: o Assistente RAG em dev e "
            "prod, entregue por pipeline e observado", 30, 20, 1760, size=30,
            cor=MAGENTA, align="left")
    d.texto("Empresa fictícia. Seu trabalho: escrever o Terraform dos "
            "ambientes e o .gitlab-ci.yml. O make bootstrap prepara o resto "
            "uma vez; cada git push entrega dev, confere dev e só então "
            "entrega prod.", 30, 64, 1760, size=20, cor=ESCURO, align="left")

    # --- geometria ----------------------------------------------------------
    CXL = 150
    BX, BY, BW, BH = 290, 105, 1500, 237
    CW, CG, CY, CH = 320, 50, 152, 172
    CX0 = BX + (BW - 4 * CW - 3 * CG) / 2
    PX, PY, PW = 290, BY + BH + 40, 1500
    SW, SG, SH = 176, 36, 100
    SY = PY + 62
    sx0 = PX + (PW - 7 * SW - 6 * SG) / 2
    sx = [sx0 + i * (SW + SG) for i in range(7)]
    scx = [x + SW / 2 for x in sx]
    SCY = SY + SH / 2
    YCOL = SY + SH + 24
    PH = YCOL + 62 - PY
    AX, AY, AW = 290, PY + PH + 40, 1500
    VX, VY = 320, AY + 55
    VR = 1760
    RX, RY, RW = 440, VY + 100, 240
    XJ = 560                                # entre os tocos 1 e 2 (L61)
    FX, FY, FW = 905, RY + 110, 420         # ambientes
    PNX, PNW = FX + 20, FW - 40
    ALB_X, EC2_X = PNX + 75, PNX + 260
    DEVY, DEVH = FY + 78, 192
    PRODY, PRODH = DEVY + DEVH + 20, 350
    alb_dev = DEVY + 71
    ec2_dev = [alb_dev]
    ec2_prod = [PRODY + 71, PRODY + 216]
    alb_prod = (ec2_prod[0] + ec2_prod[1]) / 2
    FH = PRODY + PRODH + 20 - FY
    OX, OW = FX + FW + 60, 360              # observabilidade
    OCX = OX + OW / 2
    X_BUS = FX + FW + 30
    Y_DEPLOY = FY + 26
    Y_QUERY = RY + 20
    X_QUERY = OX + 70
    Y_SMOKE = (alb_dev + alb_prod) / 2
    X_TRONCO = FX - 25
    RH = Y_SMOKE + 40 - RY
    VH = FY + FH + 22 - VY
    S3X, S3Y, S3W, S3H = 400, VY + VH + 62, 300, 236
    AH = S3Y + S3H + 22 - AY
    X_STATE = 500

    d._base("rectangle", 0, 0, 1820, AY + AH + 30 + 44 + 5 * 30.5 + 14 + 30, strokeColor="transparent",
            backgroundColor="transparent", strokeWidth=1, roughness=0)

    # --- containers ---------------------------------------------------------
    d.ret(BX, BY, BW, BH, cor=CINZA, bg=FUNDO, largura=1)
    d.texto("Preparação · uma vez, automatizada:  make bootstrap REPO=… "
            "GITLAB_TOKEN=…   (rodar de novo é seguro)",
            BX + 20, BY + 12, BW - 40, size=19, cor=TEXTO, align="left")
    d.caixa(PX, PY, PW, PH, "Pipeline do GitLab CI/CD · 7 stages, o mesmo "
            "código para dev e prod (detalhe no diagrama do pipeline)", TEXTO,
            icone="gitlab", size=19)
    d.caixa(AX, AY, AW, AH, "AWS · us-east-1", AWS, icone="aws-cloud")
    d.caixa(VX, VY, VR - VX, VH, "VPC fiap-lab", VPC_ROXO, icone="vpc-group")
    nota = ("compartilhada e já existe: o Terraform só a consulta "
            "(não cria nem apaga)")
    d.texto(nota, VR - 16 - larg(nota, 18) - 4, VY + 13,
            larg(nota, 18) + 4, size=18, cor=VPC_ROXO, align="left")
    d.ret(FX, FY, FW, FH, cor=TEXTO, bg="#ffffff")
    d.texto("Ambientes · seu Terraform cria cada\num em um workspace (cópia "
            "isolada)", FX + 20, FY + 13, FW - 34, size=18, align="left")
    d.ret(PNX, DEVY, PNW, DEVH, bg=DEV_BG, sem_borda=True)
    d.ret(PNX, PRODY, PNW, PRODH, bg=PROD_BG, sem_borda=True)
    d.caixa(OX, FY, OW, FH, "EC2 de observabilidade (B3)", TEXTO,
            icone="ec2", sub="qc-obs-tf-ai")

    # --- voce ---------------------------------------------------------------
    eu = d.no("github", CXL, 195, s=64)
    d.texto("Você no GitHub\nCodespaces (seu\nambiente no navegador)",
            CXL - 110, 233, 220, size=18)
    d.seta([(eu["r"] + 8, 195), (CX0 - 4, 195)], cor=CINZA, tracejada=True)
    d.rotulo("uma vez", 232, 152)
    repo = d.no("gitlab", CXL, SCY, s=64)
    d.seta([(CXL - 30, 312), (CXL - 30, repo["t"] - 8)], cor=TEXTO,
           largura=3)
    d.rotulo_dir("git push", CXL - 44, (312 + repo["t"]) / 2 - 11)
    d.texto("Repositório GitLab\ntrabalho-final-ai\n(criado no B1):\n"
            "recebe o kit (B4),\nseu Terraform e o\n.gitlab-ci.yml (a\nreceita do pipeline)", CXL - 110,
            repo["b"] + 8, 220, size=18)

    for i in range(3):
        x1 = CX0 + i * (CW + CG) + CW
        d.seta([(x1 - 20, CY + CH / 2), (x1 + CG - 8, CY + CH / 2)],
               cor=CINZA, tracejada=True)

    # --- pipeline -----------------------------------------------------------
    d.seta([(repo["r"] + 8, SCY), (sx[0] - 8, SCY)], cor=TEXTO, largura=3)
    d.rotulo("dispara", 232, SCY - 42)
    for i in range(6):
        d.seta([(sx[i] + SW - 20, SCY), (sx[i + 1] - 8, SCY)], cor=TEXTO,
               largura=3)
    for x in scx:
        d.seta([(x, SY + SH - 10), (x, YCOL)], cor=TEXTO, largura=3,
               ponta=False, roughness=0)
    d.seta([(scx[0], YCOL), (scx[-1], YCOL)], cor=TEXTO, largura=3,
           ponta=False, roughness=0)
    d.seta([(XJ, YCOL), (XJ, RY - 8)], cor=TEXTO, largura=3, roughness=0)
    d.rotulo_esq("o GitLab entrega os jobs de todos os stages ao runner",
                 XJ + 22, YCOL + 16)

    # --- runner age na AWS (L48) --------------------------------------------
    d.seta([(RX + RW - 20, Y_DEPLOY), (FX - 12, Y_DEPLOY)], cor=TEXTO,
           largura=3, roughness=0)
    d.rotulo("4 · deploy no dev,\ndepois 6 · no prod:\nterraform apply",
             (RX + RW + FX) / 2, Y_DEPLOY - 12 - 3 * 22.5)
    for alb_y in (alb_dev, alb_prod):
        d.seta([(RX + RW - 20, Y_SMOKE), (X_TRONCO, Y_SMOKE),
                (X_TRONCO, alb_y), (ALB_X - 36, alb_y)], cor=TEXTO,
               largura=3, roughness=0)
    d.rotulo("5 · smoke no dev,\ndepois 7 · no prod:\no teste do kit\npergunta ao ALB",
             (RX + RW + X_TRONCO) / 2, Y_SMOKE - 12 - 4 * 22.5)
    d.seta([(RX + RW - 20, Y_QUERY), (X_QUERY, Y_QUERY),
            (X_QUERY, FY - 8)], cor=TEXTO, largura=3, roughness=0)
    d.rotulo("por último, em 5 (dev) e 7 (prod), depois do smoke: o teste do kit\n"
             "consulta Prometheus, Loki e Jaeger atrás do log, da métrica e do trace",
             (RX + RW + X_QUERY) / 2, Y_QUERY - 12 - 2 * 22.5)
    d.seta([(X_STATE, RY + RH - 20), (X_STATE, S3Y - 8)], cor=CINZA,
           tracejada=True)
    d.rotulo_esq("4 · no dev, depois\n6 · no prod: o Terraform\ngrava o "
                 "state (o\nregistro do que criou)", X_STATE + 16,
                 (RY + RH + VY + VH) / 2 - 34)

    # --- ambientes: ALB -> EC2 ----------------------------------------------
    xt = (ALB_X + EC2_X) / 2
    d.seta([(ALB_X + 36, alb_dev), (EC2_X - 36, alb_dev)], cor=CINZA,
           roughness=0)
    for ey in ec2_prod:
        d.seta([(ALB_X + 36, alb_prod), (xt, alb_prod), (xt, ey),
                (EC2_X - 36, ey)], cor=CINZA, roughness=0)

    # --- telemetria: sai de cada EC2 (L42) ----------------------------------
    col = {"cx": OCX, "cy": alb_dev}
    d.seta([(EC2_X + 36, alb_dev), (OCX - 36, alb_dev)], cor=OTLP,
           tracejada=True, roughness=0)
    for ey in ec2_prod:
        d.seta([(EC2_X + 36, ey), (X_BUS, ey)], cor=OTLP, tracejada=True,
               ponta=False, roughness=0)
    d.seta([(X_BUS, ec2_prod[-1]), (X_BUS, alb_dev)], cor=OTLP,
           tracejada=True, ponta=False, roughness=0)

    # --- observabilidade: Collector -> 3 armazens -> Grafana ----------------
    y_col_lbl = col["cy"] + 34 + 2 * 22.5
    y_b = y_col_lbl + 12 + 25 + 50 + 28 + 8
    bx = [OCX - 110, OCX, OCX + 110]
    y_fan = y_col_lbl + 12 + 25
    y_col_lbl += 22.5
    y_b += 22.5
    y_fan += 22.5
    d.seta([(OCX, y_col_lbl + 12), (OCX, y_fan)], cor=OTLP, tracejada=True,
           ponta=False, roughness=0)
    d.seta([(bx[0], y_fan), (bx[2], y_fan)], cor=OTLP, tracejada=True,
           ponta=False, roughness=0)
    for x in bx:
        d.seta([(x, y_fan), (x, y_b - 36)], cor=OTLP, tracejada=True,
               roughness=0)
    y_b_lbl = y_b + 34 + 2 * 22.5
    y_in = y_b_lbl + 12 + 25
    y_g = y_in + 50 + 36
    for x in bx:
        d.seta([(x, y_b_lbl + 12), (x, y_in)], cor=OTLP, tracejada=True,
               ponta=False, roughness=0)
    d.seta([(bx[0], y_in), (bx[2], y_in)], cor=OTLP, tracejada=True,
           ponta=False, roughness=0)
    d.seta([(OCX, y_in), (OCX, y_g - 36)], cor=OTLP, tracejada=True,
           roughness=0)
    log(f"  grafana base {y_g + 28:.0f} x caixa {FY + FH:.0f}")

    # --- cartoes e nos ------------------------------------------------------
    cards = [
        ("B1 · projeto no GitLab",
         "cria trabalho-final-ai e\ndesliga os runners\ncompartilhados: os jobs\n"
         "rodam só no seu runner"),
        ("B2 · EC2 do runner",
         "sobe qc-runner-tf-ai e a\nregistra no projeto; a senha\ndo runner "
         "(token) fica no\ncofre SSM Parameter Store"),
        ("B3 · EC2 de observabilidade",
         "sobe qc-obs-tf-ai com\nCollector, Prometheus,\nLoki, Jaeger e "
         "Grafana"),
        ("B4 · clone + kit pronto",
         "clona o projeto e faz push\ndo kit: app RAG, testes e\nmódulo que "
         "empacota o app.\nSem .gitlab-ci.yml ainda,\nnenhum pipeline roda."),
    ]
    for i, (t, c) in enumerate(cards):
        d.cartao(CX0 + i * (CW + CG), CY, CW, CH, t, c)

    chips = [("1 · validar", "código; respostas\ndo assistente"),
             ("2 · planejar", "prévia (plan)\nde dev e prod"),
             ("3 · contrato", "confere nomes e\nnº de máquinas"),
             ("4 · deploy-dev", "cria dev\n(apply)"),
             ("5 · verificar-dev", "smoke e\nobservabilidade"),
             ("6 · deploy-prod", "cria prod, só\ncom dev ok"),
             ("7 · verificar-prod", "smoke e\nobservabilidade")]
    for x, (t, c) in zip(sx, chips):
        d.cartao(x, SY, SW, SH, t, c, tsize=18)

    d.cartao(RX, RY, RW, RH, "EC2 do runner\n(B2)",
             "qc-runner-tf-ai\n\nGitLab Runner: executa\ntodos os "
             "jobs.\n\nTerraform e testes\njá instalados.\n\nIAM LabRole: a EC2\n"
             "já tem a permissão;\nnenhuma chave AWS\nno GitLab.", icone="ec2", abaixo=True)
    d.cartao(S3X, S3Y, S3W, S3H, "Amazon S3\nbase-config-<seu RM>",
             "state do Terraform:\num arquivo por workspace\n(dev e prod).\n\n"
             "Já existe; o make\ndestroy não o apaga.",
             icone="s3", abaixo=True)

    d.texto("workspace dev (testes) · 1 instância", PNX + 14, DEVY + 9,
            PNW - 20, size=18, cor=DEV_COR, align="left")
    d.texto("workspace prod (clientes) · 2 instâncias", PNX + 14, PRODY + 9,
            PNW - 20, size=18, cor=PROD_COR, align="left")
    d.texto("os clientes\nreais acessam\npor este ALB", ALB_X - 78,
            alb_prod + 34 + 2 * 22.5 + 16, 156, size=18, cor=PROD_COR)
    for env, alb_y, ys in (("dev", alb_dev, ec2_dev),
                           ("prod", alb_prod, ec2_prod)):
        d.no("elb", ALB_X, alb_y, s=56)
        d.texto(f"ALB (balanceador)\nqc-rag-alb-{env}", ALB_X - 78,
                alb_y + 34, 156, size=18)
        for k, ey in enumerate(ys):
            d.no("ec2", EC2_X, ey, s=56)
            d.texto(f"qc-rag-{env}-{k + 1:03d}\nassistente RAG\n(app e documentos)",
                    EC2_X - 85, ey + 34, 170, size=18)

    d.no("otel", OCX, col["cy"], s=56)
    d.texto("OpenTelemetry Collector:\nrecebe log, métrica\ne trace das 3 instâncias", OCX - 120,
            col["cy"] + 34, 240, size=18)
    for x, ic, t in zip(bx, ("prometheus", "loki", "jaeger"),
                        ("Prometheus\nmétricas", "Loki\nlogs",
                         "Jaeger\ntraces")):
        d.no(ic, x, y_b, s=56)
        d.texto(t, x - 55, y_b + 34, 110, size=18)
    d.no("grafana", OCX, y_g, s=56)
    d.texto("Grafana: lê os\ntrês; você abre\nno navegador", OCX + 40,
            y_g - 34, 140, size=18, align="left")

    # --- rodape lateral: legenda e fim --------------------------------------
    LX, LYL, LWL = 30, AY + 60, 240
    itens = [(TEXTO, False, 3, "push e jobs\ndo pipeline", TEXTO),
             (CINZA, False, 2, "ALB distribui\nàs instâncias", TEXTO),
             (CINZA, True, 2, "preparação\ne state", ESCURO),
             (OTLP, True, 2, "telemetria:\nlog, métrica\ne trace", OTLP)]
    LHL = 56 + sum((t.count("\n") + 1) * 22.5 + 22 for *_, t, _ in itens)
    d.caixa(LX, LYL, LWL, LHL, "Legenda", TEXTO, bg=PAINEL, size=18)
    ly = LYL + 62
    for cor, tr, lg, t, tc in itens:
        d.seta([(LX + 18, ly + 12), (LX + 78, ly + 12)], cor=cor,
               tracejada=tr, largura=lg)
        d.texto(t, LX + 92, ly, LWL - 104, size=18, cor=tc, align="left")
        ly += (t.count("\n") + 1) * 22.5 + 22
    DY = LYL + LHL + 30
    d.caixa(LX, DY, LWL, 245, "Ao terminar:\nmake destroy", TEXTO, bg=PAINEL,
            size=18)
    d.texto("apaga dev, prod, o\nrunner e a EC2 de\nobservabilidade.\n"
            "A VPC fiap-lab e o\nbucket S3\nbase-config-<seu RM>\nficam.",
            LX + 14, DY + 66, LWL - 28, size=18, align="left")
    GLY = AY + AH + 30
    termos = [
        "Terraform: código que cria a infraestrutura (plan = prévia, apply = "
        "cria); workspace = cópia isolada; state = o registro do que criou.",
        "pipeline: esteira automática de etapas (stages) que roda a cada git "
        "push; runner = o servidor que executa as tarefas (jobs).",
        "RAG: o assistente responde com base nos documentos da empresa; smoke "
        "= teste rápido de que ele está no ar e responde.",
        "observabilidade: ver o sistema por dentro com log (o que aconteceu), "
        "métrica (números) e trace (o caminho de uma pergunta).",
        "EC2 = servidor virtual; ALB = balanceador; VPC = rede privada; S3 = "
        "guarda arquivos. Os demais termos estão no glossário do README.",
    ]
    GLH = 44 + len(termos) * 30.5 + 14
    d.caixa(LX, GLY, 1760, GLH, "Glossário (para quem é de negócio)", TEXTO,
            bg=PAINEL, size=18)
    for k, t in enumerate(termos):
        d.texto(t, LX + 14, GLY + 44 + k * 30.5, 1730, size=18, align="left")
    log(f"  glossario: maior termo {max(larg(t, 18) for t in termos):.0f} px")
    checar(d, "arquitetura")
    d.salvar(os.path.join(DIR, "arquitetura-trabalho-final.excalidraw"))


def pipeline():
    """Versao minima (corte do coordenador apos 14 rodadas): 7 stages em
    sequencia, cor por dono do job e UMA nota de que prod espera dev."""
    d = Diagrama()
    d.texto("Quantum Commerce · Trabalho Final: o pipeline que leva o "
            "Assistente RAG de dev a prod", 40, 20, 1760, size=30,
            cor=MAGENTA, align="left")
    d.texto("Empresa fictícia. Cada git push dispara o pipeline, que anda da "
            "esquerda para a direita, stage por stage. Se um job falha, o "
            "pipeline para ali.", 40, 64, 1780,
            size=20, cor=ESCURO, align="left")

    X0, CW, CG = 130, 210, 40
    cx = [X0 + i * (CW + CG) for i in range(7)]
    BY = 112
    HY = BY + 54
    KIT, SEU = KIT_BG, SEU_BG
    # L66: titulo do stage descritivo, nome real so no job
    stages = [
        ("1 · validar", "código e respostas\ndo assistente",
         [("fmt-validate", "formato do código", SEU),
          ("checkov", "segurança (avisa)", SEU),
          ("tf-test", "testa o Terraform", KIT),
          ("rag-quality-gate", "nota das respostas", KIT)]),
        ("2 · planejar", "prévia do que\nvai mudar",
         [("plan-dev", "prévia do dev", SEU),
          ("plan-prod", "prévia do prod", SEU)]),
        ("3 · contrato", "a prévia confere\ncom os requisitos",
         [("contrato-dev", "confere o dev", KIT),
          ("contrato-prod", "confere o prod", KIT)]),
        ("4 · implantar dev", "cria o ambiente\ndev na AWS",
         [("deploy-dev", "aplica a prévia", SEU)]),
        ("5 · verificar dev", "o ambiente dev\nresponde e aparece\n"
         "no monitoramento?",
         [("smoke-dev", "uma pergunta real", KIT),
          ("observabilidade-dev", "log, métrica, trace", KIT)]),
        ("6 · implantar prod", "cria o ambiente\nprod na AWS",
         [("deploy-prod", "aplica a prévia", SEU)]),
        ("7 · verificar prod", "o ambiente prod\nresponde e aparece\n"
         "no monitoramento?",
         [("smoke-prod", "uma pergunta real", KIT),
          ("observabilidade-prod", "log, métrica, trace", KIT)]),
    ]
    JY = HY + 42 + 3 * 22.5 + 14
    JH, JG = 66, 10
    SH = JY + 4 * JH + 3 * JG + 16 - HY
    NY = HY + SH + 30
    LY = NY + 3 * 22.5 + 40
    LH = 100
    d._base("rectangle", 0, 0, 1860, LY + LH + 30, strokeColor="transparent",
            backgroundColor="transparent", strokeWidth=1, roughness=0)

    for a, b, t, bg, cor in ((0, 2, "checagens (nada é criado na AWS)",
                              "#F1F3F5", TEXTO),
                             (3, 4, "ambiente dev (testes internos)", DEV_BG,
                              DEV_COR),
                             (5, 6, "ambiente prod (clientes reais)", PROD_BG,
                              PROD_COR)):
        x, w = cx[a], cx[b] + CW - cx[a]
        d.ret(x, BY, w, 38, bg=bg, sem_borda=True)
        d.texto(t, x, BY + 8, w, size=18, cor=cor)

    ya = HY + 22
    d.no("gitlab", 62, ya + 8, s=56)
    d.texto("git push\n(GitLab)", 12, ya + 44, 100, size=18)
    d.seta([(94, ya + 8), (cx[0] - 8, ya + 8)], cor=TEXTO, largura=3,
           roughness=0)
    for i in range(6):
        d.seta([(cx[i] + CW - 20, ya + 8), (cx[i + 1] - 8, ya + 8)],
               cor=TEXTO, largura=3, roughness=0)

    for i, (t, sub, jobs) in enumerate(stages):
        d.ret(cx[i], HY, CW, SH, cor=TEXTO, bg="#ffffff")
        d.texto(t, cx[i] + 12, HY + 12, CW - 24, size=19, align="left")
        d.texto(sub, cx[i] + 12, HY + 42, CW - 24, size=18, cor=ESCURO,
                align="left")
        for k, (nome, o_que, bg) in enumerate(jobs):
            y = JY + k * (JH + JG)
            d.ret(cx[i] + 10, y, CW - 20, JH, cor=TEXTO, bg=bg, largura=1)
            d.texto(nome, cx[i] + 18, y + 9, CW - 36, size=18, align="left")
            d.texto(o_que, cx[i] + 18, y + 35, CW - 36, size=17, cor=ESCURO,
                    align="left")
            for txt, sz in ((nome, 18), (o_que, 17), (sub, 18)):
                if larg(txt, sz) > CW - 40:
                    log(f"  LARGO: {txt!r} {larg(txt, sz):.0f}")

    xg = cx[5] - CG / 2
    d.seta([(xg, ya + 26), (xg, NY - 6)], cor=PROD_COR, largura=2,
           tracejada=True, ponta=False, roughness=0)
    nota = ("prod só depois de dev verificado: o job deploy-prod só roda\n"
            "se os jobs smoke-dev e observabilidade-dev passaram.")
    d.texto(nota, xg - larg(nota, 19) / 2 - 3, NY, larg(nota, 19) + 6,
            size=19, cor=PROD_COR, align="center")

    FW_ = cx[6] + CW - 40
    d._base("rectangle", 40, LY, FW_, LH, strokeColor=TEXTO,
            backgroundColor="#ffffff", strokeWidth=2, roughness=0)
    d.texto("Legenda · caixa grande numerada = stage (etapa); caixa pequena "
            "= job (tarefa); jobs do mesmo stage rodam juntos", 54, LY + 13,
            FW_ - 28, size=18, align="left")
    lx, ly = 60, LY + 52
    for bg, t in ((SEU_BG, "vocês escrevem este job no .gitlab-ci.yml"),
                  (KIT_BG, "pronto no kit: job e teste já vêm escritos")):
        d.ret(lx, ly, 46, 30, cor=TEXTO, bg=bg, largura=1)
        d.texto(t, lx + 58, ly + 4, larg(t, 18) + 6, size=18, align="left")
        lx += 58 + larg(t, 18) + 50
    d.seta([(lx, ly + 15), (lx + 60, ly + 15)], cor=TEXTO, largura=3,
           roughness=0)
    t = "ordem dos stages"
    d.texto(t, lx + 72, ly + 4, larg(t, 18) + 6, size=18, align="left")
    log(f"  legenda termina em {lx + 72 + larg(t, 18):.0f} de {40 + FW_:.0f}")

    checar(d, "pipeline")
    d.salvar(os.path.join(DIR, "pipeline-trabalho-final.excalidraw"))


if __name__ == "__main__":
    arquitetura()
    pipeline()
