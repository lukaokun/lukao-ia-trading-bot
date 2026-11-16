#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TESTE OFFLINE - verifica a logica do bot com um mercado falso (sem internet).
Correr:  python teste_offline.py
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import time
import copy
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as B  # noqa: E402
import paper_trading_limits as PTL  # noqa: E402

# ---------------------------------------------------------------- mercado falso
RELOGIO = {"t": 1_800_000_000.0}
MERCADO = {"perfis": [], "boosts": [], "pares": {}, "rug": {}, "modo": "ok", "pedidos": 0}
_BOTS_COM_SNAPSHOTS = []


class _AutoApprovalFixture:
    """Test-only approval source for legacy offline scenarios that expect Paper fills."""
    def __init__(self):
        self.pending = []
        self.used = set()
        self.token_proposals = {}
        self.counter = 0

    def start(self):
        pass

    def propose(self, mint, pair, analysis, amount):
        self.counter += 1
        proposal = {
            "proposal_id": f"fixture-{self.counter}", "confirmation_code": "TEST",
            "mint": mint, "symbol": B.simbolo_limpo(pair),
            "pair_address": str(pair.get("pairAddress") or ""),
            "price_usd": str(pair.get("priceUsd") or ""), "amount_usd": float(amount),
            "score": analysis.get("pontuacao"), "created_at": B.agora(),
            "expires_monotonic": time.monotonic() + 90, "pair": copy.deepcopy(pair),
            "analysis": copy.deepcopy(analysis), "status": "approved",
        }
        token = "fixture-token-" + str(self.counter)
        self.token_proposals[token] = proposal["proposal_id"]
        self.pending.append((proposal, token, True, "fixture approval"))
        return proposal

    def drain_results(self):
        result, self.pending = self.pending, []
        return result

    def consume(self, token, mint, pair, amount=None):
        if token in self.used or not token or not token.startswith("fixture-token-"):
            return False
        self.used.add(token)
        return True

    def proposal_id_for_token(self, token):
        return self.token_proposals.get(token)

    def discard(self, token):
        self.token_proposals.pop(token, None)
        return True

    def public_pending(self):
        return []

    def close(self):
        self.pending.clear()


class Resposta:
    def __init__(self, codigo, dados=None, cabecalhos=None):
        self.status_code = codigo
        self._dados = dados
        self.headers = cabecalhos or {}

    def json(self):
        if self._dados is ValueError:
            raise ValueError("nao e json")
        return self._dados


def get_falso(url, timeout=None, headers=None):
    MERCADO["pedidos"] += 1
    modo = MERCADO["modo"]
    if modo == "em_baixo":
        return Resposta(500)
    if modo == "excecao":
        raise B.requests.ConnectionError("sem rede")
    if modo == "lixo":
        return Resposta(200, ValueError)
    if modo == "429":
        return Resposta(429, cabecalhos={"Retry-After": "1"})
    if url == B.URL_PERFIS:
        return Resposta(200, MERCADO["perfis"])
    if url == B.URL_BOOSTS:
        return Resposta(200, MERCADO["boosts"])
    if MERCADO.get("falha_mint") and MERCADO["falha_mint"] in url:
        return Resposta(500)
    if "/tokens/v1/solana/" in url:
        if modo == "legacy":
            return Resposta(404)
        mints = url.rsplit("/", 1)[1].split(",")
        assert len(mints) <= 30, "mais de 30 enderecos num pedido"
        return Resposta(200, [p for m in mints for p in MERCADO["pares"].get(m, [])])
    if "/latest/dex/tokens/" in url:
        mints = url.rsplit("/", 1)[1].split(",")
        return Resposta(200, {"pairs": [p for m in mints for p in MERCADO["pares"].get(m, [])]})
    if "rugcheck" in url:
        mint = url.split("/tokens/")[1].split("/")[0]
        if mint in MERCADO["rug"]:
            return Resposta(200, MERCADO["rug"][mint])
        # Baseline fake response is a valid low-risk report. Failure paths are
        # exercised separately and must never be mistaken for this result.
        return Resposta(200, {"score_normalised": 10, "risks": []})
    return Resposta(404)


def mint_de(nome):
    return (nome + "x" * 44)[:44]


def par(nome, preco=0.001, liq=60000, mcap=400000, idade_min=60, vol_m5=4000, vol_h1=90000,
        m5=(30, 12), h1=(300, 150), var_m5=5, var_h1=30):
    return {
        "chainId": "solana", "pairAddress": "PAR" + nome, "url": "https://dexscreener.com/solana/" + nome,
        "baseToken": {"address": mint_de(nome), "symbol": nome, "name": "Token " + nome},
        "priceUsd": str(preco), "liquidity": {"usd": liq}, "marketCap": mcap, "fdv": mcap,
        "pairCreatedAt": int((RELOGIO["t"] - idade_min * 60) * 1000),
        "volume": {"m5": vol_m5, "h1": vol_h1},
        "txns": {"m5": {"buys": m5[0], "sells": m5[1]}, "h1": {"buys": h1[0], "sells": h1[1]}},
        "priceChange": {"m5": var_m5, "h1": var_h1},
    }


def listar(nome, **kw):
    """Poe um token no mercado falso (perfil + par)."""
    MERCADO["perfis"].append({"chainId": "solana", "tokenAddress": mint_de(nome)})
    MERCADO["pares"][mint_de(nome)] = [par(nome, **kw)]


def mexer(nome, preco=None, liq=None):
    p = MERCADO["pares"][mint_de(nome)][0]
    if preco is not None:
        p["priceUsd"] = str(preco)
    if liq is not None:
        p["liquidity"]["usd"] = liq


def fechar_log():
    """Fecha os ficheiros associados ao logger antes de os apagar ou trocar."""
    for handler in list(B.log.handlers):
        B.log.removeHandler(handler)
        handler.close()


def novo_bot(pasta, limpar=True):
    # The async market-snapshot writer must release its SQLite handle before
    # the shared temporary test directory is reset or the bot is recreated.
    for existente in _BOTS_COM_SNAPSHOTS:
        existente.close()
    _BOTS_COM_SNAPSHOTS.clear()
    fechar_log()
    if limpar:
        MERCADO.update(perfis=[], boosts=[], pares={}, rug={}, modo="ok", falha_mint=None)
        for f in os.listdir(pasta):
            alvo = os.path.join(pasta, f)
            if os.path.isdir(alvo):
                shutil.rmtree(alvo)
            else:
                os.remove(alvo)
    cfg = dict(B.CONFIG)
    cfg.update(ficheiro_estado=os.path.join(pasta, "estado.json"),
               ficheiro_trades=os.path.join(pasta, "trades.csv"),
               ficheiro_log=os.path.join(pasta, "bot.log"), limpar_ecra=False,
               painel_web=False, porta_web=0, ficheiro_percursos=os.path.join(pasta, "percursos.jsonl"),
               # os testes usam regras fixas, independentes dos valores por defeito do bot
               stop_loss_pct=-6.0, take_profit_pct=25.0, trailing_ativar_pct=10.0, trailing_distancia_pct=8.0,
               tempo_maximo_min=60, percurso_minutos=90)
    # Test-only approved ceiling. The project operational config remains unset and fail-closed.
    PTL.APPROVED_MAX_SLIPPAGE_PCT = Decimal("95")
    policy_path = os.path.join(pasta, "config_operacoes.json")
    with open(policy_path, "w", encoding="utf-8") as policy_file:
        json.dump({
            "mode": "paper", "live_trading_enabled": False,
            "private_keys_in_application": False, "manual_confirmation_required": True,
            "max_eur_per_operation": 5, "max_open_positions": 1,
            "reject_duplicate_orders": True, "require_cost_estimate_before_approval": True,
            "require_slippage_estimate_before_approval": True,
            "block_if_costs_or_minimum_order_cannot_be_verified": True,
            "emergency_stop_enabled": True, "max_slippage_pct": "95",
        }, policy_file)
    B.aplicar_config_operacional(cfg, policy_path)
    B.configurar_log(cfg)
    bot = B.Bot(cfg)
    # Isolated Paper fixtures use a fixed, valid ECB observation so the operational
    # ceiling is exercised without a network request.
    sessao = bot.estado["operational_session"]
    sessao.update({
        "fx_usd_per_eur": "1.1052",
        "fx_reference_date": datetime.now(timezone.utc).date().isoformat(),
        "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(),
        "fx_source": B.ECB_SOURCE,
    })
    B.guardar_estado(cfg, bot.estado, strict=True)
    bot._fx_rate_persisted = True
    bot.manual_approvals.close()
    bot.manual_approvals = _AutoApprovalFixture()
    _BOTS_COM_SNAPSHOTS.append(bot)
    return bot, cfg


def trades(cfg):
    with open(cfg["ficheiro_trades"], encoding="utf-8") as f:
        return list(csv.DictReader(f))


def avancar(minutos):
    RELOGIO["t"] += minutos * 60


# ---------------------------------------------------------------------- testes
def t_fluxo_completo(pasta):
    bot, cfg = novo_bot(pasta)
    listar("BOM")
    listar("LIXO", liq=2000)                       # liquidez baixa
    listar("PUMP", var_h1=300)                     # ja pumpou
    listar("VELHO", idade_min=60 * 30)             # demasiado antigo
    listar("VENDE", m5=(3, 40), h1=(60, 400), vol_m5=300, vol_h1=13000)  # so vendas -> pontuacao baixa
    listar("PERIGO")
    MERCADO["rug"][mint_de("PERIGO")] = {"score_normalised": 20,
                                         "risks": [{"name": "Mint authority enabled", "level": "danger"}]}
    MERCADO["perfis"].append({"chainId": "ethereum", "tokenAddress": "0x" + "a" * 40})  # outra rede
    MERCADO["perfis"].append("lixo")                                                     # dados estragados
    assert bot.ciclo_scanner() == 1
    assert list(bot.estado["posicoes"]) == [mint_de("BOM")], bot.estado["posicoes"].keys()
    analisadas = {r["simbolo"]: r for r in bot.recentes}
    assert analisadas["BOM"]["estado_analise"] == "aprovada"
    assert analisadas["BOM"]["nome"] == "Token BOM" and analisadas["BOM"]["preco"] == 0.001
    assert analisadas["LIXO"]["estado_analise"] == "rejeitada"
    assert analisadas["LIXO"]["resultado_analise"]
    pos = bot.estado["posicoes"][mint_de("BOM")]
    assert abs(pos["custo"] - 5.5260) < 1e-9 and abs(bot.estado["saldo"] - 94.4740) < 1e-9
    assert pos["preco_efetivo"] > pos["preco_entrada"]            # pagou slippage
    assert pos["tokens"] * pos["preco_entrada"] < 5.5260          # pagou taxa
    assert bot.valor_total() < 100.0                              # custos ja contam no valor total
    assert len(trades(cfg)) == 1 and trades(cfg)[0]["tipo"] == "COMPRA"
    assert bot.ciclo_scanner() == 0                               # nao volta a comprar o mesmo
    assert len(bot.estado["posicoes"]) == 1
    bot.atualizar_snapshot()
    snap_pos = json.loads(bot.snapshot)["posicoes"][0]                # grafico "Posicao ao vivo"
    assert snap_pos["regras_saida"]["stop_loss_pct"] == cfg["stop_loss_pct"] and snap_pos["intervalo_seg"] == cfg["intervalo_risco_com_posicao_seg"] and snap_pos["fonte_preco"] == "dexscreener"
    assert snap_pos["par"] and snap_pos["pico"] > 0 and abs(snap_pos["pnl_usd"] - (snap_pos["valor_atual_usd"] - pos["custo"])) < 1e-9


