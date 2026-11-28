"""Laboratorio do SpiderBot (python bot.py --analisar): testa regras nos percursos reais gravados pelo bot.

So leitura: nada aqui compra, vende ou muda o que o bot faz. Separado do bot.py a 21/11/2026, sem mudar
nenhuma regra nem nenhum calculo. Os auxiliares comuns (num, obter, repetir_percurso, ler_trades_paper, ...)
continuam no bot.py; ligar() copia-os para aqui quando o bot.py importa este modulo.
"""

import bisect
import json
import re
import time


def ligar(espaco):
    """Recebe os nomes do bot.py (chamado uma vez pelo bot.py). Nunca substitui os nomes deste modulo."""
    globals().update({k: v for k, v in espaco.items() if not k.startswith("__") and k not in _PROPRIOS})


def _serie_fina(pc):
    """Serie de precos tal como o bot a ve: pontos finos (preco de venda Jupiter) onde existem e pontos
    normais (DexScreener) no resto. Devolve [(seg, rel, e_fino), ...]. O ultimo ponto e sempre o ultimo
    ponto normal, que e o que marca o fim do percurso (tempo, rug ou desaparecimento)."""
    pts = [(num(p[0]), num(p[1]), False) for p in pc["pts"]]
    fim = pts[-1][0]
    finos = sorted((num(p[0]), num(p[1])) for p in (pc.get("ptsj") or [])
                   if isinstance(p, (list, tuple)) and len(p) == 2 and num(p[1]) > 0 and 0 < num(p[0]) < fim)
    if not finos:
        return pts
    tempos = [f[0] for f in finos]

    def coberto(seg):  # ha um ponto fino a menos de 45 s: o ponto normal e redundante (e mais atrasado)
        i = bisect.bisect_left(tempos, seg)
        return any(0 <= j < len(tempos) and abs(tempos[j] - seg) <= 45 for j in (i - 1, i))

    meio = [p for p in pts[1:-1] if not coberto(p[0])] + [(s, r, True) for s, r in finos]
    return [pts[0]] + sorted(meio, key=lambda p: p[0]) + [pts[-1]]


def repetir_serie(pc, serie, regra, cfg, valor_usd=12.0, custos="simulados"):
    """Como repetir_percurso, mas sobre uma serie [(seg, rel, e_fino)] e com dois modelos de custos:
      "simulados" - os do bot: slippage + taxa, a entrada e a saida (igual a repetir_percurso);
      "reais"     - entrada pela cotacao real do Jupiter gravada no sinal (se existir); saida pelo preco de
                    venda Jupiter quando o ponto de saida e fino (ja inclui impacto e comissao da pool);
                    mais a taxa de rede estimada, uma vez por transacao.
    Devolve (resultado liquido em %, motivo, usou_cotacao_real_na_entrada)."""
    stop, alvo, ativar, dist, minutos = regra
    pico, preco, motivo, trailing, fino = 1.0, 1.0, "fim", False, False
    for i, (seg, preco, fino) in enumerate(serie[1:], 1):
        pico = max(pico, preco)
        v = (preco - 1) * 100
        if preco <= 0:
            return -100.0, "desapareceu", False
        if i == len(serie) - 1 and pc.get("fim") == "rug":
            motivo = "rug"
            break
        if v <= stop:
            motivo = "stop"
            break
        if alvo and v >= alvo:
            motivo = "alvo"
            break
        if ativar and (pico - 1) * 100 >= ativar:
            trailing = True
        if trailing and preco <= pico * (1 - dist / 100.0):
            motivo = "trailing"
            break
        if seg >= minutos * 60:
            motivo = "tempo"
            break
    s_ent = cfg["slippage_pct"] / 100.0
    s_sai = s_ent
    if motivo == "rug":  # com a liquidez a desaparecer, a venda sai muito pior
        liq = num(pc.get("liq_fim"))
        s_sai = min(0.95, s_ent + (valor_usd / (liq / 2.0) if liq > 0 else 1.0))
    taxa = cfg["taxa_pct"] / 100.0
    if custos != "reais":
        liquido = (1 - taxa) / (1 + s_ent) * preco * (1 - s_sai) * (1 - taxa)
        return (liquido - 1) * 100.0, motivo, False
    real = num(obter(pc, "obs", "jupiter", "slippage_real_pct"), None)
    tem_real = real is not None and -10.0 <= real <= 50.0
    entrada = (1 + real / 100.0) if tem_real else (1 + s_ent) / (1 - taxa)
    # ponto fino = preco a que a posicao se vendia mesmo; ponto normal (ou rug) = preco medio, leva os custos simulados
    saida = preco if (fino and motivo != "rug") else preco * (1 - s_sai) * (1 - taxa)
    rede = 2 * max(0.0, num(cfg.get("lab_taxa_rede_usd"), 0.05)) / valor_usd if valor_usd > 0 else 0.0
    return (saida / entrada - rede - 1) * 100.0, motivo, tem_real