def comprar_um(pasta, nome="BOM", **kw):
    bot, cfg = novo_bot(pasta)
    listar(nome, **kw)
    assert bot.ciclo_scanner() == 1
    return bot, cfg


def t_stop_loss(pasta):
    bot, cfg = comprar_um(pasta)
    mexer("BOM", preco=0.00097)                                   # -3%: mantem
    assert bot.ciclo_risco() == 0
    mexer("BOM", preco=0.00093)                                   # -7%: vende
    assert bot.ciclo_risco() == 1 and not bot.estado["posicoes"]
    v = trades(cfg)[-1]
    assert v["motivo"].startswith("STOP_LOSS") and float(v["pnl_usd"]) < 0
    assert -13 < float(v["pnl_pct"]) < -9, v["pnl_pct"]           # -7% + custos de entrada e saida
    assert bot.estado["stats"]["perdas"] == 1
    assert abs(bot.estado["saldo"] - bot.valor_total()) < 1e-9


def t_take_profit(pasta):
    bot, cfg = comprar_um(pasta)
    mexer("BOM", preco=0.0013)                                    # +30%
    assert bot.ciclo_risco() == 1
    v = trades(cfg)[-1]
    assert v["motivo"].startswith("TAKE_PROFIT") and float(v["pnl_usd"]) > 0
    assert 20 < float(v["pnl_pct"]) < 30, v["pnl_pct"]            # +30% menos custos
    assert bot.estado["stats"]["ganhos"] == 1 and bot.estado["saldo"] > 100


def t_trailing(pasta):
    bot, cfg = comprar_um(pasta)
    mexer("BOM", preco=0.00112)                                   # +12%: liga o trailing
    assert bot.ciclo_risco() == 0
    assert bot.estado["posicoes"][mint_de("BOM")]["trailing"] is True
    mexer("BOM", preco=0.00108)                                   # -3.6% do pico: mantem
    assert bot.ciclo_risco() == 0
    mexer("BOM", preco=0.00102)                                   # -8.9% do pico: vende
    assert bot.ciclo_risco() == 1
    assert trades(cfg)[-1]["motivo"].startswith("TRAILING")


def t_rug_liquidez(pasta):
    bot, cfg = comprar_um(pasta)
    mexer("BOM", preco=0.0001, liq=800)                           # puxaram a liquidez
    assert bot.ciclo_risco() == 1
    v = trades(cfg)[-1]
    assert v["motivo"].startswith("RUG") and float(v["pnl_pct"]) < -90, v
    assert bot.estado["saldo"] >= 94.4740


def t_rug_par_desaparece(pasta):
    bot, cfg = comprar_um(pasta)
    MERCADO["pares"][mint_de("BOM")] = []
    for _ in range(cfg["falhas_sem_dados_max"] - 1):
        assert bot.ciclo_risco() == 0
    assert bot.ciclo_risco() == 1
    v = trades(cfg)[-1]
    assert "perda total" in v["motivo"] and float(v["pnl_pct"]) == -100.0
    assert abs(bot.estado["saldo"] - 94.4740) < 1e-9


def t_tempo_maximo(pasta):
    bot, cfg = comprar_um(pasta)
    avancar(30)
    assert bot.ciclo_risco() == 0
    avancar(31)
    assert bot.ciclo_risco() == 1 and trades(cfg)[-1]["motivo"].startswith("TEMPO")


def t_limites(pasta):
    bot, cfg = novo_bot(pasta)
    for i in range(7):
        listar(f"T{i}")
    assert bot.ciclo_scanner() == 1                               # maximo operacional de 1 posicao
    investido = sum(p["custo"] for p in bot.estado["posicoes"].values())
    assert investido <= 5.5260 + 1e-9 and bot.estado["saldo"] >= 94.4740 - 1e-9
    # Uma posição e o limite EUR continuam a prevalecer sobre a percentagem da estratégia.
    bot, cfg = novo_bot(pasta)
    bot.cfg["tamanho_posicao_pct"] = 20
    for i in range(7):
        listar(f"U{i}")
    bot.ciclo_scanner()
    investido = sum(p["custo"] for p in bot.estado["posicoes"].values())
    assert abs(investido - 5.5260) < 1e-6, investido


def t_persistencia(pasta):
    bot, cfg = comprar_um(pasta)
    bot.guardar()
    bot2, _ = novo_bot(pasta, limpar=False)
    assert mint_de("BOM") in bot2.estado["posicoes"] and abs(bot2.estado["saldo"] - 94.4740) < 1e-9
    assert mint_de("BOM") in bot2.estado["vistos"]
    mexer("BOM", preco=0.0013)
    assert bot2.ciclo_risco() == 1                                # continua a gerir depois de reiniciar
    bot2.guardar()
    with open(cfg["ficheiro_estado"], "r", encoding="utf-8") as f:
        estado_valido = f.read()
    # Estado corrompido e preservado e impede um arranque que perderia dados.
    bot2.close()
    with open(cfg["ficheiro_estado"], "w") as f:
        f.write("{isto nao e json")
    corrupto = "{isto nao e json"
    try:
        B.Bot(cfg)
        raise AssertionError("estado corrompido devia bloquear a inicializacao")
    except B.StateIntegrityError:
        pass
    with open(cfg["ficheiro_estado"], "r", encoding="utf-8") as f:
        assert f.read() == corrupto
    with open(cfg["ficheiro_estado"], "w", encoding="utf-8") as f:
        f.write(estado_valido)
    bot3, _ = novo_bot(pasta, limpar=False)
    # --reset
    bot3.guardar()
    with open(cfg["ficheiro_estado"], "rb") as f:
        estado_arquivo = f.read()
    with open(cfg["ficheiro_trades"], "rb") as f:
        trades_arquivo = f.read()
    B.reiniciar(cfg, confirmado=True)
    assert os.path.exists(cfg["ficheiro_estado"]) and os.path.exists(cfg["ficheiro_trades"])
    with open(cfg["ficheiro_estado"], "r", encoding="utf-8") as f:
        estado_novo = json.load(f)
    assert estado_novo["saldo"] == cfg["saldo_inicial_usd"] and not estado_novo["posicoes"]
    assert trades(cfg) == []
    archives = os.path.join(os.path.dirname(cfg["ficheiro_estado"]), "reset_archives")
    pastas_arquivo = os.listdir(archives)
    assert len(pastas_arquivo) == 1
    arquivo = os.path.join(archives, pastas_arquivo[0])
    with open(os.path.join(arquivo, "estado.json"), "rb") as f:
        assert f.read() == estado_arquivo
    with open(os.path.join(arquivo, "trades.csv"), "rb") as f:
        assert f.read() == trades_arquivo


def t_api_com_problemas(pasta):
    bot, cfg = comprar_um(pasta)
    for modo in ("em_baixo", "excecao", "lixo", "429"):
        MERCADO["modo"] = modo
        assert bot.ciclo_scanner() == 0 and bot.ciclo_risco() == 0, modo
        assert len(bot.estado["posicoes"]) == 1, modo             # nao vende por falta de dados da API
    MERCADO["modo"] = "ok"
    # dados malformados
    for i, estragado in enumerate([
        {"chainId": "solana", "baseToken": {"address": mint_de("M0")}},
        {"chainId": "solana", "baseToken": {"address": mint_de("M1")}, "priceUsd": "abc",
         "liquidity": None, "txns": "x", "volume": [], "priceChange": {"m5": "nan"}},
        {"chainId": "solana", "baseToken": {"address": mint_de("M2")}, "priceUsd": "0.01",
         "liquidity": {"usd": "muito"}, "pairCreatedAt": "ontem", "marketCap": None},
    ]):
        MERCADO["perfis"].append({"chainId": "solana", "tokenAddress": mint_de(f"M{i}")})
        MERCADO["pares"][mint_de(f"M{i}")] = [estragado, None, "texto"]
    assert bot.ciclo_scanner() == 0 and len(bot.estado["posicoes"]) == 1
    bot.painel()
    assert "RESUMO" in bot.resumo()


def t_formato_antigo(pasta):
    bot, cfg = novo_bot(pasta)
    listar("BOM")
    MERCADO["modo"] = "legacy"
    assert bot.ciclo_scanner() == 1
    mexer("BOM", preco=0.0009)
    assert bot.ciclo_risco() == 1


def t_reanalise(pasta):
    bot, cfg = novo_bot(pasta)
    listar("NOVO", idade_min=2)                                   # demasiado novo: fica em espera
    assert bot.ciclo_scanner() == 0 and mint_de("NOVO") in bot.estado["pendentes"]
    avancar(6)
    assert bot.ciclo_scanner() == 1                               # ja tem idade: compra
    assert mint_de("NOVO") not in bot.estado["pendentes"]
    listar("NUNCA", liq=10000)                                    # quase bom: espera, depois desiste
    bot.ciclo_scanner()
    assert mint_de("NUNCA") in bot.estado["pendentes"]
    avancar(25)
    bot.ciclo_scanner()
    assert mint_de("NUNCA") not in bot.estado["pendentes"] and mint_de("NUNCA") in bot.estado["vistos"]


def t_ctrl_c(pasta):
    bot, cfg = novo_bot(pasta)
    B.aplicar_config_operacional(cfg)
    bot.estado["operational_session"].update({
        "fx_usd_per_eur": "1.15",
        "fx_reference_date": datetime.now(timezone.utc).date().isoformat(),
        "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(),
        "fx_source": "European Central Bank (ECB) daily reference rate",
    })
    B.guardar_estado(cfg, bot.estado, strict=True)
    bot._fx_rate_persisted = True
    listar("BOM")
    chamadas = {"n": 0}

    def dormir_que_para(s):
        RELOGIO["t"] += max(s, 0)
        chamadas["n"] += 1
        if chamadas["n"] > 40:
            raise KeyboardInterrupt

    antigo = B.dormir
    B.dormir = dormir_que_para
    try:
        bot.correr()                                              # tem de sair sem erro
    finally:
        B.dormir = antigo
    assert os.path.exists(cfg["ficheiro_estado"]) and len(bot.estado["posicoes"]) == 1


def t_plataforma_web(pasta):
    import json
    import urllib.request
    bot, cfg = novo_bot(pasta)
    listar("BOM")
    listar("<img src=x onerror=alert(1)>")                       # simbolo malicioso: tem de ir como texto
    for i in range(5):
        listar(f"S{i}")
    bot.ciclo_scanner()
    sinais = bot.estado["sinais"]
    assert len(sinais) == 7 and sum(s["estado"] == "comprado" for s in sinais) == 1
    assert sum(s["estado"] == "sem_vaga" for s in sinais) == 6
    mexer("BOM", preco=0.0013)
    bot.ciclo_risco()
    s = next(x for x in sinais if x["simbolo"] == "BOM")
    assert s["estado"] == "fechado" and s["resultado"] > 20 and s["saida"].startswith("TAKE_PROFIT")
    bot.registar_historico()
    total_publicado = bot.atualizar_snapshot()
    cfg["painel_web"] = True
    url = B.iniciar_painel_web(bot)
    try:
        porta = bot.servidor_web.server_address[1]
        base = f"http://127.0.0.1:{porta}"
        assert url.startswith(f"http://localhost:{porta}")
        neural = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        # Dashboard Neural: so leitura, so GET /api/estado, sem codigos de confirmacao nem acoes.
        assert neural.startswith("<!DOCTYPE html>") and "Neural Sphere" in neural and "fetch('/api/estado',{cache:'no-store'})" in neural
        # Unica escrita autorizada (2026-11-14): POST /api/proposta/decisao, com confirmacao e chave da sessao.
        assert neural.count("fetch(") == 2 and neural.count("method:'POST'") == 1 and "'/api/proposta/decisao'" in neural
        assert "confirmation_code" not in neural and "__MEMEBOT_TOKEN__" not in neural
        assert "XMLHttpRequest" not in neural and "WebSocket" not in neural and "sendBeacon" not in neural
        assert "function verificarAvisos()" in neural and "Proposta de compra pendente" in neural and "Sim, recusar" in neural and "Sim, aprovar" in neural
        assert 'href="/classico"' in neural and "@media(max-width:699px)" in neural
        pagina = urllib.request.urlopen(base + "/classico", timeout=5).read().decode("utf-8")
        original_neural = B.CAMINHO_PAGINA_NEURAL
        try:
            B.CAMINHO_PAGINA_NEURAL = original_neural + ".inexistente"
            assert urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8") == pagina
        finally:
            B.CAMINHO_PAGINA_NEURAL = original_neural
        assert "<!DOCTYPE html>" in pagina and "/api/estado" in pagina and "viewport" in pagina
        assert "typeof d.hora==='string'" in pagina
        assert "function conta(n,ate,fmt)" in pagina and "(t-t0)/700" in pagina
        assert "typeof ate!=='number'&&typeof ate!=='string'" in pagina
        assert "!disponivel(ate)" in pagina and "if(n._v===alvo)" in pagina
        assert "function moedasRadar()" in pagina and "function estadoRadar(o)" in pagina
        assert "function atualizarCartoesRadar()" in pagina and "radarHoverMint||radarCartaoAbertoMint" in pagina
        assert ".blip.cartao-visivel .blip-cartao" in pagina
        assert ".blip.selecionado .blip-cartao" not in pagina and ".blip:focus-visible .blip-cartao" not in pagina
        assert "radarCartaoAbertoMint=null" in pagina and "atualizarCartoesRadar()}" in pagina
        assert "matchMedia('(hover: none), (pointer: coarse)').matches" in pagina
        assert "if(toque&&mesma&&cartaoAberto){radarCartaoAbertoMint=null;atualizarCartoesRadar()}" in pagina
        assert "function melhorSinalRadar()" in pagina and "function atualizarCartaoRadar(n,o,melhor)" in pagina
        assert ".blip-orbita" in pagina and "@keyframes orbita-moeda" in pagina and "@keyframes contra-orbita-moeda" in pagina
        assert "function valorCartaoRadar(v,fmt)" in pagina and "'\\u2014'" in pagina
        assert "campo('blip-card-preco'" in pagina and "campo('blip-card-variacao'" in pagina and "campo('blip-card-score'" in pagina
        assert "c.preco.textContent=disponivel(o.preco)&&Number.isFinite(Number(o.preco))&&Number(o.preco)>=0" in pagina
        assert "c.variacao.textContent=valorCartaoRadar(o.var_h1,v=>pct(v))" in pagina
        assert "c.score.textContent=disponivel(o.pontuacao)?Math.round(Number(o.pontuacao))+'/100':'\\u2014'" in pagina
        # Etapa 1 do dashboard: tooltip com liquidez, market cap, idade e motivo, so com dados ja publicados.
        assert "campo('blip-card-liq'" in pagina and "campo('blip-card-mcap'" in pagina and "campo('blip-card-idade'" in pagina
        assert "function criterioRadar(o,id)" in pagina and "x.status==='passed'||x.status==='rejected'" in pagina
        assert "criterioRadar(o,'market_cap')" in pagina and "criterioRadar(o,'idade_par')" in pagina
        assert "c.motivo.textContent=txt" in pagina and "c.motivo.innerHTML" not in pagina
        assert ".blip-card-mcap,.blip-card-idade,.blip-card-motivo{display:none}" in pagina
        assert "atualizarCartaoRadar(n,o,isMelhor)" in pagina and "n.orbita.classList.toggle('destaque',melhor)" in pagina
        assert "if(!ids.has(m)){n.orbita.remove();blips.delete(m);if(radarHoverMint===m)radarHoverMint=null;if(radarCartaoAbertoMint===m)radarCartaoAbertoMint=null}" in pagina
        assert "n.addEventListener('mouseenter'" in pagina and "n.addEventListener('mouseleave'" in pagina
        assert "@media(prefers-reduced-motion:reduce)" in pagina and ".blip-card-variacao,.blip-card-score{display:none}" in pagina
        assert "radar-info" in pagina and "addEventListener('click'" in pagina
        assert "Ainda não há moedas analisadas." in pagina and "slice(-40)" in pagina
        assert ".blip.aprovada" in pagina and ".blip.rejeitada" in pagina and ".blip.analise" in pagina
        assert ".aviso{position:relative" in pagina and ".zona{position:relative;display:grid" in pagina
        assert "@media(max-width:699px)" in pagina and "@media(min-width:1100px)" in pagina
        assert ".radar-legenda{display:flex" in pagina and "width:min(100%,440px)" in pagina
        assert "conta($('total'),d.total,usd)" in pagina
        assert "conta($('saldo'),d.saldo,usd)" in pagina
        assert "conta($('real'),st.realizado,sinalado)" in pagina
        assert 'id="cmode"' in pagina and 'id="estado-bot"' in pagina and 'id="ultima-atualizacao"' in pagina
        assert "Paper Trading · simulação" in pagina and "Demo · mercado inventado" in pagina
        d = json.loads(urllib.request.urlopen(base + "/api/estado", timeout=5).read().decode("utf-8"))
        assert d["modo"] == "simulacao" and len(d["sinais"]) == 7 and len(d["posicoes"]) == 0
        assert d["em_analise"] == [] and len(d["recentes"]) == 6
        assert all("estado_analise" in o and "preco" in o and "nome" in o for o in d["recentes"])
        assert isinstance(d["hora"], str) and d["hora"]
        assert d["stats"]["fechados"] == 1 and len(d["agentes"]) == 4 and len(d["historico"]) >= 2
        assert abs(d["total"] - bot.valor_total()) < 1e-9
        assert abs(d["total"] - total_publicado) < 1e-9
        assert abs(d["saldo"] - bot.estado["saldo"]) < 1e-9
        assert abs(d["pnl"] - (bot.valor_total() - bot.estado["saldo_inicial"])) < 1e-9
        assert d["sinais_24h"] == len(bot.estado["sinais_ts"])
        assert d["agora"] > 0 and d["limite_atualizacao_seg"] >= 30
        try:
            urllib.request.urlopen(base + "/estado.json", timeout=5)
            raise AssertionError("devia dar 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        bot.servidor_web.shutdown()
        bot.servidor_web.server_close()

    # O terminal usa o valor publicado pela API, mesmo que os preços mudem depois do snapshot.
    import contextlib, io
    pos = next(iter(bot.estado["posicoes"].values()), None)
    if pos is not None:
        valor_original = pos["preco_atual"]
        pos["preco_atual"] *= 1.2
    else:
        valor_original = bot.estado["saldo"]
        bot.estado["saldo"] += 1.0
    try:
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            bot.painel(total_publicado)
        assert abs(bot.valor_total() - total_publicado) > 1e-6
        assert f"Valor total: ${total_publicado:.2f}" in saida.getvalue()
    finally:
        if pos is not None:
            pos["preco_atual"] = valor_original
        else:
            bot.estado["saldo"] = valor_original
    # estado antigo (sem sinais/historico) continua a carregar
    bot.estado.pop("sinais"), bot.estado.pop("historico")
    bot.guardar()
    bot2, _ = novo_bot(pasta, limpar=False)
    assert bot2.estado["sinais"] == [] and len(bot2.estado["posicoes"]) == 0


def t_scanner_publica_analise_em_curso(pasta):
    import json
    import threading
    bot, _ = novo_bot(pasta)
    listar("ATIVA")
    entrou, continuar = threading.Event(), threading.Event()
    analisar = bot.analista.analisar

    def pausar(mint, par_atual):
        entrou.set()
        assert continuar.wait(3), "o teste não libertou o scanner"
        return analisar(mint, par_atual)

    bot.analista.analisar = pausar
    thread = threading.Thread(target=bot.ciclo_scanner)
    thread.start()
    try:
        assert entrou.wait(3), "o scanner não iniciou a análise"
        durante = json.loads(bot.snapshot)
        assert len(durante["em_analise"]) == 1
        moeda = durante["em_analise"][0]
        assert moeda["mint"] == mint_de("ATIVA") and moeda["estado_analise"] == "em_analise"
        assert moeda["preco"] == 0.001 and moeda["pontuacao"] is None
    finally:
        continuar.set()
        thread.join(5)
    assert not thread.is_alive()
    depois = json.loads(bot.snapshot)
    assert depois["em_analise"] == []
    assert len([r for r in depois["recentes"] if r["mint"] == mint_de("ATIVA")]) == 1
    assert depois["recentes"][0]["estado_analise"] == "aprovada"


def t_valores_historicos_paper_scanner_carteira_operacoes(pasta):
    import json
    bot, cfg = novo_bot(pasta)
    listar("WRENCHP")
    listar("Sakura")
    listar("SEMCOMPRA")
    bot.ciclo_scanner()
    assert mint_de("WRENCHP") in bot.estado["posicoes"]
    assert mint_de("Sakura") not in bot.estado["posicoes"]
    assert mint_de("SEMCOMPRA") not in bot.estado["posicoes"]
    bot.atualizar_snapshot()
    d = json.loads(bot.snapshot)
    assert d["trades_disponiveis"] is True
    wrench = mint_de("WRENCHP")
    sakura = mint_de("Sakura")
    semcompra = mint_de("SEMCOMPRA")
    compra_w = next(t for t in d["trades"] if t["mint"] == wrench and t["tipo"] == "COMPRA")
    assert compra_w["valor_usd"] == 5.526
    assert not any(t["mint"] == sakura for t in d["trades"])
    aberta = next(p for p in d["posicoes"] if p["mint"] == wrench)
    assert aberta["custo"] == compra_w["valor_usd"] and aberta["estado"] == "aberta"
    assert aberta["tokens"] > 0 and aberta["preco_efetivo"] == compra_w["preco_efetivo"]
    assert aberta["aberta_em"] > 0 and aberta["preco_atual"] > 0
    assert not any(t["mint"] == semcompra for t in d["trades"])
    assert not any(p["mint"] == semcompra for p in d["posicoes"])
    sinais = {s["mint"]: s for s in d["sinais"]}
    assert sinais[wrench]["estado"] == "comprado" and sinais[sakura]["estado"] == "sem_vaga"
    assert sinais[semcompra]["estado"] == "sem_vaga"
    pagina = B.PAGINA_HTML
    assert "function blocoOperacao(mint)" in pagina and "function historicoOperacoes()" in pagina
    assert "Valor investido" in pagina and "Valor atual bruto" in pagina and "Resultado realizado" in pagina


def t_demo(pasta):
    import random
    random.seed(7)
    bot, cfg = novo_bot(pasta)
    B.preparar_demo(cfg)
    bot.modo = "demo"
    bot.http = bot.scanner.http = bot.analista.http = bot.risco.http = B.MercadoDemo()
    for _ in range(400):
        bot.ciclo_scanner()
        bot.ciclo_risco()
        bot.registar_historico()
        bot.atualizar_snapshot()
        avancar(0.1)
    assert bot.estado["stats"]["fechados"] > 5 and len(bot.estado["sinais"]) > 5
    assert len(bot.estado["sinais"]) <= cfg["max_sinais"] and len(bot.estado["historico"]) <= 400
    assert '"modo": "demo"' in bot.snapshot and bot.valor_total() > 0


def t_pedido_parcial(pasta):
    """Um pedido que falha nao pode ser confundido com 'o par desapareceu'."""
    bot, cfg = novo_bot(pasta)
    for i in range(35):
        listar(f"P{i:02d}", liq=2000)                             # todos rejeitados (liquidez baixa)
    MERCADO["falha_mint"] = mint_de("P33")                        # o 2.o grupo de 30 falha
    r = B.buscar_pares(bot.http, [mint_de(f"P{i:02d}") for i in range(35)])
    assert len(r[mint_de("P00")]) == 1 and r[mint_de("P33")] is None and r[mint_de("P34")] is None
    bot.ciclo_scanner()
    assert mint_de("P00") in bot.estado["vistos"]
    assert mint_de("P33") not in bot.estado["vistos"] and mint_de("P33") not in bot.estado["pendentes"]
    assert next(x for x in bot.recentes if x["mint"] == mint_de("P33"))["status"] == "technical_error"
    MERCADO["falha_mint"] = None
    bot.ciclo_scanner()
    assert mint_de("P33") in bot.estado["vistos"]                 # analisado quando a API voltou
    # no Risco: sem resposta para a posicao nao conta para a "perda total"
    bot, cfg = comprar_um(pasta)
    real = B.buscar_pares
    B.buscar_pares = lambda http, enderecos: {m: None for m in enderecos}
    try:
        for _ in range(cfg["falhas_sem_dados_max"] + 3):
            assert bot.ciclo_risco() == 0
    finally:
        B.buscar_pares = real
    assert bot.estado["posicoes"][mint_de("BOM")]["sem_dados"] == 0


def t_nomes_hostis(pasta):
    """O nome do token e escolhido por desconhecidos: nao pode estragar o Excel, a consola nem a pagina."""
    bot, cfg = novo_bot(pasta)
    listar("EXCEL")
    MERCADO["pares"][mint_de("EXCEL")][0]["baseToken"]["symbol"] = "=HYPERLINK(\"http://x\")"
    listar("ANSI")
    MERCADO["pares"][mint_de("ANSI")][0]["baseToken"]["symbol"] = "\x1b[2J\nA\u202eB\tC\r"
    listar("VAZIO")
    MERCADO["pares"][mint_de("VAZIO")][0]["baseToken"]["symbol"] = None
    listar("IMG")
    MERCADO["pares"][mint_de("IMG")][0]["info"] = {"imageUrl": "javascript:alert(1)"}
    assert bot.ciclo_scanner() == 1
    nomes = sorted(s["simbolo"] for s in bot.estado["sinais"])
    assert sorted(["=HYPERLINK(\"", "[2JABC", "?", "IMG"]) == nomes
    assert len(bot.estado["posicoes"]) == 1
    linhas = trades(cfg)
    assert any(l["simbolo"].startswith("'=") for l in linhas) and not any(l["simbolo"].startswith("=") for l in linhas)
    assert all(s["img"] == "" for s in bot.estado["sinais"])
    par_ok = {"info": {"imageUrl": "https://dd.dexscreener.com/ds-data/tokens/solana/abc.png?size=lg&key=1"}}
    assert B.imagem_do_par(par_ok).startswith("https://dd.dexscreener.com/")
    assert B.imagem_do_par({"info": {"imageUrl": "https://evil.com/dexscreener.com/x.png"}}) == ""
    assert B.imagem_do_par({"info": {"imageUrl": "https://dexscreener.com.evil.io/x.png"}}) == ""
    assert B.imagem_do_par({"info": "x"}) == "" and B.imagem_do_par({}) == ""
    # O campo ja produzido pelo scanner e publicado para o radar sem novo pedido HTTP.
    registo = next(r for r in bot.recentes if r["simbolo"] == "IMG")
    assert registo["img"] == ""
    par_com_imagem = {"baseToken": {"symbol": "IMG", "name": "Imagem de teste"},
                      "info": {"imageUrl": "https://dd.dexscreener.com/ds-data/tokens/solana/abc.png"}}
    bot.registar_analise(mint_de("IMG"), par_com_imagem,
                         {"motivo": "aprovado", "aprovado": True, "pontuacao": 42})
    assert next(r for r in bot.recentes if r["mint"] == mint_de("IMG"))["img"] == B.imagem_do_par(par_com_imagem)
    pagina = B.PAGINA_HTML
    assert "function urlImagemRadar(valor)" in pagina and "u.protocol==='https:'" in pagina
    assert "dexscreener\\.com" in pagina and "function atualizarImagemRadar(n,o)" in pagina
    assert "n.dataset.mint=o.mint" in pagina and "n.addEventListener('click',()=>selecionarMoeda(o.mint))" in pagina
    assert "img.loading='lazy'" in pagina and "img.decoding='async'" in pagina
    assert ".blip-img" in pagina and ".blip-fallback" in pagina and "@keyframes blip-pulso" in pagina
    # Imagens pendentes ficam renderizaveis para o browser iniciar o lazy load; o fallback fica por cima.
    assert "n.dataset.img=url;img.hidden=false;img.style.opacity='0';fallback.hidden=false;img.src=url" in pagina
    assert "if(n.dataset.img===img.src&&img.naturalWidth)" in pagina
    assert "if(n.dataset.img===img.src){img.hidden=true;img.style.opacity='0';fallback.hidden=false}" in pagina
    assert "const valida=img.naturalWidth>0;img.hidden=!valida" in pagina
    # Um par sem imagem usa o fallback; a URL maliciosa rejeitada acima não chega ao DOM.
    bot.atualizar_snapshot()
    bot.painel()


def t_contadores_e_grafico(pasta):
    bot, cfg = novo_bot(pasta)
    listar("NOVO", idade_min=2)                                   # fica em espera e e reanalisado
    for _ in range(5):
        bot.ciclo_scanner()
    assert bot.analisados == 1 and len(bot.recentes) == 1         # conta tokens, nao reanalises
    for i in range(3000):                                         # ~25 horas de simulacao
        avancar(0.5)
        bot.registar_historico()
    h = bot.estado["historico"]
    assert 150 <= len(h) <= 400 and bot.estado["hist_passo"] > 30
    assert h[-1][0] - h[0][0] > 20 * 3600                         # o grafico cobre a simulacao toda
    assert all(h[i][0] < h[i + 1][0] for i in range(len(h) - 1))
    for i in range(70):
        listar(f"Z{i}")
    bot.ciclo_scanner()
    bot.atualizar_snapshot()
    import json
    d = json.loads(bot.snapshot)
    assert d["sinais_24h"] == 70 and len(d["sinais"]) == cfg["max_sinais"] and d["analisados"] == 71
    avancar(25 * 60)
    bot.atualizar_snapshot()
    assert json.loads(bot.snapshot)["sinais_24h"] == 0


def t_porta_ocupada(pasta):
    bot, cfg = novo_bot(pasta)
    bot2 = None
    cfg["painel_web"] = True
    B.iniciar_painel_web(bot)
    try:
        cfg["porta_web"] = bot.servidor_web.server_address[1]
        pasta2 = os.path.join(pasta, "instancia_2")
        os.makedirs(pasta2, exist_ok=True)
        cfg2 = dict(cfg, ficheiro_estado=os.path.join(pasta2, "estado.json"),
                    ficheiro_trades=os.path.join(pasta2, "trades.csv"),
                    ficheiro_log=os.path.join(pasta2, "bot.log"),
                    ficheiro_percursos=os.path.join(pasta2, "percursos.jsonl"))
        bot2 = B.Bot(cfg2)
        assert B.iniciar_painel_web(bot2).startswith("INDISPONIVEL") and bot2.servidor_web is None
    finally:
        bot.servidor_web.shutdown()
        bot.servidor_web.server_close()
        if bot2:
            bot2.close()


def t_laboratorio(pasta):
    import json
    bot, cfg = novo_bot(pasta)
    for nome in ("SOBE", "CAI", "RUG", "SOME", "LADO"):
        listar(nome)
    for i in range(3):
        listar(f"X{i}")                                           # sem vaga: tambem sao gravados
    bot.ciclo_scanner()
    assert len(bot.estado["percursos"]) == 8 and len(bot.estado["posicoes"]) == 1
    cfg["tempo_maximo_min"] = 10 ** 6                             # as posicoes nao interessam aqui
    cfg["take_profit_pct"] = cfg["trailing_ativar_pct"] = 0       # alvo e trailing desligados
    for minuto in range(1, 95):
        avancar(1)
        if minuto <= 40:
            mexer("SOBE", preco=0.001 * (1 + 0.02 * minuto))      # sobe ate +80%
            mexer("CAI", preco=0.001 * (1 - 0.01 * minuto))       # cai ate -40%
        if minuto == 10:
            mexer("RUG", preco=0.0002, liq=500)
        if minuto == 5:
            MERCADO["pares"][mint_de("SOME")] = []
        bot.ciclo_risco()
    assert bot.estado["percursos"] == {} and bot.estado["n_percursos"] == 8
    assert mint_de("SOBE") in bot.estado["posicoes"]              # com alvo desligado nao vendeu aos +25%
    linhas = [json.loads(x) for x in open(cfg["ficheiro_percursos"], encoding="utf-8")]
    por = {x["simbolo"]: x for x in linhas}
    assert por["RUG"]["fim"] == "rug" and por["SOME"]["fim"] == "desapareceu" and por["SOBE"]["fim"] == "tempo"
    assert abs(max(p[1] for p in por["SOBE"]["pts"]) - 1.8) < 0.01 and por["SOBE"]["pts"][-1][0] >= 90 * 60
    r = lambda nome, regra: B.repetir_percurso(por[nome], regra, cfg)
    assert r("SOBE", (-6, 25, 0, 0, 60))[1] == "alvo" and 18 < r("SOBE", (-6, 25, 0, 0, 60))[0] < 22
    assert r("SOBE", (-6, 0, 0, 0, 60))[1] == "tempo" and r("SOBE", (-6, 0, 0, 0, 60))[0] > 60   # sem alvo ganha mais
    assert r("SOBE", (-6, 0, 0, 0, 30))[0] < r("SOBE", (-6, 0, 0, 0, 60))[0]
    assert r("CAI", (-6, 25, 0, 0, 60))[1] == "stop" and -13 < r("CAI", (-6, 25, 0, 0, 60))[0] < -11
    assert r("CAI", (-30, 25, 0, 0, 60))[0] < -30
    assert r("RUG", (-6, 25, 0, 0, 60))[1] == "rug" and r("RUG", (-6, 25, 0, 0, 60))[0] < -80
    assert r("SOME", (-6, 25, 0, 0, 60)) == (-100.0, "desapareceu")
    assert r("LADO", (-6, 25, 10, 8, 60))[1] == "tempo" and -7 < r("LADO", (-6, 25, 10, 8, 60))[0] < -5  # so custos
    por["SOBE"]["pts"] += [[95 * 60, 1.5]]
    assert B.repetir_percurso(dict(por["SOBE"], pts=[[0, 1], [60, 1.3], [120, 1.15]]), (-6, 0, 20, 10, 60), cfg)[1] == "trailing"
    texto = B.analisar(cfg)
    assert "Sinais analisados: 8" in texto and "REGRAS ATUAIS" in texto and "AS 8 MELHORES" in texto and "pouco" in texto
    os.remove(cfg["ficheiro_percursos"])
    assert "Ainda nao ha percursos" in B.analisar(cfg)
    # com muitos sinais aparece a prova dos nove
    with open(cfg["ficheiro_percursos"], "w", encoding="utf-8") as f:
        for i in range(60):
            f.write(json.dumps(dict(por["SOBE" if i % 3 == 0 else "CAI"], ts=1000 + i, pontuacao=70 + i % 30)) + "\n")
        f.write("linha estragada\n")
    texto = B.analisar(cfg)
    assert "PROVA DOS NOVE" in texto and "Sinais analisados: 60" in texto and "90-100" in texto


def t_retomar_depois_de_pausa(pasta):
    import json
    bot, cfg = novo_bot(pasta)
    listar("A")
    listar("B")
    bot.ciclo_scanner()
    bot.ciclo_risco()
    assert len(bot.estado["posicoes"]) == 1 and len(bot.estado["percursos"]) == 2
    saldo_antes = bot.estado["saldo"]
    bot.guardar()
    # pausa curta (2 min): nada muda
    avancar(2)
    b2, _ = novo_bot(pasta, limpar=False)
    assert b2.retomar() == 0 and len(b2.estado["posicoes"]) == 1
    # PC desligado 3 horas, com o preco a cair a pique entretanto
    avancar(180)
    mexer("A", preco=0.0001)
    b3, _ = novo_bot(pasta, limpar=False)
    assert b3.retomar() == 1
    e = b3.estado
    assert e["posicoes"] == {} and e["percursos"] == {} and abs(e["saldo"] - 100.0) < 1e-9
    assert saldo_antes < 100.0
    assert e["stats"]["fechados"] == 0 and e["stats"]["pnl_total"] == 0     # nao conta como trade
    assert sum(s["estado"] == "anulado" for s in e["sinais"]) == 1
    assert sum(s["estado"] == "sem_vaga" for s in e["sinais"]) == 1
    linhas = trades(cfg)
    assert [l["tipo"] for l in linhas].count("ANULADA") == 1
    assert not os.path.exists(cfg["ficheiro_percursos"])                 # percursos interrompidos nao sao gravados
    assert any("parado" in a for a in e["acoes"])
    b3.atualizar_snapshot()
    assert json.loads(b3.snapshot)["stats"]["fechados"] == 0
    # depois de retomar, continua a funcionar normalmente
    b3.guardar()
    listar("C")
    assert b3.ciclo_scanner() == 1
    # primeiro arranque de sempre (sem ultimo_ciclo): nao faz nada
    b4, _ = novo_bot(pasta)
    assert b4.retomar() == 0


def t_paragem_por_perdas(pasta):
    bot, cfg = novo_bot(pasta)
    bot.estado["saldo"] = 55.0                                    # perdeu 45% do capital
    listar("BOM")
    assert bot.ciclo_scanner() == 0 and not bot.estado["posicoes"]
    assert any("suspensas" in a for a in bot.estado["acoes"])


class _JupFalso:
    """Substituto offline do PrecoRapido (cotacao de venda Jupiter)."""
    origem = "jupiter_publico"

    def __init__(self, precos):
        self.precos = list(precos)
        self.pedidos = 0

    def cotar(self, mint, tokens, preco_ref=None):
        self.pedidos += 1
        p = self.precos.pop(0) if self.precos else None
        if p is None:
            return {"estado": "erro", "erro": "timeout", "sem_rota_seguidas": 0}
        return {"estado": "ok", "preco": p, "impacto_pct": 0.01, "rota": "Pump.fun Amm"}

    def esquecer(self, mint):
        pass

    compra = {"estado": "ok", "impacto_pct": 0.05, "rota": "Pump.fun Amm"}

    def cotar_compra(self, mint, valor_usd):
        self.pedidos += 1
        return dict(self.compra)


def t_preco_rapido_stop_com_dexscreener_atrasada(pasta):
    bot, cfg = comprar_um(pasta)
    m = mint_de("BOM")
    assert bot.ciclo_risco() == 0                                 # DexScreener: 0.001 (sem mexer)
    bot.preco_rapido = _JupFalso([0.00098, 0.00093])
    assert bot.ciclo_rapido() == 0                                # Jupiter -2%: mantem
    pos = bot.estado["posicoes"][m]
    assert pos["fonte_preco"] == "jupiter" and abs(pos["preco_atual"] - 0.00098) < 1e-12
    assert len(pos["hist"]) == len(pos["hist_t"])
    assert bot.ciclo_rapido() == 1 and not bot.estado["posicoes"]  # Jupiter -7% com a DexScreener ainda a 0.001
    v = trades(cfg)[-1]
    assert v["motivo"].startswith("STOP_LOSS") and abs(float(v["preco_mercado"]) - 0.00093) < 1e-12
    assert bot.ciclo_rapido() == 0 and not bot.risco.precos_rapidos  # limpa o preco da posicao fechada


def t_preco_rapido_seguranca(pasta):
    bot, cfg = comprar_um(pasta)
    m = mint_de("BOM")
    assert bot.ciclo_risco() == 0
    bot.preco_rapido = _JupFalso([0.0005])                        # 1.o preco -50% face a DexScreener: ignorado
    assert bot.ciclo_rapido() == 0 and m in bot.estado["posicoes"] and not bot.risco.precos_rapidos
    bot.preco_rapido = _JupFalso([None])                          # Jupiter falha: nada muda
    assert bot.ciclo_rapido() == 0 and bot.estado["posicoes"][m]["sem_dados"] == 0
    bot.preco_rapido = _JupFalso([0.00099] * 12)
    bot._pares_risco = {m: []}                                    # ciclo rapido nunca conta falhas de dados
    for _ in range(12):
        bot.ciclo_rapido()
    assert m in bot.estado["posicoes"] and bot.estado["posicoes"][m]["sem_dados"] == 0
    bot.risco.precos_rapidos[m] = {"preco": 0.0001, "ts": B.agora() - 60}  # preco antigo deixa de ser usado
    assert bot.ciclo_risco() == 0 and bot.estado["posicoes"][m]["fonte_preco"] == "dexscreener"
    bot.atualizar_snapshot()
    snap_pos = json.loads(bot.snapshot)["posicoes"][0]
    assert snap_pos["fonte_preco"] == "dexscreener" and len(snap_pos["hist_t"]) == len(snap_pos["hist"])


def t_percurso_controlo_e_laboratorio(pasta):
    bot, cfg = novo_bot(pasta)
    cfg["percursos_controlo_max"] = 1
    a, b, c = mint_de("CTRLA"), mint_de("CTRLB"), mint_de("SINAL")
    bot.iniciar_percurso(a, par("CTRLA", var_m5=12), {"pontuacao": 60}, grupo="controlo")
    bot.iniciar_percurso(b, par("CTRLB"), {"pontuacao": 60}, grupo="controlo")   # limite do controlo
    bot.iniciar_percurso(c, par("SINAL"), {"pontuacao": 80})
    pc = bot.estado["percursos"]
    assert pc[a]["grupo"] == "controlo" and b not in pc and pc[c]["grupo"] == "sinal"
    assert pc[a]["feat"]["m5"] == 12 and pc[a]["feat"]["compras_m5"] == 30 and pc[a]["feat"]["idade_min"] == 60
    bot.iniciar_percurso(mint_de("SEMNOTA"), par("SEMNOTA"), {"pontuacao": None}, grupo="controlo")
    assert mint_de("SEMNOTA") not in pc
    pc[a]["pts"].append([600, 1.2])
    pc[c]["pts"].append([600, 0.9])
    bot._fechar_percurso(a, "tempo", 50000)
    bot._fechar_percurso(c, "tempo", 50000)
    with open(cfg["ficheiro_percursos"], encoding="utf-8") as f:
        linhas = [json.loads(l) for l in f]
    assert [l["grupo"] for l in linhas] == ["controlo", "sinal"] and "feat" in linhas[0]
    texto = B.analisar(cfg)
    assert "Sinais analisados: 1" in texto and "recusados (controlo)" in texto, texto
    assert "POR CARACTERISTICA DE ENTRADA - 2 percursos" in texto and "10 a 30%" in texto, texto
    os.remove(cfg["ficheiro_percursos"])
    with open(cfg["ficheiro_percursos"], "w", encoding="utf-8") as f:
        f.write(json.dumps(linhas[0]) + "\n")
    texto = B.analisar(cfg)                                       # so controlo: nao rebenta
    assert "Ainda nao ha percursos completos de sinais aprovados" in texto and "recusados (controlo)" in texto
    os.remove(cfg["ficheiro_percursos"])


def t_rota_compra_jupiter_antes_da_proposta(pasta):
    for resposta, compra in (({"estado": "erro", "erro": "sem_rota"}, False),
                             ({"estado": "ok", "impacto_pct": 4.0}, False),
                             ({"estado": "erro", "erro": "timeout"}, True),     # Jupiter em baixo: nao bloqueia
                             ({"estado": "ok", "impacto_pct": 0.2}, True)):
        bot, cfg = novo_bot(pasta)
        bot.preco_rapido = _JupFalso([])
        bot.preco_rapido.compra = resposta
        listar("ROTA")
        assert bot.ciclo_scanner() == (1 if compra else 0), resposta
        if not compra:
            assert any("sem proposta" in a for a in bot.estado["acoes"]), bot.estado["acoes"]
            assert mint_de("ROTA") in bot.estado["percursos"]           # o laboratorio continua a seguir o sinal
        MERCADO["perfis"].clear()
        MERCADO["pares"].clear()

def t_laboratorio_estrategia_post(pasta):
    bot, cfg = novo_bot(pasta)
    s, tx = cfg["slippage_pct"] / 100.0, cfg["taxa_pct"] / 100.0
    # sobe, recua 20% (entra a 1.2), dobra (vende metade), sobe mais e recua 23% do pico (trailing no resto)
    pc = {"pts": [[0, 1.0], [60, 1.5], [120, 1.2], [180, 2.5], [240, 3.0], [300, 2.3], [360, 2.2]], "fim": "tempo"}
    r, motivo = B.repetir_avancado(pc, cfg, queda=(15, 35), stop=-25, parcial_2x=True, trail_dist=20, minutos=180)
    esperado = ((1 - tx) / (1 + s) * (0.5 * (2.5 / 1.2) * (1 - s) * (1 - tx) + 0.5 * (2.3 / 1.2) * (1 - s) * (1 - tx)) - 1) * 100
    assert motivo == "trailing" and abs(r - esperado) < 1e-9, (r, esperado, motivo)
    # faca a cair: recua mais de 35% de uma vez, nunca compra
    assert B.repetir_avancado({"pts": [[0, 1.0], [60, 0.5], [120, 0.3], [180, 0.2]], "fim": "tempo"}, cfg,
                              queda=(15, 35)) is None
    # entra na queda e continua a cair: stop a -25% desde a entrada
    r, motivo = B.repetir_avancado({"pts": [[0, 1.0], [60, 0.8], [120, 0.7], [180, 0.59], [240, 0.5]], "fim": "tempo"},
                                   cfg, queda=(15, 35), stop=-25)
    assert motivo == "stop" and r < -25, (r, motivo)
    # rug no fim: sai com o slippage de rug
    r, motivo = B.repetir_avancado({"pts": [[0, 1.0], [60, 1.1], [120, 0.05]], "fim": "rug", "liq_fim": 100}, cfg)
    assert motivo == "rug" and r < -90
    # o relatorio do laboratorio mostra a secao
    with open(cfg["ficheiro_percursos"], "w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps(dict(pc, simbolo=f"P{i}", pontuacao=75, ts=1000 + i, liq0=20000)) + "\n")
    texto = B.analisar(cfg)
    assert "ESTRATEGIA DO POST" in texto and "post completo" in texto and "so entrada na queda" in texto, texto
    os.remove(cfg["ficheiro_percursos"])

def t_log_analise_sem_repeticoes(pasta):
    import logging
    bot, cfg = novo_bot(pasta)
    registos = []
    h = logging.Handler(level=logging.INFO)
    h.emit = lambda r: registos.append(r.getMessage())
    nivel = B.log.level
    B.log.addHandler(h)
    B.log.setLevel(logging.INFO)
    try:
        listar("NOVO", idade_min=2)
        bot.ciclo_scanner()
        avancar(1)
        bot.ciclo_scanner()
    finally:
        B.log.removeHandler(h)
        B.log.setLevel(nivel)
    analises = [r for r in registos if r.startswith("ANALISE NOVO")]
    assert len(analises) == 1, registos
    assert bot.estado["scanner_counters"]["session"]["analysis_completed"] >= 2  # foi mesmo reanalisado


def t_telegram_em_segundo_plano(pasta):
    enviados = []
    original = B.requests.post
    B.requests.post = lambda *a, **k: enviados.append(k["json"]["text"])
    try:
        tg = B.Telegram("123456:" + "a" * 30, "987654", em_segundo_plano=True)
        tg.enviar("ola")
        for _ in range(100):
            if enviados:
                break
            time.sleep(0.01)
    finally:
        B.requests.post = original
    assert enviados == ["ola"] and not B.Telegram("", "").ativo


class _TgFalso:
    """Telegram de teste: guarda as mensagens em vez de as enviar."""
    ativo = True
    em_segundo_plano = False

    def __init__(self):
        self.msgs = []

    def enviar(self, texto):
        self.msgs.append(texto)


def t_scanner_cede_a_vez_ao_preco_rapido(pasta):
    bot, cfg = comprar_um(pasta)
    m = mint_de("BOM")
    assert bot.ciclo_risco() == 0
    assert bot.scanner.entre_pedidos == bot._vigiar_posicao_no_scanner
    bot.preco_rapido = _JupFalso([0.00099] * 9)
    RELOGIO["t"] += 5
    bot.scanner.tokens_recentes()                                  # o Scanner cede a vez entre os seus pedidos
    assert bot.preco_rapido.pedidos == 1
    RELOGIO["t"] += 5
    bot.preco_rapido = _JupFalso([0.00099] * 6)
    assert bot._vigiar_posicao_no_scanner() == 0 and bot.preco_rapido.pedidos == 1  # primeira leitura
    assert bot._vigiar_posicao_no_scanner() == 0 and bot.preco_rapido.pedidos == 1  # ainda nao passaram 3 s
    listar("OUTRO")
    listar("MAIS")
    RELOGIO["t"] += 5
    antes = bot.preco_rapido.pedidos
    bot.ciclo_scanner()
    assert bot.preco_rapido.pedidos > antes and m in bot.estado["posicoes"]  # leu o preco a meio das analises
    bot.preco_rapido = _JupFalso([0.0009])                        # -10% durante o scanner (stop de teste: -6%)
    listar("TERCEIRO")
    RELOGIO["t"] += 5
    bot.ciclo_scanner()
    assert m not in bot.estado["posicoes"]
    assert any(v["mint"] == m and v["tipo"] == "VENDA" and v["motivo"].startswith("STOP_LOSS") for v in trades(cfg))
    for aberto in list(bot.estado["posicoes"]):                   # sem posicao aberta nao faz pedidos
        bot.executor.anular(aberto, "teste")
    bot.preco_rapido = _JupFalso([0.001])
    assert bot._vigiar_posicao_no_scanner() == 0 and bot.preco_rapido.pedidos == 0


def t_http_urgente_nao_atrasa_os_stops(pasta):
    bot, cfg = novo_bot(pasta)
    http = bot.http
    MERCADO["modo"] = "em_baixo"
    n = MERCADO["pedidos"]
    assert http.get_json(B.URL_PERFIS, "perfis") is None and MERCADO["pedidos"] - n == cfg["tentativas"]
    http.urgente = True
    n = MERCADO["pedidos"]
    assert http.get_json(B.URL_PERFIS, "perfis") is None and MERCADO["pedidos"] - n == 1  # sem repeticoes
    MERCADO["modo"] = "429"
    n = MERCADO["pedidos"]
    assert http.get_json(B.URL_PERFIS, "perfis") is None and MERCADO["pedidos"] - n == 1
    assert http.get_json(B.URL_PERFIS, "perfis") is None and MERCADO["pedidos"] - n == 1  # em pausa: nem pede
    assert http.get_json(B.URL_BOOSTS, "pares") is None and MERCADO["pedidos"] - n == 2   # outro grupo nao e afetado
    http.urgente = False
    MERCADO["modo"] = "ok"
    assert http.get_json(B.URL_PERFIS, "perfis") == []            # fora do modo urgente a pausa nao se aplica
    bot._atualizar_urgencia_http()
    assert bot.http.urgente is False
    listar("BOM")
    assert bot.ciclo_scanner() == 1
    bot._atualizar_urgencia_http()
    assert bot.http.urgente is True                               # com posicao aberta o Bot liga o modo urgente


def t_aviso_telegram_preco_rapido_em_falha(pasta):
    bot, cfg = comprar_um(pasta)
    m = mint_de("BOM")
    simbolo = bot.estado["posicoes"][m]["simbolo"]
    assert bot.ciclo_risco() == 0
    bot.telegram = tg = _TgFalso()
    bot.preco_rapido = _JupFalso([None, None, None])
    bot.ciclo_rapido()
    bot.ciclo_rapido()
    assert len(tg.msgs) == 1 and simbolo in tg.msgs[0] and "timeout" in tg.msgs[0]  # 1 aviso, nao 1 por ciclo

    class _SemRota(_JupFalso):
        def cotar(self, mint, tokens, preco_ref=None):
            self.pedidos += 1
            return {"estado": "erro", "erro": "sem_rota", "sem_rota_seguidas": self.pedidos}

    bot.preco_rapido = _SemRota([])
    bot.ciclo_rapido()
    bot.ciclo_rapido()
    assert len(tg.msgs) == 2 and "rota de VENDA" in tg.msgs[1]    # aviso proprio, logo a seguir ao outro
    assert m in bot.estado["posicoes"]                            # e so um aviso: nao vende por causa disto
    bot.preco_rapido = _JupFalso([0.00099])
    assert bot.ciclo_rapido() == 0 and len(tg.msgs) == 2          # voltou a responder: sem mais avisos


def t_percursos_finos_so_gravacao(pasta):
    bot, cfg = comprar_um(pasta)
    m = mint_de("BOM")
    assert m in bot.estado["percursos"] and bot.ciclo_risco() == 0
    bot.preco_fino = fino = _JupFalso([0.00101, 0.00102, 0.00103])
    assert bot.ciclo_percursos_finos() == 0 and fino.pedidos == 0  # com posicao aberta nao gasta pedidos Jupiter
    bot.preco_rapido = _JupFalso([0.00099, 0.0009])
    assert bot.ciclo_rapido() == 0
    assert [p[1] for p in bot.estado["percursos"][m]["ptsj"]] == [0.99]  # a cotacao da posicao serve de ponto fino
    assert bot.ciclo_rapido() == 1 and m not in bot.estado["posicoes"]   # stop
    pc = bot.estado["percursos"][m]
    assert [p[1] for p in pc["ptsj"]] == [0.99, 0.9] and pc["pts"][0] == [0, 1.0]
    pts_antes = copy.deepcopy(pc["pts"])
    assert bot.ciclo_percursos_finos() == 1 and fino.pedidos == 1  # sem posicao: 1 cotacao por chamada
    pc = bot.estado["percursos"][m]
    assert [p[1] for p in pc["ptsj"]] == [0.99, 0.9, 1.01] and pc["pts"] == pts_antes  # 'pts' nunca e tocado
    pc["grupo"] = "controlo"
    assert bot.ciclo_percursos_finos() == 0 and fino.pedidos == 1  # grupo de controlo nao gasta pedidos
    pc["grupo"] = "sinal"
    bot.cfg["percursos_finos"] = False
    assert bot.ciclo_percursos_finos() == 0 and fino.pedidos == 1  # desligado no CONFIG
    bot.cfg["percursos_finos"] = True
    bot.preco_fino = _JupFalso([None])
    assert bot.ciclo_percursos_finos() == 0                        # Jupiter falha: nada muda
    assert len(bot.estado["percursos"][m]["ptsj"]) == 3
    bot.preco_fino = espera = _JupFalso([0.00105])
    assert bot.ciclo_percursos_finos() == 0 and espera.pedidos == 0  # ...e nao insiste nos 30 s seguintes
    RELOGIO["t"] += 31
    assert bot.ciclo_percursos_finos() == 1 and espera.pedidos == 1
    assert [p[1] for p in bot.estado["percursos"][m]["ptsj"]] == [0.99, 0.9, 1.01, 1.05]
    bot.preco_fino = fino
    avancar(31)
    assert bot.ciclo_percursos_finos() == 0 and fino.pedidos == 1  # passados os 30 minutos deixa de cotar
    avancar(60)
    bot.ciclo_risco()                                              # o percurso (90 min nos testes) fecha
    assert m not in bot.estado["percursos"]
    with open(cfg["ficheiro_percursos"], encoding="utf-8") as f:
        linha = json.loads(f.read().splitlines()[-1])
    assert linha["mint"] == m and linha["pts"][0] == [0, 1.0]
    assert [p[1] for p in linha["ptsj"]] == [0.99, 0.9, 1.01, 1.05]
    assert "LABORATORIO" in B.analisar(cfg)                        # o laboratorio continua a ler o ficheiro


def t_fonte_do_preco_na_observacao(pasta):
    bot, cfg = comprar_um(pasta)

    class _Obs:
        def __init__(self):
            self.eventos = []

        def record_evaluation(self, evento):
            self.eventos.append(evento)

        def note_caller_error(self):
            pass

    original = bot.risco.observability
    bot.risco.observability = obs = _Obs()
    try:
        assert bot.ciclo_risco() == 0 and obs.eventos[-1]["data_source"] == "DexScreener"
        bot.preco_rapido = _JupFalso([0.00099])
        assert bot.ciclo_rapido() == 0 and obs.eventos[-1]["data_source"] == "Jupiter+DexScreener"
    finally:
        bot.risco.observability = original


def t_laboratorio_pontos_finos_e_custos_reais(pasta):
    bot, cfg = novo_bot(pasta)
    pc = {"mint": mint_de("LABA"), "simbolo": "LABA", "pontuacao": 80, "ts": 1_799_000_000, "preco0": 0.001,
          "liq0": 50000, "fim": "tempo", "liq_fim": 50000,
          "pts": [[0, 1.0], [33, 1.02], [66, 0.99], [99, 1.3], [132, 1.1], [5400, 1.05]]}
    rug = dict(pc, mint=mint_de("LABB"), ts=1_799_000_100, fim="rug", liq_fim=1000, pts=[[0, 1.0], [30, 1.1], [60, 0.2]])
    regras = ((-6, 25, 10, 8, 60), (-12, 100, 20, 15, 120), (-30, 0, 0, 0, 30), (-6, 50, 0, 0, 180))
    for regra in regras:                                          # sem pontos finos: igual ao laboratorio antigo
        for x in (pc, rug):
            antigo = B.repetir_percurso(x, regra, cfg)
            novo = B.repetir_serie(x, B._serie_fina(x), regra, cfg)
            assert abs(antigo[0] - novo[0]) < 1e-9 and antigo[1] == novo[1] and novo[2] is False
    assert B._serie_fina(pc) == [(p[0], p[1], False) for p in pc["pts"]]
    fino = dict(pc, mint=mint_de("LABC"), ts=1_799_000_200, par="PARLABC",
                ptsj=[[10, 0.99], [20, 0.93], [30, 1.0], [40, 1.01], [9999, 5.0], [0, 1.0]],
                obs={"jupiter": {"slippage_real_pct": 1.0},
                     "holders": {"top10_pct": 60.0, "top": [["PARLABC", 30.0], ["DonoA", 5.0], ["DonoB", 4.0]]}})
    serie = B._serie_fina(fino)
    assert serie[0] == (0, 1.0, False) and serie[-1] == (5400, 1.05, False)        # inicio e fim sao os normais
    assert (20, 0.93, True) in serie and (9999, 5.0, True) not in serie            # fora do percurso: ignorado
    assert (33, 1.02, False) not in serie and (66, 0.99, False) not in serie       # cobertos por pontos finos
    assert (99, 1.3, False) in serie and (132, 1.1, False) in serie                # longe dos finos: mantem-se
    assert [p[0] for p in serie] == sorted(p[0] for p in serie)
    regra = (-6, 25, 10, 8, 60)
    normal = B.repetir_percurso(fino, regra, cfg)
    assert normal[1] == "alvo"                                    # os pontos normais nao veem a queda de -7%
    simulado = B.repetir_serie(fino, serie, regra, cfg)
    assert simulado[1] == "stop" and simulado[0] < 0 < normal[0]  # com pontos finos o stop dispara antes do alvo
    real = B.repetir_serie(fino, serie, regra, cfg, valor_usd=5.5, custos="reais")
    assert real[1] == "stop" and real[2] is True
    assert abs(real[0] - (0.93 / 1.01 - 2 * 0.05 / 5.5 - 1) * 100) < 1e-9          # entrada real, saida real, rede
    sem_cotacao = dict(fino, obs={})
    r2 = B.repetir_serie(sem_cotacao, serie, regra, cfg, valor_usd=5.5, custos="reais")
    assert r2[2] is False and r2[0] < real[0]                     # sem cotacao de entrada usa a simulada (mais cara)
    rug_real = B.repetir_serie(rug, B._serie_fina(rug), (-99, 0, 0, 0, 180), cfg, valor_usd=5.5, custos="reais")
    assert rug_real[1] == "rug" and rug_real[0] < -80             # rug: nunca usa a saida "sem custos"
    with open(cfg["ficheiro_percursos"], "w", encoding="utf-8") as f:
        for x in (pc, rug):
            f.write(json.dumps(x) + "\n")
    txt = B.analisar(cfg)
    assert "PONTOS FINOS (preco de venda real no Jupiter) - 0 de 2" in txt and "Ainda sem dados" in txt
    assert "OPERACOES PAPER FECHADAS, POR DIA" in txt and "Ainda sem operacoes fechadas" in txt
    with open(cfg["ficheiro_percursos"], "a", encoding="utf-8") as f:
        f.write(json.dumps(fino) + "\n")
    txt = B.analisar(cfg)
    assert "PONTOS FINOS (preco de venda real no Jupiter) - 1 de 3" in txt
    assert "pontos finos,   custos reais" in txt and "1 de 1 com cotacao real de entrada" in txt
    assert "finos+reais" in txt and "ATENCAO: 1 percursos com pontos finos" in txt
    assert "pool do par identificada em 1" in txt and "top10 SEM a pool <= 25%" in txt   # 5% + 4% sem a pool


def t_percurso_grava_par_e_operacoes_por_dia(pasta):
    bot, cfg = comprar_um(pasta)
    m = mint_de("BOM")
    mexer("BOM", preco=0.0009)                                    # -10%: stop de teste (-6%)
    assert bot.ciclo_risco() == 1
    avancar(91)
    bot.ciclo_risco()                                             # fecha o percurso
    with open(cfg["ficheiro_percursos"], encoding="utf-8") as f:
        linha = json.loads(f.read().splitlines()[-1])
    assert linha["mint"] == m and linha["par"] == "PARBOM"
    txt = B.analisar(cfg)
    dia = B.data_txt()[:10]
    assert "OPERACOES PAPER FECHADAS, POR DIA" in txt
    assert any(l.strip().startswith(dia) and "1 fechadas" in l and " 0 ganhas" in l for l in txt.splitlines())


def t_laboratorio_carteiras_e_painel_sem_a_lista(pasta):
    bot, cfg = novo_bot(pasta)
    base = {"simbolo": "X", "pontuacao": 80, "preco0": 0.001, "liq0": 50000, "liq_fim": 50000}

    def pc(nome, i, pts, fim, top, dentro=None):
        d = dict(base, mint=mint_de(nome), ts=1_799_000_000 + i, par="PAR" + nome, fim=fim, pts=pts,
                 obs={"holders": {"top10_pct": 50.0, "top": top}})
        if dentro is not None:
            d["obs"]["carteiras"] = {"estado": "ok", "n_lista": 2, "consultadas": 2, "dentro": dentro, "quem": []}
        return d

    sobe, cai = [[0, 1.0], [60, 1.8], [5400, 1.6]], [[0, 1.0], [60, 0.5], [120, 0.2]]
    lista = [pc("CA", 1, sobe, "tempo", [["PARCA", 30.0], ["DonoBom", 5.0], ["DonoMau", 2.0]], dentro=1),
             pc("CB", 2, sobe, "tempo", [["DonoBom", 4.0], ["DonoMau", 2.0], [None, 1.0]], dentro=2),
             pc("CC", 3, cai, "rug", [["DonoBom", 3.0], ["DonoMau", 9.0], ["Raro", 1.0]], dentro=0),
             pc("CD", 4, cai, "rug", [["DonoMau", 8.0], ["PARCD", 20.0]])]
    with open(cfg["ficheiro_percursos"], "w", encoding="utf-8") as f:
        for x in lista:
            f.write(json.dumps(x) + "\n")
    txt = B.analisar(cfg)
    assert "CARTEIRAS (so observacao" in txt and "3 percursos com a lista consultada" in txt
    for nome in ("nenhuma carteira seguida dentro", "1 carteira seguida dentro", "2 ou mais dentro"):
        assert nome in txt
    linhas = [l.split() for l in txt.splitlines() if "chegaram a +50%" in l and "acabaram em rug" in l]
    assert [(l[0], l[1], l[3], l[7]) for l in linhas] == [("DonoBom", "3", "2", "1"), ("DonoMau", "4", "2", "2")]
    assert "PARCA " not in txt and "Raro" not in txt              # a pool e os donos raros nao entram
    with open(cfg["ficheiro_percursos"], "w", encoding="utf-8") as f:
        f.write(json.dumps(dict(lista[0], obs={})) + "\n")
    txt = B.analisar(cfg)
    assert "Carteiras seguidas: ainda sem dados" in txt and "Ainda nenhum" in txt
    # O dashboard (e a pagina publica) nunca recebem a lista de carteiras seguidas nem os donos das contas.
    limpa = B.Bot._observacao_para_o_painel({"holders": {"top10_pct": 1, "top": [["a", 1]]},
                                             "carteiras": {"dentro": 1, "quem": [["w", 1]]}, "jupiter": {"estado": "ok"}})
    assert limpa == {"holders": {"top10_pct": 1}, "carteiras": {"dentro": 1}, "jupiter": {"estado": "ok"}}
    bot.recentes = [{"mint": mint_de("CA")}]
    bot.obs_extra._dados[mint_de("CA")] = {"carteiras": {"dentro": 1, "quem": [["CarteiraSecreta", 1.0]]},
                                           "holders": {"top": [["DonoQualquer", 2.0]]}}
    bot.atualizar_snapshot()
    assert "CarteiraSecreta" not in bot.snapshot and "DonoQualquer" not in bot.snapshot
    assert json.loads(bot.snapshot)["recentes"][0]["observacao_extra"]["carteiras"] == {"dentro": 1}


def t_mensagens_telegram_legiveis(pasta):
    bot, cfg = novo_bot(pasta)
    bot.telegram = tg = _TgFalso()
    listar("BOM")
    assert bot.ciclo_scanner() == 1
    m = mint_de("BOM")
    simbolo = bot.estado["posicoes"][m]["simbolo"]
    proposta = next(x for x in tg.msgs if "NOVA PROPOSTA" in x)
    assert proposta.startswith("🔔 [PAPER] NOVA PROPOSTA — $" + simbolo) and "(mínimo 70)" in proposta
    assert "Liquidez: $60,000 · Market cap: $400,000" in proposta and "Idade: 60 min" in proposta
    assert "⏳ Expira em 90 segundos." in proposta and proposta.endswith("https://dexscreener.com/solana/" + m)
    compra = next(x for x in tg.msgs if "COMPRA" in x)
    assert compra.startswith("🟢 [PAPER] COMPRA — $" + simbolo) and "Preço de entrada: $0.00" in compra
    assert "Saídas: stop -6% · alvo +25% · trailing 10/8 · máximo 60 min" in compra
    mexer("BOM", preco=0.0009)
    assert bot.ciclo_risco() == 1
    venda = tg.msgs[-1]
    assert venda.startswith("🔴 [PAPER] VENDA — $" + simbolo) and "Resultado: -$" in venda
    assert "Motivo: stop-loss (preço a -10.0%)" in venda and "Carteira: $" in venda
    for tecnico, legivel in (("TRAILING +15.8% (pico +36.3%)", "trailing stop (saiu a +15.8%; pico +36.3%)"),
                             ("TAKE_PROFIT +101.2%", "alvo atingido (preço a +101.2%)"),
                             ("TEMPO 120min (+3.0%)", "tempo máximo (120 min; preço a +3.0%)"),
                             ("RUG liquidez $2,475", "possível rug: liquidez $2,475"),
                             ("RUG preco caiu 55% de repente", "possível rug: preço caiu 55% de repente"),
                             ("outra coisa", "outra coisa"), ("", "—")):
        assert B.motivo_legivel(tecnico) == legivel, tecnico
    assert B.texto_venda("X", "TRAILING +15.8% (pico +36.3%)", 0.49, 8.9, 1.7, 79.2).startswith("✅ [PAPER] VENDA — $X\nResultado: +$0.49 (+8.9%)")
    assert B.texto_venda("X", "RUG liquidez $2,475", -11.68, -97.3, 0.4, 68.0).startswith("⚠️ [PAPER] VENDA")
    assert "Duração: menos de 1 min" in B.texto_venda("X", "STOP_LOSS -12.0%", -1.0, -18.0, 0.4, 68.0)
    sem_dados = B.texto_proposta(cfg, "X", "mint invalido", 80, "5.526", None)
    assert "Valor a simular: $5.53" in sem_dados and "Liquidez" not in sem_dados and "dexscreener" not in sem_dados
    resumo = bot.resumo_telegram()
    assert resumo.startswith("📊 [PAPER] RESUMO\nCarteira: $") and "Fechadas: 1 · 0 ganhas · 1 perdidas (0%)" in resumo
    assert "Posição aberta: nenhuma" in resumo and "tokens analisados" in resumo and "====" not in resumo
    assert bot._texto_estado_bot("▶️", "iniciado").startswith("▶️ [PAPER] Bot iniciado · carteira $")
    assert bot._texto_estado_bot("▶️", "iniciado").endswith("sem posição aberta")
    assert all("[PAPER]" in x for x in tg.msgs)                   # nunca se confunde com dinheiro real
    assert B.texto_compra({}, "X", m, "5.526", 0.0000565, None, 0).startswith("🟢 [PAPER] COMPRA — $X\nValor: $5.53")
    assert "Saídas" not in B.texto_compra({}, "X", m, 5, 0.001, 80, 0) and "mínimo" not in B.texto_proposta({}, "X", m, 80, 5, {})

    class _TgAvariado(_TgFalso):
        def enviar(self, texto):
            raise RuntimeError("telegram em baixo")

    bot2, cfg2 = novo_bot(pasta)
    bot2.telegram = _TgAvariado()
    listar("BOM")
    assert bot2.ciclo_scanner() == 1 and mint_de("BOM") in bot2.estado["posicoes"]   # compra feita apesar do aviso falhar
    mexer("BOM", preco=0.0009)
    assert bot2.ciclo_risco() == 1 and not bot2.estado["posicoes"]                   # venda feita apesar do aviso falhar


def main():
    B.requests.get = get_falso
    B.requests.post = lambda *a, **k: Resposta(200, {})
    B.agora = lambda: RELOGIO["t"]
    B.dormir = lambda s: RELOGIO.__setitem__("t", RELOGIO["t"] + max(s, 0))
    os.environ.pop("TELEGRAM_TOKEN", None)
    testes = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    falhas = 0
    with tempfile.TemporaryDirectory() as pasta:
        for t in testes:
            saida = sys.stdout
            sys.stdout = open(os.devnull, "w")
            try:
                t(pasta)
                res = "OK"
            except Exception:  # noqa: BLE001
                import traceback
                res = "FALHOU: " + traceback.format_exc(limit=3)
                falhas += 1
            finally:
                sys.stdout.close()
                sys.stdout = saida
            print(f"{t.__name__:<26} {res}")
        for bot in _BOTS_COM_SNAPSHOTS:
            bot.close()
        _BOTS_COM_SNAPSHOTS.clear()
        fechar_log()
    print(f"\n{len(testes) - falhas}/{len(testes)} testes passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