def _mediana(valores):
    v = sorted(valores)
    return v[len(v) // 2] if v else None


# Hipoteses escritas a 17/11/2026 com os primeiros 36 sinais (analise do Grok + laboratorio). NAO se mudam:
# so se medem nos sinais que vieram depois. Se falharem, deitam-se fora (nao se escolhe outra com os mesmos sinais).
HIPOTESES_CONGELADAS = {
    "data": "17/11/2026",
    "desde_sinal": 37,                 # o 37.o sinal (por ordem de tempo) e o primeiro que nenhuma regra viu
    "saida": (-20, 50, 0, 0, 30),      # A) stop -20%, alvo +50%, sem trailing, 30 min
    "pontuacao_max": 79,               # B) recusar pontuacao 80 ou mais (saida = regras atuais)
    "minimo": 30,                      # sinais novos para poder decidir
}


def _secao_hipoteses_congeladas(percursos, atual, cfg, h=None):
    """Mede as hipoteses congeladas SO nos sinais que chegaram depois de elas serem escritas."""
    h = h or HIPOTESES_CONGELADAS
    novos = percursos[h["desde_sinal"] - 1:]
    L = ["", f"  HIPOTESES CONGELADAS (escritas a {h['data']} com os primeiros {h['desde_sinal'] - 1} sinais; "
         "medidas SO nos sinais seguintes)"]
    if not novos:
        return L + [f"    Ainda nao ha sinais novos (comecam no {h['desde_sinal']}.o)."]
    saida, pmax = h["saida"], h["pontuacao_max"]
    L.append(f"    Sinais novos: {len(novos)} (do {h['desde_sinal']}.o em diante)")
    m0, a0 = _medir(novos, atual, cfg)
    L.append(f"    {'regras atuais (referencia)':<36}{len(novos):>3} trades   acerto {a0:5.1f}%   media {m0:+7.2f}%")
    mA, aA = _medir(novos, saida, cfg)
    melhorA = sum(1 for pc in novos if repetir_percurso(pc, saida, cfg)[0] > repetir_percurso(pc, atual, cfg)[0])
    L.append(f"    {'A) saida -20% / +50% / 30 min':<36}{len(novos):>3} trades   acerto {aA:5.1f}%   media {mA:+7.2f}%"
             f"   | melhor do que as atuais em {melhorA} de {len(novos)}")
    ficam = [pc for pc in novos if num(pc.get("pontuacao")) <= pmax]
    saem = [pc for pc in novos if num(pc.get("pontuacao")) > pmax]
    linha = f"    {'B) recusar pontuacao %d+' % (pmax + 1):<36}"
    if ficam:
        mB, aB = _medir(ficam, atual, cfg)
        linha += f"{len(ficam):>3} trades   acerto {aB:5.1f}%   media {mB:+7.2f}%"
    else:
        linha += "  0 trades"
    if saem:
        linha += f"   | recusados {len(saem)}: media {_medir(saem, atual, cfg)[0]:+.2f}% (com as regras atuais)"
    L.append(linha)
    com = [pc for pc in novos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    if com:
        valor = max(0.01, num(cfg.get("percurso_fino_valor_usd"), 5.5))

        def fr(grupo, regra):
            r = [repetir_serie(pc, _serie_fina(pc), regra, cfg, valor_usd=valor, custos="reais")[0] for pc in grupo]
            return f"{sum(r) / len(r):+7.2f}% ({len(r)})" if r else "    sem dados"
        L.append(f"    Com pontos finos e custos reais ({len(com)} dos {len(novos)} novos):")
        L.append(f"      atuais {fr(com, atual)}   A) {fr(com, saida)}   "
                 f"B) {fr([pc for pc in com if num(pc.get('pontuacao')) <= pmax], atual)}")
    falta = h["minimo"] - len(novos)
    L += [f"    Decisao: so com {h['minimo']} ou mais sinais novos" + (f" (faltam {falta})." if falta > 0 else " (ja ha)."),
          "    Uma hipotese so passa se ganhar as regras atuais aqui E com pontos finos e custos reais.",
          "    As hipoteses nao se mudam: se falharem, deitam-se fora (nao se escolhe outra com os mesmos sinais)."]
    return L


# 2.o conjunto (18/11/2026, com os primeiros 77 sinais): A e B falharam; o problema esta nas ENTRADAS.
# Duas hipoteses de entrada que se repetiram em todas as leituras. NAO se mudam; medem-se do 78.o sinal em diante.
HIPOTESES_ENTRADA = {
    "data": "18/11/2026",
    "desde_sinal": 78,
    "idade_min": 30,          # C) so entrar em tokens com 30 min ou mais
    "queda": (15, 35),        # D) depois do sinal, esperar um recuo de 15-35% e so entao entrar
    "minimo": 30,
}


HIPOTESES_SAIDA_D = {
    "data": "21/11/2026",
    "desde_sinal": 130,       # escritas com os primeiros 129 sinais; medidas SO a partir do 130.o
    "minimo": 30,
    "queda": (15, 35),        # entrada D (a que o bot usa desde 21/11/2026)
    "regras": (
        ("E) stop -30% / alvo +25% / 120 min", dict(stop=-30, alvo=25, trail_ativar=None, minutos=120)),
        ("F) stop -20% / alvo +50% / 120 min", dict(stop=-20, alvo=50, trail_ativar=None, minutos=120)),
    ),
}


# 4.o conjunto (26/11/2026, com os primeiros 195 sinais): a entrada D falhou e foi desligada; a prova dos nove
# passou pela 1.a vez com esta saida. Entrada logo no sinal. NAO se muda; mede-se do 196.o sinal em diante.
HIPOTESES_SAIDA_G = {
    "data": "26/11/2026",
    "desde_sinal": 196,
    "minimo": 30,
    "regra": (-20, 25, 0, 0, 30),     # G) stop -20%, alvo +25%, sem trailing, 30 min
}


def _secao_hipoteses_saida_g(percursos, atual, cfg, h=None):
    """Saida G (entrada logo no sinal), medida so nos sinais que chegaram depois de escrita."""
    h = h or HIPOTESES_SAIDA_G
    novos = percursos[h["desde_sinal"] - 1:]
    L = ["", f"  HIPOTESE DE SAIDA G (escrita a {h['data']} com os primeiros {h['desde_sinal'] - 1} sinais;"
         " medida SO nos sinais seguintes; entrada logo no sinal)"]
    if not novos:
        return L + [f"    Ainda nao ha sinais novos (comecam no {h['desde_sinal']}.o)."]
    g = h["regra"]
    com = [pc for pc in novos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    valor = max(0.01, num(cfg.get("percurso_fino_valor_usd"), 5.5))

    def finos(regra, custos):
        r = [repetir_serie(pc, _serie_fina(pc), regra, cfg, valor_usd=valor, custos=custos)[0] for pc in com]
        return f"{sum(r) / len(r):+7.2f}%" if r else "   sem dados"
    L.append(f"    Sinais novos: {len(novos)} (do {h['desde_sinal']}.o em diante); {len(com)} com pontos finos")
    for nome, regra in (("regras atuais (referencia)", atual), ("G) stop -20% / alvo +25% / 30 min", g)):
        m, a = _medir(novos, regra, cfg)
        L.append(f"    {nome:<36}{len(novos):>3} trades   acerto {a:5.1f}%   media {m:+7.2f}%"
                 f"   | finos {finos(regra, 'simulados')}   finos+reais {finos(regra, 'reais')}")
    falta = h["minimo"] - len(novos)
    L += [f"    Decisao: so com {h['minimo']} ou mais sinais novos" + (f" (faltam {falta})." if falta > 0 else " (ja ha)."),
          "    G so passa se ganhar as regras atuais com pontos normais E com pontos finos e custos reais.",
          "    Nao se muda: se falhar, deita-se fora."]
    return L


# 5.o conjunto (28/11/2026, com os primeiros 226 sinais): G falhou com o preco real (o stop -20% e apertado
# demais nos pontos finos). A familia stop -30% / alvo +25% esteve sempre no topo; esta variante e a melhor com
# preco real e custos reais. Entrada logo no sinal. NAO se muda; mede-se do 227.o sinal em diante.
HIPOTESES_SAIDA_H = {
    "data": "28/11/2026",
    "desde_sinal": 227,
    "minimo": 30,
    "regra": (-30, 25, 20, 15, 30),   # H) stop -30%, alvo +25%, trailing 20/15, 30 min
}


def _secao_hipoteses_saida_h(percursos, atual, cfg, h=None):
    """Saida H (entrada logo no sinal), medida so nos sinais que chegaram depois de escrita."""
    h = h or HIPOTESES_SAIDA_H
    novos = percursos[h["desde_sinal"] - 1:]
    L = ["", f"  HIPOTESE DE SAIDA H (escrita a {h['data']} com os primeiros {h['desde_sinal'] - 1} sinais;"
         " medida SO nos sinais seguintes; entrada logo no sinal)",
         "    (G, de 26/11, falhou com 31 sinais novos: so ganhava com pontos normais.)"]
    if not novos:
        return L + [f"    Ainda nao ha sinais novos (comecam no {h['desde_sinal']}.o)."]
    regra = h["regra"]
    com = [pc for pc in novos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    valor = max(0.01, num(cfg.get("percurso_fino_valor_usd"), 5.5))

    def finos(r_, custos):
        r = [repetir_serie(pc, _serie_fina(pc), r_, cfg, valor_usd=valor, custos=custos)[0] for pc in com]
        return f"{sum(r) / len(r):+7.2f}%" if r else "   sem dados"
    L.append(f"    Sinais novos: {len(novos)} (do {h['desde_sinal']}.o em diante); {len(com)} com pontos finos")
    for nome, r_ in (("regras atuais (referencia)", atual), ("H) stop -30% / alvo +25% / 20/15 / 30 min", regra)):
        m, a = _medir(novos, r_, cfg)
        L.append(f"    {nome:<42}{len(novos):>3} trades   acerto {a:5.1f}%   media {m:+7.2f}%"
                 f"   | finos {finos(r_, 'simulados')}   finos+reais {finos(r_, 'reais')}")
    falta = h["minimo"] - len(novos)
    L += [f"    Decisao: so com {h['minimo']} ou mais sinais novos" + (f" (faltam {falta})." if falta > 0 else " (ja ha)."),
          "    H so passa se ganhar as regras atuais com pontos finos E com pontos finos e custos reais",
          "    (o que o bot ve de verdade); os pontos normais sao so referencia. Nao se muda: se falhar, deita-se fora."]
    return L


def _pts_finos(pc):
    """O percurso com a serie fina (preco de venda Jupiter onde existe), no formato dos pontos normais."""
    return dict(pc, pts=[[sg, rl] for sg, rl, _ in _serie_fina(pc)])


def _secao_hipoteses_saida_d(percursos, cfg, h=None):
    """Saidas congeladas para a entrada D, medidas so nos sinais que chegaram depois de escritas."""
    h = h or HIPOTESES_SAIDA_D
    novos = percursos[h["desde_sinal"] - 1:]
    L = ["", f"  HIPOTESES DE SAIDA PARA A ENTRADA D (escritas a {h['data']} com os primeiros {h['desde_sinal'] - 1}"
         " sinais; medidas SO nos sinais seguintes)"]
    if not novos:
        return L + [f"    Ainda nao ha sinais novos (comecam no {h['desde_sinal']}.o)."]
    atual = dict(stop=cfg["stop_loss_pct"], parcial_2x=False, trail_dist=cfg["trailing_distancia_pct"],
                 trail_ativar=cfg["trailing_ativar_pct"] or None, alvo=cfg["take_profit_pct"] or None,
                 minutos=cfg["tempo_maximo_min"], queda=h["queda"])
    finos = [_pts_finos(pc) for pc in novos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    L.append(f"    Sinais novos: {len(novos)} (do {h['desde_sinal']}.o em diante); {len(finos)} com pontos finos")

    def linha(nome, regra):
        n, m, a = _medir_avancado(novos, cfg, **regra)
        nf, mf, af = _medir_avancado(finos, cfg, **regra) if finos else (0, None, None)
        txt = f"    {nome:<40}" + (f"{n:>3} trades   acerto {a:5.1f}%   media {m:+7.2f}%" if n else "  0 trades")
        return txt + (f"   | pontos finos {mf:+7.2f}% ({nf})" if nf else "")
    L.append(linha("D + saidas atuais (referencia)", atual))
    for nome, regra in h["regras"]:
        L.append(linha(nome, dict(atual, parcial_2x=False, trail_dist=cfg["trailing_distancia_pct"], **regra)))
    falta = h["minimo"] - len(novos)
    L += [f"    Decisao: so com {h['minimo']} ou mais sinais novos" + (f" (faltam {falta})." if falta > 0 else " (ja ha)."),
          "    Uma hipotese de saida so passa se ganhar a referencia com pontos normais E com pontos finos.",
          "    As hipoteses nao se mudam: se falharem, deitam-se fora."]
    return L


def _secao_hipoteses_entrada(percursos, cfg, h=None):
    """Hipoteses de ENTRADA congeladas (C e D), medidas so nos sinais que chegaram depois de escritas."""
    h = h or HIPOTESES_ENTRADA
    novos = percursos[h["desde_sinal"] - 1:]
    L = ["", f"  HIPOTESES DE ENTRADA CONGELADAS (escritas a {h['data']} com os primeiros {h['desde_sinal'] - 1} sinais;"
         " medidas SO nos sinais seguintes)",
         "    (A e B, as de 17/11, falharam com 41 sinais novos e foram postas de parte.)"]
    if not novos:
        return L + [f"    Ainda nao ha sinais novos (comecam no {h['desde_sinal']}.o)."]
    atual = dict(stop=cfg["stop_loss_pct"], parcial_2x=False, trail_dist=cfg["trailing_distancia_pct"],
                 trail_ativar=cfg["trailing_ativar_pct"] or None, alvo=cfg["take_profit_pct"] or None,
                 minutos=cfg["tempo_maximo_min"])
    L.append(f"    Sinais novos: {len(novos)} (do {h['desde_sinal']}.o em diante)")
    n0, m0, a0 = _medir_avancado(novos, cfg, **atual)
    if n0:
        L.append(f"    {'regras atuais (referencia)':<36}{n0:>3} trades   acerto {a0:5.1f}%   media {m0:+7.2f}%")
    idade = lambda pc: num(obter(pc, "feat", "idade_min", defeito=None), -1)
    ficam = [pc for pc in novos if idade(pc) >= h["idade_min"]]
    saem = [pc for pc in novos if 0 <= idade(pc) < h["idade_min"]]
    sem_dado = len(novos) - len(ficam) - len(saem)
    linha = f"    {'C) so tokens com %d min ou mais' % h['idade_min']:<36}"
    nc, mc, ac = _medir_avancado(ficam, cfg, **atual) if ficam else (0, None, None)
    linha += f"{nc:>3} trades   acerto {ac:5.1f}%   media {mc:+7.2f}%" if nc else "  0 trades"
    if saem:
        linha += f"   | saem {len(saem)}: media {_medir_avancado(saem, cfg, **atual)[1]:+.2f}%"
    if sem_dado:
        linha += f"   | sem idade: {sem_dado}"
    L.append(linha)
    nd, md, ad = _medir_avancado(novos, cfg, **dict(atual, queda=h["queda"]))
    q0, q1 = h["queda"]
    L.append(f"    {'D) entrar na queda de %d-%d%%' % (q0, q1):<36}" +
             (f"{nd:>3} trades   acerto {ad:5.1f}%   media {md:+7.2f}%   | sem entrada (nao caiu): {len(novos) - nd}"
              if nd else "  0 trades (nenhum recuou o suficiente)"))
    com = [pc for pc in ficam if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    todos_f = [pc for pc in novos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    if todos_f:
        valor = max(0.01, num(cfg.get("percurso_fino_valor_usd"), 5.5))
        regra = (cfg["stop_loss_pct"], cfg["take_profit_pct"], cfg["trailing_ativar_pct"],
                 cfg["trailing_distancia_pct"], cfg["tempo_maximo_min"])

        def fr(grupo):
            r = [repetir_serie(pc, _serie_fina(pc), regra, cfg, valor_usd=valor, custos="reais")[0] for pc in grupo]
            return f"{sum(r) / len(r):+7.2f}% ({len(r)})" if r else "    sem dados"
        L.append(f"    Com pontos finos e custos reais: atuais {fr(todos_f)}   C) {fr(com)}   "
                 "(D usa os pontos normais)")

        fino_pts = _pts_finos
        sim = [repetir_serie(pc, _serie_fina(pc), regra, cfg)[0] for pc in todos_f]
        nf, mf, af = _medir_avancado([fino_pts(pc) for pc in todos_f], cfg, **dict(atual, queda=h["queda"]))
        L.append(f"    D com pontos finos (custos simulados): atuais {sum(sim) / len(sim):+7.2f}% ({len(sim)})   "
                 + (f"D) {mf:+7.2f}% ({nf}, acerto {af:.1f}%)" if nf else "D) sem entradas"))
    falta = h["minimo"] - len(novos)
    L += [f"    Decisao: so com {h['minimo']} ou mais sinais novos" + (f" (faltam {falta})." if falta > 0 else " (ja ha)."),
          "    Uma hipotese de entrada so passa se ganhar as regras atuais com margem clara E nao deixar quase tudo de fora.",
          "    As hipoteses nao se mudam: se falharem, deitam-se fora."]
    return L


def _secao_pontos_finos(percursos, melhores, atual, cfg):
    """As mesmas regras, nos mesmos percursos, com pontos normais e com pontos finos, e com os dois custos."""
    com = [pc for pc in percursos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    L = ["", f"  PONTOS FINOS (preco de venda real no Jupiter) - {len(com)} de {len(percursos)} percursos com pontos finos"]
    if not com:
        return L + [f"    Ainda sem dados: o bot grava-os nos primeiros {num(cfg.get('percurso_fino_minutos'), 30):.0f} min "
                    "de cada sinal desde 15/11/2026; aparecem aqui quando esses percursos fecharem."]
    valor = max(0.01, num(cfg.get("percurso_fino_valor_usd"), 5.5))
    series = [(pc, _serie_fina(pc)) for pc in com]

    def medir(regra, modo):
        if modo == "normais":
            r = [repetir_percurso(pc, regra, cfg)[0] for pc, _ in series]
        elif modo == "finos":
            r = [repetir_serie(pc, s, regra, cfg)[0] for pc, s in series]
        else:
            r = [repetir_serie(pc, s, regra, cfg, valor_usd=valor, custos="reais")[0] for pc, s in series]
        return sum(r) / len(r), sum(1 for x in r if x > 0) / len(r) * 100.0

    def intervalos(chave):
        saltos = []
        for pc in com:
            tempos = sorted(num(p[0]) for p in pc.get(chave) or [] if isinstance(p, (list, tuple)) and len(p) == 2)
            saltos += [b - a for a, b in zip(tempos, tempos[1:]) if b > a]
        return _mediana(saltos)

    i_fino, i_normal = intervalos("ptsj"), intervalos("pts")
    if i_fino is not None and i_normal is not None:
        L.append(f"    Intervalo mediano entre pontos: finos {i_fino:.0f} s, normais {i_normal:.0f} s")
    com_real = sum(1 for pc, s in series if repetir_serie(pc, s, atual, cfg, valor_usd=valor, custos="reais")[2])
    L.append("    REGRAS ATUAIS, nos mesmos percursos:")
    for nome, modo in (("pontos normais, custos simulados", "normais"), ("pontos finos,   custos simulados", "finos"),
                       ("pontos finos,   custos reais", "reais")):
        media, acerto = medir(atual, modo)
        L.append(f"      {nome:<34} acerto {acerto:5.1f}%   media {media:+6.2f}% por trade")
    L.append(f"      (custos reais: {com_real} de {len(com)} com cotacao real de entrada; taxa de rede "
             f"${num(cfg.get('lab_taxa_rede_usd'), 0.05):.2f} por transacao, HIPOTESE por medir; operacao de ${valor:.2f})")
    if melhores:
        L.append("    AS MELHORES COMBINACOES (escolhidas com pontos normais em todos os sinais), nestes percursos:")
        for regra in melhores[:5]:
            a, b, c = medir(regra, "normais")[0], medir(regra, "finos")[0], medir(regra, "reais")[0]
            L.append(f"      {_regra_txt(regra)}   normais {a:+6.2f}%   finos {b:+6.2f}%   finos+reais {c:+6.2f}%")
    L += ["    - 'pontos finos, custos simulados' e o mais parecido com o que o bot Paper regista hoje.",
          "    - Se uma regra so ganha com pontos normais, o ganho vem da lentidao dos dados, nao da regra."]
    if len(com) < 40:
        L.append(f"    - ATENCAO: {len(com)} percursos com pontos finos e pouco; abaixo de 40 isto e sobretudo sorte.")
    return L


def _secao_por_dia(cfg):
    """Operacoes Paper fechadas, por dia (resultado real registado pelo bot, nao o do laboratorio)."""
    L = ["", "  OPERACOES PAPER FECHADAS, POR DIA (o que o bot registou mesmo, nao o laboratorio)"]
    try:
        todas = [t for t in (ler_trades_paper(cfg) or []) if t.get("tipo") == "VENDA" and t.get("pnl_usd") is not None]
    except Exception:
        todas = []
    manuais = [t for t in todas if "[manual]" in str(t.get("motivo") or "")]
    vendas = [t for t in todas if "[manual]" not in str(t.get("motivo") or "")]
    if not todas:
        return L + ["    Ainda sem operacoes fechadas (ou trades.csv indisponivel)."]

    def por_dia(grupo):
        dias = {}
        for t in grupo:
            dias.setdefault(str(t.get("data_hora") or "")[:10], []).append(t)
        for dia in sorted(dias):
            g = dias[dia]
            pnl = sum(num(t["pnl_usd"]) for t in g)
            ganhas = sum(1 for t in g if num(t["pnl_usd"]) > 0)
            media = sum(num(t.get("pnl_pct")) for t in g) / len(g)
            L.append(f"    {dia}   {len(g):>3} fechadas   {ganhas:>2} ganhas   resultado ${pnl:+7.2f}   media {media:+6.2f}% por operacao")
    por_dia(vendas)
    if manuais:
        L.append("  COMPRAS MANUAIS (escolhas tuas; nao contam acima)")
        por_dia(manuais)
    L += ["    As regras do bot mudaram pelo caminho (confirmacao manual e limite de 5 EUR desde 14/11, preco rapido",
          "    desde 15/11): compara dias com as mesmas regras, nao o total."]
    return L


def repetir_avancado(pc, cfg, queda=None, stop=-25.0, parcial_2x=True, trail_dist=20.0, trail_ativar=None,
                     alvo=None, minutos=180, valor_usd=12.0):
    """Laboratorio: estrategia com entrada na queda e/ou venda de metade a 2x (so simulacao).

    queda=(min, max): so compra quando o preco recua entre min% e max% desde o pico apos o sinal
    (se cair mais do que max% de uma vez, nao compra). parcial_2x: vende metade ao dobrar; o resto
    sai por trailing (trail_dist% abaixo do pico), stop, tempo ou rug.
    Devolve (resultado liquido em %, motivo) ou None se nunca chegou a comprar."""
    pts = pc["pts"]
    if not pts or len(pts) < 2:
        return None
    s_ent = cfg["slippage_pct"] / 100.0
    taxa = cfg["taxa_pct"] / 100.0
    rug_final = pc.get("fim") == "rug"
    inicio, entrada = 0, 1.0
    if queda:
        lo, hi = queda
        pico = 1.0
        inicio = None
        for i, (seg, preco) in enumerate(pts[1:], 1):
            if preco <= 0 or (i == len(pts) - 1 and rug_final):
                break
            pico = max(pico, preco)
            recuo = (1 - preco / pico) * 100
            if recuo > hi:          # caiu demais desde o pico: faca a cair, so compra se voltar a zona 15-35%
                continue
            if recuo >= lo:
                inicio, entrada = i, preco
                break
        if inicio is None:
            return None
    t0 = pts[inicio][0]
    vendido, recebido = 0.0, 0.0     # fracao da posicao ja vendida e valor recebido (relativo ao preco de entrada)
    pico, trailing, motivo, rel = 1.0, False, "fim", 1.0
    for i in range(inicio + 1, len(pts)):
        seg, preco = pts[i]
        if preco <= 0:
            motivo, rel = "desapareceu", 0.0
            break
        rel = preco / entrada
        pico = max(pico, rel)
        v = (rel - 1) * 100
        if i == len(pts) - 1 and rug_final:
            motivo = "rug"
            break
        if v <= stop:
            motivo = "stop"
            break
        if alvo and v >= alvo:
            motivo = "alvo"
            break
        if parcial_2x and vendido == 0 and rel >= 2.0:
            vendido, recebido = 0.5, 0.5 * rel * (1 - s_ent) * (1 - taxa)  # vende metade ao preco visto
            trailing = True
        if trail_ativar is not None and (pico - 1) * 100 >= trail_ativar:
            trailing = True
        if trailing and rel <= pico * (1 - trail_dist / 100.0):
            motivo = "trailing"
            break
        if seg - t0 >= minutos * 60:
            motivo = "tempo"
            break
    s_sai = s_ent
    if motivo == "rug":
        liq = num(pc.get("liq_fim"))
        s_sai = min(0.95, s_ent + (valor_usd / (liq / 2.0) if liq > 0 else 1.0))
    resto = (1 - vendido) * rel * (1 - s_sai) * (1 - taxa)
    liquido = (1 - taxa) / (1 + s_ent) * (recebido + resto)
    return (liquido - 1) * 100.0, motivo


def _medir_avancado(percursos, cfg, **regra):
    r = [x for x in (repetir_avancado(pc, cfg, **regra) for pc in percursos) if x is not None]
    if not r:
        return 0, None, None
    return len(r), sum(x[0] for x in r) / len(r), sum(1 for x in r if x[0] > 0) / len(r) * 100.0


def _secao_estrategia_post(percursos, todos, cfg):
    """Estrategia vista num post do X: entrada na queda de 15-35%, metade a 2x + trailing, stop -25%."""
    atual = dict(stop=cfg["stop_loss_pct"], parcial_2x=False, trail_dist=cfg["trailing_distancia_pct"],
                 trail_ativar=cfg["trailing_ativar_pct"] or None, alvo=cfg["take_profit_pct"] or None,
                 minutos=cfg["tempo_maximo_min"])
    variantes = (
        ("regras atuais (referencia)", atual),
        ("post completo", dict(queda=(15, 35), stop=-25, parcial_2x=True, trail_dist=20, minutos=180)),
        ("so entrada na queda", dict(atual, queda=(15, 35))),
        ("so saida do post", dict(stop=-25, parcial_2x=True, trail_dist=20, minutos=180)),
        ("so stop -25%", dict(atual, stop=-25)),
    )
    L = ["", "  ESTRATEGIA DO POST (X): comprar na queda de 15-35%, vender metade a 2x + trailing 20%, stop -25%"]
    for titulo, grupo in (("sinais aprovados", percursos), ("aprovados + controlo", todos)):
        if not grupo:
            continue
        L.append(f"    {titulo} ({len(grupo)} percursos):")
        for nome, regra in variantes:
            n, media, acerto = _medir_avancado(grupo, cfg, **regra)
            if n == 0:
                L.append(f"      {nome:<28} nunca chegou a comprar")
                continue
            sem = len(grupo) - n
            extra = f"   ({sem} sem entrada)" if sem else ""
            L.append(f"      {nome:<28} {n:>4} trades   media {media:+6.2f}%   acerto {acerto:5.1f}%{extra}")
    L.append("    Teste so no laboratorio: nada disto muda o que o bot compra ou vende.")
    return L


def ler_percursos(cfg):
    percursos = []
    try:
        with open(cfg["ficheiro_percursos"], "r", encoding="utf-8") as f:
            for linha in f:
                try:
                    pc = json.loads(linha)
                    if isinstance(pc.get("pts"), list) and len(pc["pts"]) >= 2:
                        percursos.append(pc)
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    return sorted(percursos, key=lambda pc: num(pc.get("ts")))


def _medir(percursos, regra, cfg):
    r = [repetir_percurso(pc, regra, cfg)[0] for pc in percursos]
    return sum(r) / len(r), sum(1 for x in r if x > 0) / len(r) * 100.0


def _regra_txt(regra):
    stop, alvo, ativar, dist, minutos = regra
    return (f"stop {stop:>4.0f}%  alvo {('+%d%%' % alvo) if alvo else 'sem':>5}  "
            f"trailing {('%d/%d' % (ativar, dist)) if ativar else 'nao':>6}  {minutos:>3} min")


def analisar(cfg):
    """Devolve o texto do relatorio do laboratorio."""
    lidos = ler_percursos(cfg)
    manuais = [pc for pc in lidos if pc.get("grupo") == "manual"]
    todos = [pc for pc in lidos if pc.get("grupo") != "manual"]
    percursos = [pc for pc in todos if pc.get("grupo", "sinal") != "controlo"]
    controlo = [pc for pc in todos if pc.get("grupo") == "controlo"]
    n = len(percursos)
    L = ["", "=" * 74, "  LABORATORIO - regras testadas nos percursos REAIS gravados pelo bot", "=" * 74]
    if n == 0:
        L += ["  Ainda nao ha percursos completos de sinais aprovados.",
              f"  O bot grava o preco de cada sinal durante {cfg['percurso_minutos']} minutos.",
              "  Deixa-o a correr e volta a tentar mais tarde."]
        if controlo:
            atual = (cfg["stop_loss_pct"], cfg["take_profit_pct"], cfg["trailing_ativar_pct"],
                     cfg["trailing_distancia_pct"], cfg["tempo_maximo_min"])
            L += _secao_controlo(percursos, controlo, atual, cfg)
            L += _secao_caracteristicas(todos, atual, cfg)
            L += _secao_estrategia_post(percursos, todos, cfg)
        L += _secao_manuais(manuais, regra_atual(cfg), cfg)
        return "\n".join(L)
    dias = (num(percursos[-1].get("ts")) - num(percursos[0].get("ts"))) / 86400.0
    L.append(f"  Sinais analisados: {n}   (ao longo de {dias:.1f} dias)")
    if n < 100:
        L.append(f"  ATENCAO: {n} sinais e pouco. Abaixo de 100 os resultados sao sobretudo sorte.")
    atual = (cfg["stop_loss_pct"], cfg["take_profit_pct"], cfg["trailing_ativar_pct"],
             cfg["trailing_distancia_pct"], cfg["tempo_maximo_min"])
    media, acerto = _medir(percursos, atual, cfg)
    L += ["", "  REGRAS ATUAIS", f"    {_regra_txt(atual)}   acerto {acerto:5.1f}%   media {media:+6.2f}% por trade"]

    # ate onde chegaram os precos (responde a: vale a pena deixar correr os ganhos?)
    maximos = [max(p[1] for p in pc["pts"]) for pc in percursos]
    minimos = [min(p[1] for p in pc["pts"]) for pc in percursos]
    L += ["", f"  O QUE OS PRECOS FIZERAM (nos {cfg['percurso_minutos']} min depois do sinal)"]
    for alvo in (25, 50, 100, 300):
        L.append(f"    chegaram a +{alvo}%: {sum(1 for m in maximos if m >= 1 + alvo / 100) / n * 100:5.1f}%")
    L.append(f"    cairam 50% ou mais:  {sum(1 for m in minimos if m <= 0.5) / n * 100:5.1f}%")
    L.append(f"    acabaram em rug ou desapareceram: {sum(1 for pc in percursos if pc.get('fim') != 'tempo') / n * 100:5.1f}%")

    grelha = [(s, a, t[0], t[1], m) for s in (-6, -10, -15, -20, -30) for a in (25, 50, 100, 0)
              for t in ((0, 0), (10, 8), (20, 15), (30, 20)) for m in (30, 60, 120, 180)
              if m <= cfg["percurso_minutos"]]
    if not grelha:
        return "\n".join(L + ["", "  (percurso_minutos e demasiado curto para testar combinacoes)", "=" * 74])
    resultados = sorted(((_medir(percursos, r, cfg), r) for r in grelha), key=lambda x: -x[0][0])
    L += ["", f"  AS 8 MELHORES de {len(grelha)} combinacoes (por media por trade)"]
    for (media, acerto), r in resultados[:8]:
        L.append(f"    {_regra_txt(r)}   acerto {acerto:5.1f}%   media {media:+6.2f}%")
    positivas = sum(1 for (media, _), _ in resultados if media > 0)
    L.append(f"    Combinacoes com media positiva: {positivas} de {len(grelha)}")

    if n >= 40:  # a melhor regra da 1.a metade continua boa na 2.a? (protege contra "regra feita a medida")
        a, b = percursos[:n // 2], percursos[n // 2:]
        melhor = max(grelha, key=lambda r: _medir(a, r, cfg)[0])
        m_a, _ = _medir(a, melhor, cfg)
        m_b, ac_b = _medir(b, melhor, cfg)
        m_at, ac_at = _medir(b, atual, cfg)
        dif = m_b - m_at
        L += ["", "  PROVA DOS NOVE (a melhor regra da 1.a metade dos sinais, testada na 2.a metade)",
              f"    {_regra_txt(melhor)}",
              f"    1.a metade: {m_a:+6.2f}% por trade     2.a metade: {m_b:+6.2f}% por trade (acerto {ac_b:.1f}%)",
              f"    regras atuais na mesma 2.a metade ({len(b)} sinais): {m_at:+6.2f}% por trade (acerto {ac_at:.1f}%)",
              f"    -> na 2.a metade a regra escolhida fica {abs(dif):.2f} pontos "
              + ("ACIMA" if dif > 0 else "ABAIXO") + " das regras atuais",
              "    Se a 2.a metade for muito pior, a regra so 'acertou' por acaso - nao confies nela."]
    L += _secao_hipoteses_congeladas(percursos, atual, cfg)
    L += _secao_hipoteses_entrada(percursos, cfg)
    L += _secao_hipoteses_saida_d(percursos, cfg)
    L += _secao_hipoteses_saida_g(percursos, atual, cfg)
    L += _secao_hipoteses_saida_h(percursos, atual, cfg)

    L += _secao_pontos_finos(percursos, [r for _, r in resultados[:5]], atual, cfg)
    L += _secao_entrada_adiada(percursos, atual, cfg)
    L += _secao_observacao_extra(percursos, atual, cfg)
    L += ["", "  POR FONTE DE DESCOBERTA (com as regras atuais)"]
    for fonte in ("dexscreener", "geckoterminal"):
        grupo = [pc for pc in percursos if pc.get("fonte", "dexscreener") == fonte]
        if grupo:
            media, acerto = _medir(grupo, atual, cfg)
            L.append(f"    {fonte:<14} {len(grupo):>4} sinais   acerto {acerto:5.1f}%   media {media:+6.2f}%")
    L += ["", "  POR PONTUACAO DO ANALISTA (com as regras atuais)"]
    for lo, hi in ((70, 79), (80, 89), (90, 100)):
        grupo = [pc for pc in percursos if lo <= num(pc.get("pontuacao")) <= hi]
        if grupo:
            media, acerto = _medir(grupo, atual, cfg)
            L.append(f"    {lo}-{hi}: {len(grupo):>4} sinais   acerto {acerto:5.1f}%   media {media:+6.2f}%")
    L += _secao_controlo(percursos, controlo, atual, cfg)
    L += _secao_caracteristicas(todos, atual, cfg)
    L += _secao_pontuacao_por_criterio(percursos, atual, cfg)
    L += _secao_filtros_a_testar(percursos, atual, cfg)
    L += _secao_carteiras(todos, atual, cfg)
    L += _secao_estrategia_post(percursos, todos, cfg)
    L += _secao_manuais(manuais, atual, cfg)
    L += _secao_por_dia(cfg)
    L += ["", "  COMO LER", "    - 'media' ja inclui os custos simulados (slippage e taxas, a entrada e a saida).",
          "    - Fora da seccao PONTOS FINOS, tudo usa os pontos normais (DexScreener, ~30 em 30 s).",
          "    - Conta cada sinal como um trade, sem o limite de posicoes abertas.",
          "    - Se NENHUMA combinacao tiver media positiva, o problema esta nas entradas (o Analista),",
          "      nao nas saidas: nenhuma regra de venda salva compras sem vantagem.",
          "    - Resultado em simulacao nao garante resultado com dinheiro real.", "=" * 74]
    return "\n".join(L)


def _secao_manuais(manuais, regra, cfg):
    """Tokens acompanhados a pedido (botao Acompanhar). Nao entram em nenhuma estatistica do laboratorio.
    Mostra as regras atuais e o stop alternativo lado a lado, para comparares as duas com os teus tokens."""
    if not manuais:
        return []
    alt = regra_alternativa(cfg)
    L = ["", f"  TOKENS ACOMPANHADOS A MAO ({len(manuais)}; NAO entram nas estatisticas acima)",
         f"    regras atuais (stop {regra[0]:.0f}%) contra o mesmo com stop {alt[0]:.0f}%"]
    for pc in manuais[-15:]:
        res, motivo = repetir_percurso(pc, regra, cfg)
        res_a, motivo_a = repetir_percurso(pc, alt, cfg)
        pico = (max(p[1] for p in pc["pts"]) - 1) * 100
        p = pc.get("pontuacao")
        L.append(f"    {time.strftime('%d/%m %H:%M', time.localtime(num(pc.get('ts'))))}  ${str(pc.get('simbolo'))[:10]:<10} "
                 f"pontos {('%3d' % p) if p is not None else '  -'}   atuais {res:+7.2f}% ({motivo:<8})  "
                 f"stop {alt[0]:.0f}% {res_a:+7.2f}% ({motivo_a:<8})  pico {pico:+6.1f}%   fim {pc.get('fim')}")
    r1 = [repetir_percurso(pc, regra, cfg)[0] for pc in manuais]
    r2 = [repetir_percurso(pc, alt, cfg)[0] for pc in manuais]
    r3 = [_simular_recuperar([[num(p[0]), num(p[1])] for p in pc["pts"]], regra, pc.get("fim"), pc.get("liq_fim"), cfg)["pct"]
          for pc in manuais]
    melhor = sum(1 for a, b in zip(r1, r2) if b > a + 0.01)
    pior = sum(1 for a, b in zip(r1, r2) if b < a - 0.01)
    L.append(f"    media: atuais {sum(r1) / len(r1):+.2f}%   stop {alt[0]:.0f}% {sum(r2) / len(r2):+.2f}% por token   "
             f"| stop {alt[0]:.0f}% melhor em {melhor}, pior em {pior}, igual em {len(r1) - melhor - pior}")
    m3 = sum(1 for a, b in zip(r1, r3) if b > a + 0.01)
    L.append(f"    recuperar entrada a 2x: media {sum(r3) / len(r3):+.2f}% por token | melhor do que as atuais em {m3} de {len(r3)}")
    if len(manuais) < 20:
        L.append("    Com menos de 20 tokens acompanhados isto ainda e sobretudo sorte.")
    return L


def _linha_grupo(nome, grupo, regra, cfg):
    media, acerto = _medir(grupo, regra, cfg)
    pico = sum(max(p[1] for p in pc["pts"]) for pc in grupo) / len(grupo)
    rugs = sum(1 for pc in grupo if pc.get("fim") != "tempo") / len(grupo) * 100.0
    return (f"    {nome:<26} {len(grupo):>4}   media {media:+6.2f}%   acerto {acerto:5.1f}%   "
            f"pico medio {100 * (pico - 1):+6.1f}%   rug {rugs:5.1f}%")


def _secao_controlo(percursos, controlo, regra, cfg):
    """Aprovados vs. recusados (grupo de controlo), com as mesmas regras de saida."""
    L = ["", "  APROVADOS vs. RECUSADOS (grupo de controlo, com as regras atuais)"]
    if not controlo:
        return L + ["    Ainda sem grupo de controlo: so os percursos gravados depois desta versao o tem."]
    if percursos:
        L.append(_linha_grupo("aprovados (sinais)", percursos, regra, cfg))
    L.append(_linha_grupo("recusados (controlo)", controlo, regra, cfg))
    L.append("    Se os recusados derem igual ou melhor do que os aprovados, a pontuacao/filtros nao estao a ajudar.")
    return L


def _secao_caracteristicas(todos, regra, cfg):
    """Resultado por caracteristica do par no momento do sinal (aprovados + controlo)."""
    com = [pc for pc in todos if isinstance(pc.get("feat"), dict)]
    L = ["", f"  POR CARACTERISTICA DE ENTRADA - {len(com)} percursos com dados (aprovados + controlo)"]
    if not com:
        return L + ["    Ainda sem dados: so os percursos gravados depois desta versao os tem."]

    def f(pc, k):
        v = pc["feat"].get(k)
        return num(v, None) if v is not None else None

    def racio(pc, janela):
        c, v = f(pc, "compras_" + janela), f(pc, "vendas_" + janela)
        return c / v if c is not None and v else None

    blocos = (
        ("subida 5 min", lambda pc: f(pc, "m5"), ((None, 0, "< 0%"), (0, 10, "0 a 10%"), (10, 30, "10 a 30%"), (30, None, "> 30%"))),
        ("subida 1 h", lambda pc: f(pc, "h1"), ((None, 0, "< 0%"), (0, 50, "0 a 50%"), (50, 100, "50 a 100%"), (100, None, "> 100%"))),
        ("compras/vendas 5 min", lambda pc: racio(pc, "m5"), ((None, 1, "< 1 (mais vendas)"), (1, 1.5, "1 a 1.5"), (1.5, None, "> 1.5"))),
        ("idade do par", lambda pc: f(pc, "idade_min"), ((None, 30, "< 30 min"), (30, 120, "30 min a 2 h"), (120, None, "> 2 h"))),
    )
    for titulo, valor, faixas in blocos:
        L.append(f"    {titulo}:")
        for lo, hi, nome in faixas:
            g = [pc for pc in com if valor(pc) is not None and (lo is None or valor(pc) >= lo)
                 and (hi is None or valor(pc) < hi)]
            if g:
                L.append("  " + _linha_grupo(nome, g, regra, cfg))
    L.append("    Com poucos percursos por faixa (menos de 20) as diferencas ainda sao sobretudo sorte.")
    return L


def _top10_sem_pool(pc):
    """Top 10 de holders sem a conta da pool do par; None se nao der para saber."""
    top, pool = obter(pc, "obs", "holders", "top"), pc.get("par")
    validos = [x for x in top if isinstance(x, list) and len(x) == 2] if isinstance(top, list) else []
    if not pool or not any(x[0] == pool for x in validos):
        return None
    return sum(num(x[1]) for x in [x for x in validos if x[0] != pool][:10])


CRITERIOS_PONTUACAO = (("liquidez", "Liquidez", 20), ("mcap/liq", "Market cap/liquidez", 10), ("idade", "Idade", 10),
                       ("volume", "Volume", 20), ("compras", "Pressao compradora", 20), ("momento", "Momento", 5),
                       ("acumulacao", "Acumulacao", 5), ("rugcheck", "RugCheck", 10))
_ANALISE_LOG_RE = re.compile(r"\(([A-Za-z0-9]{30,50})\): (?:APROVADO|REPROVADO) (\d+)/100 \[([^\]]*)\]")


def _componentes_do_log(cfg):
    """Pontos de cada criterio por token, lidos das linhas ANALISE do bot.log (para os percursos antigos).
    Devolve {mint: [(pontuacao, {criterio: pontos}), ...]}. Nunca lanca excecoes."""
    resultado = {}
    try:
        with open(cfg["ficheiro_log"], encoding="utf-8", errors="replace") as f:
            for linha in f:
                if "ANALISE " not in linha or "/100 [" not in linha:
                    continue
                m = _ANALISE_LOG_RE.search(linha)
                if not m:
                    continue
                pontos = {}
                for parte in m.group(3).split():
                    nome, _, valor = parte.rpartition("=")
                    if nome and valor.replace(".", "", 1).isdigit():
                        pontos[nome] = float(valor)
                if pontos:
                    resultado.setdefault(m.group(1), []).append((int(m.group(2)), pontos))
    except (OSError, KeyError, TypeError):
        pass
    return resultado


def _componentes(pc, do_log):
    """Pontos de cada criterio deste sinal: os gravados no percurso, ou os do bot.log com a mesma pontuacao."""
    comp = pc.get("comp")
    if isinstance(comp, dict) and comp:
        return {k: num(v) for k, v in comp.items()}
    candidatos = do_log.get(pc.get("mint")) or []
    iguais = [p for total, p in candidatos if total == int(num(pc.get("pontuacao"), -1))]
    return (iguais or [p for _, p in candidatos] or [None])[-1]


def _secao_pontuacao_por_criterio(percursos, regra, cfg):
    """Que criterio da pontuacao separa os sinais bons dos maus? So relatorio."""
    do_log = _componentes_do_log(cfg)
    com = [(pc, c) for pc in percursos for c in [_componentes(pc, do_log)] if c]
    L = ["", f"  PONTUACAO POR CRITERIO (o que distingue 70-79 de 80+?) - {len(com)} de {len(percursos)} sinais com detalhe"]
    if not com:
        return L + ["    Ainda sem dados: o detalhe vem do bot.log ou dos percursos gravados a partir de 17/11/2026."]
    baixo = [c for pc, c in com if num(pc.get("pontuacao")) < 80]
    alto = [c for pc, c in com if num(pc.get("pontuacao")) >= 80]
    media = lambda grupo, k: sum(num(c.get(k)) for c in grupo) / len(grupo) if grupo else None
    txt = lambda v: "   -  " if v is None else f"{v:5.1f}"
    L.append(f"    {'criterio':<20} {'max':>3}  {'70-79':>6} {'80+':>6}   |  com MAIS pontos neste criterio  |  com MENOS pontos")
    L.append(f"    {'':<20} {'':>3}  {f'({len(baixo)})':>6} {f'({len(alto)})':>6}   |  (a mediana conta como 'mais')  |")
    for chave, nome, maximo in CRITERIOS_PONTUACAO:
        valores = sorted(num(c.get(chave)) for _, c in com if chave in c)
        if not valores:
            continue
        mediana = valores[len(valores) // 2]
        mais = [pc for pc, c in com if chave in c and num(c.get(chave)) >= mediana]
        menos = [pc for pc, c in com if chave in c and num(c.get(chave)) < mediana]
        partes = []
        for grupo in (mais, menos):
            if grupo:
                m, a = _medir(grupo, regra, cfg)
                partes.append(f"{len(grupo):>3}: media {m:+7.2f}%  acerto {a:5.1f}%")
            else:
                partes.append(f"{'0':>3}: (todos iguais)            ")
        L.append(f"    {nome:<20} {maximo:>3}  {txt(media(baixo, chave)):>6} {txt(media(alto, chave)):>6}   |  "
                 + "  |  ".join(partes))
    L += ["    Colunas 70-79 e 80+: pontos medios desse criterio em cada grupo. Um criterio que da mais pontos aos 80+",
          "    e cujos sinais 'com mais pontos' correm pior e o candidato a estar a premiar a coisa errada.",
          "    Com poucos sinais de cada lado, as diferencas sao sobretudo sorte."]
    return L


def _secao_filtros_a_testar(percursos, regra, cfg):
    """Hipoteses de filtros aplicadas aos sinais ja gravados. Nenhuma esta ativa no bot."""
    def f(pc, chave):
        v = obter(pc, "feat", chave)
        return num(v, None) if v is not None else None

    def soma_h1(pc):
        c, v = f(pc, "compras_h1"), f(pc, "vendas_h1")
        return c + v if c is not None and v is not None else None

    def compras_h1(pc):
        total = soma_h1(pc)
        return f(pc, "compras_h1") / total * 100 if total else None

    def teste(valor, minimo=None, maximo=None):
        def aplica(pc):
            v = valor(pc)
            if v is None:
                return None
            return (minimo is None or v >= minimo) and (maximo is None or v <= maximo)
        return aplica

    def autoridades(pc):
        m = obter(pc, "obs", "mint")
        if not isinstance(m, dict) or m.get("mint_authority") is None or m.get("freeze_authority") is None:
            return None
        return m.get("mint_authority") == "revoked" and m.get("freeze_authority") == "revoked"

    externos = [
        ("liquidez >= $25.000", teste(lambda pc: num(pc.get("liq0")) or None, 25000)),
        ("idade 15 a 90 min", teste(lambda pc: f(pc, "idade_min"), 15, 90)),
        ("market cap $50k a $1,5M", teste(lambda pc: f(pc, "fdv"), 50000, 1500000)),
        ("volume 1h >= $15.000", teste(lambda pc: f(pc, "vol_h1"), 15000)),
        ("transacoes 1h >= 50", teste(soma_h1, 50)),
        ("subida 1h <= 80%", teste(lambda pc: f(pc, "h1"), None, 80)),
        ("subida 5 min <= 40%", teste(lambda pc: f(pc, "m5"), None, 40)),
    ]

    def todos_juntos(pc):
        r = [t(pc) for _, t in externos]
        return None if any(x is None for x in r) else all(r)

    testes = externos + [
        ("os 7 de cima juntos", todos_juntos),
        ("compras 1h <= 80%", teste(compras_h1, None, 80)),
        ("pontuacao 70 a 79", teste(lambda pc: num(pc.get("pontuacao"), None), 70, 79)),
        ("top10 sem a pool <= 40%", teste(_top10_sem_pool, None, 40)),
        ("mint e freeze revogadas", autoridades),
    ]
    L = ["", f"  FILTROS A TESTAR (hipoteses; NENHUM esta ativo no bot) - {len(percursos)} sinais aprovados, regras atuais"]
    for nome, aplica in testes:
        resultados = [(pc, aplica(pc)) for pc in percursos]
        ficam = [pc for pc, r in resultados if r is True]
        saem = [pc for pc, r in resultados if r is False]
        if not ficam and not saem:
            L.append(f"    {nome:<26} ainda sem dados")
            continue
        partes = []
        for rotulo, grupo in (("ficam", ficam), ("saem", saem)):
            if grupo:
                media, acerto = _medir(grupo, regra, cfg)
                partes.append(f"{rotulo} {len(grupo):>3}: media {media:+7.2f}%  acerto {acerto:5.1f}%")
            else:
                partes.append(f"{rotulo}   0" + " " * 35)
        L.append(f"    {nome:<26} " + "   |   ".join(partes))
    L += ["    Um filtro so ajuda se os que FICAM forem claramente melhores do que os que SAEM, com dezenas de",
          "    sinais de cada lado. Com poucos sinais, estas diferencas sao sobretudo sorte."]
    return L


def repetir_entrada_adiada(pc, regra, cfg, espera_seg, queda_max_pct):
    """Laboratorio: em vez de comprar no sinal, espera `espera_seg` e so entra se o preco real (ponto fino)
    nao tiver caido mais de `queda_max_pct` face ao preco do sinal. Devolve (estado, resultado em % ou None),
    com estado "entrou", "evitada" ou "sem_dados" (sem ponto fino perto do fim da espera)."""
    serie = _serie_fina(pc)
    idx = next((i for i, p in enumerate(serie) if p[2] and p[0] >= espera_seg), None)
    if idx is None or idx >= len(serie) - 1:
        return "sem_dados", None
    seg0, rel0, _ = serie[idx]
    if seg0 > espera_seg + 60 or rel0 <= 0:
        return "sem_dados", None
    if rel0 < 1 - queda_max_pct / 100.0:
        return "evitada", None
    nova = [(0, 1.0, True)] + [(s - seg0, r / rel0, fino) for s, r, fino in serie[idx + 1:]]
    return "entrou", repetir_serie(pc, nova, regra, cfg)[0]


def _secao_entrada_adiada(percursos, regra, cfg):
    """Esperar uns minutos antes de entrar ajudava? So nos percursos com pontos finos."""
    com = [pc for pc in percursos if isinstance(pc.get("ptsj"), list) and len(pc["ptsj"]) >= 2]
    L = ["", f"  ENTRADA ADIADA (hipotese; o bot NAO faz isto) - {len(com)} percursos com pontos finos, regras atuais"]
    if not com:
        return L + ["    Ainda sem dados: precisa de percursos com pontos finos."]
    imediato = {id(pc): repetir_serie(pc, _serie_fina(pc), regra, cfg)[0] for pc in com}
    media = lambda valores: sum(valores) / len(valores)
    L.append(f"    entrar logo no sinal (referencia)      {len(com):>3} entradas   media {media(list(imediato.values())):+7.2f}%")
    for espera, queda in ((60, 5), (120, 5), (180, 5), (300, 5), (180, 0), (180, 100)):
        r = [(pc, *repetir_entrada_adiada(pc, regra, cfg, espera, queda)) for pc in com]
        entrou = [x[2] for x in r if x[1] == "entrou"]
        evitadas = [imediato[id(x[0])] for x in r if x[1] == "evitada"]
        sem = sum(1 for x in r if x[1] == "sem_dados")
        condicao = ("sem condicao" if queda >= 100 else "se nao caiu" if queda == 0 else f"se caiu menos de {queda}%")
        linha = f"    esperar {espera // 60} min, {condicao:<21} {len(entrou):>3} entradas"
        linha += f"   media {media(entrou):+7.2f}%" if entrou else "   media     -   "
        linha += f"   | evitadas {len(evitadas)}" + (f" (dariam {media(evitadas):+.2f}% entrando logo)" if evitadas else "")
        L.append(linha + (f"   | sem dados {sem}" if sem else ""))
    L += ["    'evitadas' = sinais em que nao se entrava; entre parenteses, o que deram a quem entrou logo.",
          "    O preco de entrada adiada e o de VENDA no Jupiter (um pouco abaixo do de compra): resultado algo otimista.",
          "    Adiar so ajuda se as entradas ficarem melhores E as evitadas forem as mas. Com poucos percursos e sorte."]
    return L


def _secao_carteiras(todos, regra, cfg):
    """Carteiras seguidas (wallet_watchlist.json) e donos que se repetem nos sinais. So observacao."""
    L = ["", "  CARTEIRAS (so observacao: nada disto decide compras)"]
    com = [pc for pc in todos if obter(pc, "obs", "carteiras", "estado") in ("ok", "parcial")]
    if com:
        dentro = lambda pc: int(num(obter(pc, "obs", "carteiras", "dentro")))
        L.append(f"    Carteiras seguidas: {len(com)} percursos com a lista consultada (aprovados + controlo)")
        for nome, escolhe in (("nenhuma carteira seguida dentro", lambda pc: dentro(pc) == 0),
                              ("1 carteira seguida dentro", lambda pc: dentro(pc) == 1),
                              ("2 ou mais dentro", lambda pc: dentro(pc) >= 2)):
            grupo = [pc for pc in com if escolhe(pc)]
            if grupo:
                L.append(_linha_grupo(nome, grupo, regra, cfg))
    else:
        L.append("    Carteiras seguidas: ainda sem dados (wallet_watchlist.json vazio, ou sem sinais desde que existe).")
    vistos, com_donos = {}, 0
    for pc in todos:
        top = obter(pc, "obs", "holders", "top")
        if not isinstance(top, list):
            continue
        com_donos += 1
        subiu = max(num(p[1]) for p in pc["pts"]) >= 1.5
        donos = {x[0] for x in top if isinstance(x, list) and len(x) == 2 and isinstance(x[0], str)
                 and x[0] and x[0] != pc.get("par")}
        for dono in donos:
            registo = vistos.setdefault(dono, [0, 0, 0])
            registo[0] += 1
            registo[1] += 1 if subiu else 0
            registo[2] += 1 if pc.get("fim") != "tempo" else 0
    repetidos = sorted(((d, r) for d, r in vistos.items() if r[0] >= 3),
                       key=lambda x: (-x[1][1] / x[1][0], -x[1][0], x[0]))
    L.append(f"    Donos que se repetem entre as maiores contas ({com_donos} percursos com dados; minimo 3 aparicoes):")
    if not repetidos:
        L.append("      Ainda nenhum: precisa de mais sinais gravados (os donos sao guardados desde 15/11/2026).")
    for dono, (n, subiram, rugs) in repetidos[:12]:
        L.append(f"      {dono:<44} {n:>3} sinais   {subiram:>2} chegaram a +50%   {rugs:>2} acabaram em rug")
    L += ["    Um dono repetido pode ser uma boa carteira, mas tambem um bot, uma bolsa, a propria DEX ou o criador",
          "    dos tokens. Com poucas aparicoes e sobretudo sorte: serve para escolher candidatas, nao para copiar."]
    return L


def _secao_observacao_extra(percursos, regra, cfg):
    """Laboratorio: cruza a observacao extra (so leitura) com o resultado real dos percursos."""
    com = [pc for pc in percursos if isinstance(pc.get("obs"), dict)]
    L = ["", f"  OBSERVACAO EXTRA (holders, mint e Jupiter) - {len(com)} de {len(percursos)} percursos com dados"]
    if not com:
        return L + ["    Ainda sem dados: so os percursos gravados depois desta versao os tem."]

    def grupo(nome, sel):
        g = [pc for pc in com if sel(pc)]
        if not g:
            return
        media, acerto = _medir(g, regra, cfg)
        maxi = sum(max(p[1] for p in pc["pts"]) for pc in g) / len(g)
        L.append(f"    {nome:<34} {len(g):>4}   media {media:+6.2f}%   acerto {acerto:5.1f}%   pico medio {100 * (maxi - 1):+6.1f}%")

    top10 = lambda pc: num(obter(pc, "obs", "holders", "top10_pct"), -1)
    grupo("top10 holders <= 40%", lambda pc: 0 <= top10(pc) <= 40)
    grupo("top10 holders 40-70%", lambda pc: 40 < top10(pc) <= 70)
    grupo("top10 holders > 70%", lambda pc: top10(pc) > 70)

    def sem_pool(pc):  # top 10 sem a conta da pool (que costuma ser o maior "holder"); None se nao der para saber
        top, pool = obter(pc, "obs", "holders", "top"), pc.get("par")
        validos = [x for x in top if isinstance(x, list) and len(x) == 2] if isinstance(top, list) else []
        if not pool or not any(x[0] == pool for x in validos):
            return None
        return sum(num(x[1]) for x in [x for x in validos if x[0] != pool][:10])

    com_donos = [pc for pc in com if isinstance(obter(pc, "obs", "holders", "top"), list)]
    if com_donos:
        L.append(f"    Donos das maiores contas: {len(com_donos)} percursos com dados; pool do par identificada em "
                 f"{sum(1 for pc in com_donos if sem_pool(pc) is not None)}")
        grupo("top10 SEM a pool <= 25%", lambda pc: sem_pool(pc) is not None and sem_pool(pc) <= 25)
        grupo("top10 SEM a pool 25-50%", lambda pc: sem_pool(pc) is not None and 25 < sem_pool(pc) <= 50)
        grupo("top10 SEM a pool > 50%", lambda pc: sem_pool(pc) is not None and sem_pool(pc) > 50)
    estado_auth = lambda pc, k: obter(pc, "obs", "mint", k)
    grupo("mint ou freeze authority ATIVA", lambda pc: "active" in (estado_auth(pc, "mint_authority"), estado_auth(pc, "freeze_authority")))
    grupo("mint e freeze authority revogadas", lambda pc: estado_auth(pc, "mint_authority") == "revoked" and estado_auth(pc, "freeze_authority") == "revoked")
    reais = [(num(obter(pc, "obs", "jupiter", "slippage_real_pct"), None), num(obter(pc, "obs", "slippage_simulado_pct"), None)) for pc in com]
    reais = [(r, s) for r, s in reais if r is not None and s is not None]
    if reais:
        L.append(f"    Slippage de entrada: simulado medio {sum(s for _, s in reais) / len(reais):.2f}%   "
                 f"real Jupiter medio {sum(r for r, _ in reais) / len(reais):.2f}%   ({len(reais)} cotacoes)")
    L.append("    (So observacao: estes dados ainda nao filtram compras.)")
    return L


_PROPRIOS = {k for k in list(globals()) if not k.startswith("__")}
