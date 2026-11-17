#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT DE PAPER TRADING - MEMECOINS SOLANA (4 agentes)
====================================================
ATENCAO: isto e apenas uma SIMULACAO. Nao usa carteiras, nao usa chaves
privadas e nao faz nenhuma compra/venda real. O saldo e ficticio.

Agentes:
  1. Scanner   -> procura tokens novos na DexScreener
  2. Analista  -> da uma pontuacao 0-100 a cada token
  3. Executor  -> simula a compra (com slippage e taxas)
  4. Risco     -> vigia as posicoes abertas e simula a venda

Como correr:   python bot.py
Recomecar:     python bot.py --reset --confirmar-reset
Ver resumo:    python bot.py --resumo
Ver a demo:    python bot.py --demo   (mercado inventado, so para ver a plataforma)
Laboratorio:   python bot.py --analisar   (que regras dariam mais, com os dados reais gravados)

Plataforma de sinais: com o bot a correr, abre http://localhost:8000 no browser.
"""

import argparse
import bisect
import copy
import csv
import hashlib
import io
import hmac
import json
import logging
import math
import os
import queue
import random
import re
import secrets
import signal
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from early_accumulation import score_acumulacao
from smart_filters_market import MarketSnapshotRecorder
from smart_protection_observability import SmartProtectionObservability
from observacao_extra import ObservadorExtra, configurar_rpc, ler_carteiras_seguidas
from preco_rapido import PrecoRapido, ler_chaves_locais
from paper_trading_limits import (
    ECB_SOURCE, EcbRateError, MAX_RATE_AGE_DAYS, MAX_OPERATION_EUR, MAX_OPEN_POSITIONS,
    OperationalConfigError, ProcessInstanceLock,
    canonical_reset_paths, fetch_ecb_rate, load_operational_config, usd_cap_from_eur,
    validate_locked_session_rate, validate_rate, state_access_lock,
)
from paper_operations import (
    PaperOperationAuthorizationError, PaperOperationError, PaperOperationStore,
    atomic_write_bytes, atomic_write_json,
    reset_pending_path, sync_state_in_place,
)
from paper_audit_log import PaperAuditError, PaperAuditJournal, sanitize_audit_text

# ============================================================================
# DEFINICOES - altera aqui os valores a teu gosto
# ============================================================================
CONFIG = {
    # ---------------- Simulacao ----------------
    "saldo_inicial_usd": 100.0,      # saldo ficticio com que a simulacao comeca
    "ficheiro_estado": "estado.json",  # onde o bot guarda o saldo e as posicoes
    "ficheiro_trades": "trades.csv",   # historico de todas as compras e vendas
    "ficheiro_log": "bot.log",         # registo detalhado (pontuacoes e motivos)

    # ---------------- Scanner (Agente 1) ----------------
    "intervalo_scanner_seg": 30,     # de quantos em quantos segundos procura tokens novos
    "janela_reanalise_min": 20,      # tokens "quase bons" ou muito novos sao reanalisados durante X minutos
    "max_pendentes": 60,             # maximo de tokens em lista de espera para reanalise
    "max_vistos": 5000,              # quantos tokens ja analisados o bot guarda em memoria

    # ---------------- Analista (Agente 2) ----------------
    "pontuacao_minima": 70,          # so compra se a pontuacao for >= a este valor (0-100)
    # Filtros eliminatorios (se falhar um destes, o token e rejeitado de imediato)
    "liquidez_minima_usd": 15000,    # liquidez minima da pool
    "idade_minima_min": 5,           # idade minima do par, em minutos (evita os primeiros minutos caoticos)
    "idade_maxima_horas": 24,        # idade maxima do par, em horas
    "fdv_minimo_usd": 20000,         # market cap / FDV minimo
    "fdv_maximo_usd": 5000000,       # market cap / FDV maximo
    "volume_h1_minimo_usd": 5000,    # volume minimo na ultima hora
    "txns_h1_minimo": 30,            # numero minimo de transacoes na ultima hora
    "subida_h1_maxima_pct": 150,     # rejeita se ja subiu mais do que isto na ultima hora (ja "pumpou")
    "subida_m5_maxima_pct": 60,      # rejeita se subiu mais do que isto nos ultimos 5 minutos
    "queda_m5_maxima_pct": -20,      # rejeita se caiu mais do que isto nos ultimos 5 minutos
    # RugCheck obrigatorio para candidatos que ainda podem atingir a pontuacao minima; falha fecha a analise.
    "usar_rugcheck": True,
    "rugcheck_rejeitar_perigo": True,   # rejeita tokens com riscos de nivel "danger"
    "rugcheck_score_maximo": 60,        # rejeita se o score normalizado de risco for superior (0 = seguro, 100 = mau)

    # ---------------- Executor (Agente 3) ----------------
    "tamanho_posicao_pct": 12,       # % do capital total usada em cada compra
    "posicao_minima_usd": 1.0,       # nao abre posicoes mais pequenas do que isto
    "max_posicoes_abertas": 1,       # maximo operacional Paper, tambem validado no Executor
    "exposicao_maxima_pct": 55,      # % maxima do capital que pode estar investida
    "slippage_pct": 2.0,             # slippage simulado em cada compra E em cada venda
    "taxa_pct": 1.0,                 # taxa simulada em cada compra E em cada venda
    "simular_impacto_preco": True,   # slippage extra consoante o tamanho da ordem face a liquidez
    "parar_compras_se_perda_pct": 40,  # deixa de abrir posicoes se o capital cair X% (0 = desligado)

    # ---------------- Risco (Agente 4) ----------------
    "intervalo_risco_seg": 10,       # de quantos em quantos segundos atualiza os precos
    "intervalo_risco_com_posicao_seg": 5,  # com uma posicao aberta, le a DexScreener mais vezes (liquidez e rug)
    "preco_rapido": True,            # com uma posicao aberta, le o preco de VENDA real no Jupiter (so leitura)
    "intervalo_preco_rapido_seg": 3, # de quantos em quantos segundos (se o Jupiter limitar, o bot abranda sozinho)
    "preco_rapido_validade_seg": 12, # um preco do Jupiter mais antigo do que isto deixa de ser usado
    "preco_rapido_desvio_max_pct": 35,  # 1.o preco do Jupiter so e aceite se estiver a menos de X% da DexScreener
    "verificar_rota_compra_jupiter": True,  # antes de propor: confirma no Jupiter que o token se compra mesmo
    "impacto_compra_jupiter_max_pct": 1.5,  # ...e bloqueia a proposta se o impacto REAL for maior do que isto
    # Estas regras sao uma HIPOTESE de partida ("cortar as perdas, deixar correr os ganhos"), nao uma formula
    # provada. Depois de uns dias a correr, usa  python bot.py --analisar  para ver quais dao mais com dados reais.
    "stop_loss_pct": -12.0,          # vende se o preco de mercado cair X% face ao preco de entrada
    "take_profit_pct": 100.0,        # vende tudo se o preco subir X% (0 = sem alvo fixo, sai so pelo trailing)
    "trailing_ativar_pct": 20.0,     # o trailing stop liga-se depois de um ganho de X% (0 = desligado)
    "trailing_distancia_pct": 15.0,  # ...e vende se o preco cair X% desde o maximo atingido
    "tempo_maximo_min": 120,         # fecha a posicao ao fim de X minutos, aconteca o que acontecer
    "rug_queda_liquidez_pct": 50,    # fecha se a liquidez cair X% face a liquidez na entrada (rug)
    "rug_liquidez_minima_usd": 3000,  # fecha se a liquidez ficar abaixo deste valor (rug)
    "rug_queda_preco_pct": 50,       # fecha se o preco cair X% entre duas verificacoes seguidas
    "falhas_sem_dados_max": 6,       # se o par desaparecer X vezes seguidas, assume perda total
    "pausa_maxima_min": 5,           # se o bot esteve parado mais do que isto, anula as posicoes e percursos
                                     # interrompidos (em vez de os fechar com um preco de horas depois)

    # ---------------- Laboratorio (descobrir as melhores regras com dados reais) ----------------
    "observacao_extra": True,        # so leitura: holders, permissoes do mint e cotacao Jupiter dos aprovados (nao decide nada)
    "gravar_percursos": True,        # grava o percurso do preco de cada sinal (comprado ou nao)
    "percurso_minutos": 180,         # durante quantos minutos segue cada sinal
    "percursos_max_ativos": 26,      # quantos sinais segue ao mesmo tempo (26 + 4 posicoes = 1 pedido)
    "percursos_controlo_max": 12,    # grupo de controlo: segue tambem tokens RECUSADOS, para ver se os filtros ajudam
    "percursos_controlo_pontuacao_min": 50,  # ...so recusados com pontuacao calculada >= X (comparaveis)
    "ficheiro_percursos": "percursos.jsonl",
    # Pontos finos (SO GRAVACAO, nao decide nada): preco de VENDA real no Jupiter dos sinais seguidos, para o
    # laboratorio poder testar stops apertados com dados ao ritmo do bot (a DexScreener so muda de ~30 em 30 s).
    "percursos_finos": True,
    "percurso_fino_minutos": 180,      # durante X minutos de cada sinal (o percurso inteiro)
    "percurso_fino_intervalo_seg": 2,  # sem posicao aberta: 1 cotacao de X em X s no total (30/min; Free = 60/min)
    "percurso_fino_intervalo_com_posicao_seg": 5,  # com posicao: 12/min, que se juntam aos 20/min do preco rapido
    "percurso_fino_min_por_sinal_seg": 10,         # o mesmo sinal nunca tem pontos finos a menos de X s
    "percurso_fino_valor_usd": 5.5,    # tamanho da venda cotada (igual ao de uma operacao Paper)
    # So para o relatorio do laboratorio ("custos reais"): taxa de rede estimada por transacao. E uma HIPOTESE
    # conservadora, ainda por medir; nao entra em nenhuma decisao nem na contabilidade Paper.
    "lab_taxa_rede_usd": 0.05,

    # ---------------- Rede ----------------
    "timeout_seg": 10,               # tempo maximo de espera por cada pedido
    "timeout_com_posicao_seg": 4,    # com posicao aberta: pedidos curtos e sem repeticoes (os stops nao esperam)
    "tentativas": 3,                 # numero de tentativas por pedido
    "espera_base_seg": 1.5,          # espera entre tentativas (duplica a cada falha)
    # intervalo minimo entre pedidos, por tipo (respeita os limites das APIs)
    "intervalo_min_perfis_seg": 1.2,   # DexScreener perfis/boosts: ~60 pedidos/min
    "intervalo_min_pares_seg": 0.25,   # DexScreener pares: ~300 pedidos/min
    "fonte_geckoterminal": True,       # descoberta extra (pools novas); tokens so desta fonte NAO geram propostas
    "intervalo_gecko_seg": 60,         # de quantos em quantos segundos consulta o GeckoTerminal
    "intervalo_min_rugcheck_seg": 1.0,

    # ---------------- Telegram (opcional) ----------------
    "telegram_resumo_min": 60,       # envia um resumo de X em X minutos (0 = desligado)
    "telegram_botoes": True,         # botoes Aprovar/Recusar nas propostas (so o chat privado configurado)
    "telegram_venda_manual": True,   # botao "Vender agora" nas mensagens de compra (venda Paper, com confirmacao)

    # ---------------- Plataforma web (sinais em direto) ----------------
    "painel_web": True,              # liga a pagina web com os sinais
    "porta_web": 8000,               # abre http://localhost:8000 no browser
    "painel_web_rede": False,        # Dashboard exclusivamente local; servidor ligado a 127.0.0.1
    "max_sinais": 60,                # quantos sinais ficam guardados/visiveis

    # ---------------- Ecra ----------------
    "limpar_ecra": True,             # limpa o ecra a cada atualizacao do painel
    "acoes_no_painel": 10,           # quantas das ultimas acoes aparecem no painel
}

DEX = "https://api.dexscreener.com"
URL_PERFIS = DEX + "/token-profiles/latest/v1"
URL_BOOSTS = DEX + "/token-boosts/latest/v1"
URL_PARES_V1 = DEX + "/tokens/v1/solana/{}"
URL_PARES_LEGACY = DEX + "/latest/dex/tokens/{}"
# GeckoTerminal (gratis, sem chave, ~30 pedidos/min): fonte EXTRA de descoberta, em modo observacao
URL_GECKO_NOVAS_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1"
MINTS_BASE_IGNORADOS = {"So11111111111111111111111111111111111111112",  # SOL
                        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}  # USDT
URL_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
MAX_ENDERECOS_POR_PEDIDO = 30

log = logging.getLogger("bot")


# ============================================================================
# Funcoes auxiliares
# ============================================================================
def agora():
    """Hora atual em segundos (funcao separada para poder ser simulada nos testes)."""
    return time.time()


def dormir(segundos):
    """Pausa (funcao separada para poder ser simulada nos testes)."""
    if segundos > 0:
        time.sleep(segundos)


def num(valor, defeito=0.0):
    """Converte qualquer coisa para numero sem nunca rebentar."""
    try:
        if valor is None or isinstance(valor, bool):
            return defeito
        n = float(valor)
        if math.isnan(n) or math.isinf(n):
            return defeito
        return n
    except (TypeError, ValueError):
        return defeito


def obter(dados, *chaves, defeito=None):
    """Le um valor dentro de dicionarios aninhados de forma segura."""
    atual = dados
    for chave in chaves:
        if not isinstance(atual, dict):
            return defeito
        atual = atual.get(chave)
    return defeito if atual is None else atual


def escala(x, minimo, maximo):
    """Devolve 0..1 consoante a posicao de x entre minimo e maximo."""
    if maximo <= minimo:
        return 0.0
    return max(0.0, min(1.0, (x - minimo) / (maximo - minimo)))


def hora_txt(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else agora()).strftime("%H:%M:%S")


def data_txt(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else agora()).strftime("%Y-%m-%d %H:%M:%S")


def preco_txt(p):
    """Formata precos muito pequenos de forma legivel."""
    p = num(p)
    if p == 0:
        return "0"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.10f}".rstrip("0")


def simbolo_limpo(par):
    """Simbolo do token so com caracteres visiveis (o nome e escolhido por quem cria o token)."""
    bruto = str(obter(par, "baseToken", "symbol", defeito="?"))
    limpo = "".join(c for c in bruto if c.isprintable() and not c.isspace())[:12]
    return limpo or "?"


# ============================================================================
# Cliente HTTP: timeouts, tentativas com espera crescente e limite de pedidos
# ============================================================================
class ClienteHTTP:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ultimo_pedido = {}  # grupo -> instante do ultimo pedido
        self.intervalos = {
            "perfis": cfg["intervalo_min_perfis_seg"],
            "pares": cfg["intervalo_min_pares_seg"],
            "gecko": 2.5,
            "rugcheck": cfg["intervalo_min_rugcheck_seg"],
        }
        self.cabecalhos = {"Accept": "application/json", "User-Agent": "paper-bot/1.0"}
        # Com posicao aberta o Bot liga isto: 1 tentativa e timeout curto, para o scanner nunca atrasar os stops.
        self.urgente = False
        self.pausa_ate = {}  # grupo -> instante (monotonic) ate ao qual nao se repete o pedido depois de um 429

    def _respeitar_limite(self, grupo):
        intervalo = self.intervalos.get(grupo, 0.5)
        ultimo = self.ultimo_pedido.get(grupo)
        if ultimo is not None:
            falta = intervalo - (time.monotonic() - ultimo)
            if falta > 0:
                dormir(falta)
        self.ultimo_pedido[grupo] = time.monotonic()

    def get_json(self, url, grupo="pares", tentativas=None):
        """Devolve o JSON da resposta ou None se falhar. Nunca lanca excecoes."""
        tentativas = tentativas or self.cfg["tentativas"]
        espera = self.cfg["espera_base_seg"]
        timeout = self.cfg["timeout_seg"]
        urgente = bool(getattr(self, "urgente", False))
        pausas = self.__dict__.setdefault("pausa_ate", {})
        if urgente:
            if time.monotonic() < pausas.get(grupo, 0.0):
                return None  # ainda dentro da pausa pedida pelo servidor (429)
            tentativas = 1
            timeout = min(timeout, max(1.0, num(self.cfg.get("timeout_com_posicao_seg"), 4.0)))
        for tentativa in range(1, tentativas + 1):
            self._respeitar_limite(grupo)
            try:
                r = requests.get(url, timeout=timeout, headers=self.cabecalhos)
                codigo = r.status_code
                if codigo == 200:
                    try:
                        return r.json()
                    except ValueError:
                        log.warning("Resposta invalida (nao e JSON): %s", url[:90])
                        return None
                if codigo == 429:
                    # limite de pedidos atingido: espera o que o servidor pedir
                    pausa = num(obter(dict(r.headers), "Retry-After"), espera * 2)
                    pausa = max(1.0, min(pausa, 30.0))
                    log.warning("Limite de pedidos (429). A esperar %.0fs...", pausa)
                    if urgente:  # nao dorme (os stops nao esperam): salta os pedidos deste grupo durante a pausa
                        pausas[grupo] = time.monotonic() + pausa
                    if tentativa < tentativas:
                        dormir(pausa)
                elif codigo >= 500:
                    log.warning("Erro do servidor %s em %s", codigo, url[:90])
                    if tentativa < tentativas:
                        dormir(espera)
                else:
                    # 400/404 etc.: nao adianta repetir
                    log.info("Pedido recusado (%s): %s", codigo, url[:90])
                    return None
            except requests.RequestException as e:
                log.warning("Falha de rede (%s) tentativa %d/%d", type(e).__name__, tentativa, tentativas)
                if tentativa < tentativas:
                    dormir(espera)
            except Exception as e:  # qualquer coisa inesperada
                log.warning("Erro inesperado no pedido: %r", e)
                return None
            espera *= 2
        return None


# ============================================================================
# Telegram (opcional)
# ============================================================================
class Telegram:
    def __init__(self, token=None, chat=None, em_segundo_plano=False):
        self.token = str(token if token is not None else os.environ.get("TELEGRAM_TOKEN", "")).strip()
        self.chat = str(chat if chat is not None else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        self.ativo = bool(self.token and self.chat)
        self.em_segundo_plano = bool(em_segundo_plano)

    def enviar(self, texto, botoes=None, ao_enviar=None):
        """Envia uma mensagem. Se nao estiver configurado, nao faz nada.
        botoes: teclado inline do Telegram; ao_enviar(message_id, texto) e chamado se a mensagem for aceite."""
        if not self.ativo:
            return
        if self.em_segundo_plano:  # nunca atrasa o ciclo do bot (stops e propostas)
            threading.Thread(target=self._enviar, args=(texto, botoes, ao_enviar), name="telegram", daemon=True).start()
            return
        self._enviar(texto, botoes, ao_enviar)

    def _enviar(self, texto, botoes=None, ao_enviar=None):
        corpo = {"chat_id": self.chat, "text": texto[:4000]}
        if botoes:
            corpo["reply_markup"] = {"inline_keyboard": botoes}
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=corpo,
                timeout=8,
            )
            if ao_enviar is not None and getattr(r, "status_code", 0) == 200:
                mid = obter(r.json(), "result", "message_id")
                if isinstance(mid, int) and not isinstance(mid, bool):
                    ao_enviar(mid, corpo["text"])
        except Exception as e:
            log.warning("Telegram falhou: %s", type(e).__name__)


class TelegramDecisoes:
    """Botoes Aprovar/Recusar nas propostas Paper, com confirmacao ("Sim, aprovar").

    So aceita cliques do chat PRIVADO configurado (id do utilizador = id do chat); qualquer outro e ignorado.
    A decisao segue pela mesma fila do dashboard e o bot revalida tudo antes de executar em Paper.
    Le os cliques por getUpdates numa thread propria: nunca atrasa o ciclo do bot. O token nunca vai para o log."""

    ERROS_FATAIS = ("http_401", "http_404", "http_409")

    def __init__(self, token, chat, decidir, pendente, avisar=None, post=None, vender=None, posicao_aberta=None):
        self.token = str(token or "").strip()
        self.chat = str(chat or "").strip()
        self.decidir = decidir      # fn(proposal_id, "aprovar"|"recusar") -> (ok, mensagem)
        self.pendente = pendente    # fn(proposal_id) -> bool
        self.avisar = avisar        # fn(texto): aviso unico se os botoes tiverem de ser desligados
        self.vender = vender                  # fn(mint) -> (ok, mensagem): pede a venda manual ao ciclo principal
        self.posicao_aberta = posicao_aberta  # fn(mint) -> bool
        self._post = post           # so nos testes
        # so chat privado: num grupo, outros membros poderiam carregar nos botoes
        self.ativo = bool(self.token) and re.fullmatch(r"\d{3,20}", self.chat) is not None
        self.desligado_motivo = None if self.ativo else "chat de grupo ou Telegram nao configurado"
        self.offset = None
        self._mensagens = {}        # proposal_id -> (message_id, texto original)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def botoes_proposta(proposal_id):
        return [[{"text": "✅ Aprovar", "callback_data": "a:" + proposal_id},
                 {"text": "❌ Recusar", "callback_data": "r:" + proposal_id}]]

    @staticmethod
    def botoes_venda(mint):
        return [[{"text": "🔴 Vender agora", "callback_data": "v:" + mint}]]

    @staticmethod
    def botoes_confirmar_venda(mint):
        return [[{"text": "✅ Sim, vender", "callback_data": "y:" + mint},
                 {"text": "↩️ Não", "callback_data": "k:" + mint}]]

    def fechar_posicoes(self, abertas):
        """Tira o botao "Vender agora" das compras cujas posicoes ja fecharam (pelas regras ou a mao)."""
        if not self.ativo:
            return
        with self._lock:
            fechadas = [k for k in self._mensagens if k.startswith("pos:") and k[4:] not in abertas]
        for chave in fechadas:
            self.fechar(chave, "Posição fechada.")

    @staticmethod
    def botoes_confirmar(proposal_id):
        return [[{"text": "✅ Sim, aprovar", "callback_data": "s:" + proposal_id},
                 {"text": "↩️ Não", "callback_data": "n:" + proposal_id}]]

    def _api(self, metodo, corpo, timeout=10):
        post = self._post or requests.post
        try:
            r = post(f"https://api.telegram.org/bot{self.token}/{metodo}", json=corpo, timeout=timeout)
        except Exception as e:  # nunca regista a excecao inteira: o URL leva o token
            return None, f"rede_{type(e).__name__}"
        codigo = getattr(r, "status_code", 0)
        try:
            dados = r.json()
        except Exception:
            dados = None
        if codigo == 200 and isinstance(dados, dict) and dados.get("ok"):
            return dados.get("result"), None
        return None, f"http_{codigo}"

    def registar_mensagem(self, proposal_id, message_id, texto):
        with self._lock:
            self._mensagens[proposal_id] = (message_id, texto)
            while len(self._mensagens) > 50:
                self._mensagens.pop(next(iter(self._mensagens)))

    def _responder(self, callback_id, texto, alerta=False):
        if callback_id:
            self._api("answerCallbackQuery", {"callback_query_id": callback_id, "text": texto[:190],
                                              "show_alert": bool(alerta)})

    def _mudar_botoes(self, message_id, botoes):
        if isinstance(message_id, int):
            self._api("editMessageReplyMarkup", {"chat_id": self.chat, "message_id": message_id,
                                                 "reply_markup": {"inline_keyboard": botoes or []}})

    def _escrever_estado(self, proposal_id, estado, message_id=None, final=False):
        """Acrescenta o estado ao texto original da proposta e tira os botoes."""
        with self._lock:
            guardada = self._mensagens.pop(proposal_id, None) if final else self._mensagens.get(proposal_id)
        if guardada is None:
            if message_id is not None:
                self._mudar_botoes(message_id, None)
            return False
        mid, texto = guardada
        self._api("editMessageText", {"chat_id": self.chat, "message_id": mid,
                                      "text": (texto + "\n\n" + estado)[:4000]})
        return True

    def fechar(self, proposal_id, estado):
        """Chamado pelo ciclo principal quando a proposta fica resolvida (em segundo plano)."""
        if not self.ativo:
            return
        with self._lock:
            if proposal_id not in self._mensagens:
                return
        threading.Thread(target=self._escrever_estado, args=(proposal_id, estado), kwargs={"final": True},
                         name="telegram-fechar", daemon=True).start()

    def processar(self, update):
        """Trata um clique. Devolve o que aconteceu (para o registo e para os testes)."""
        cq = update.get("callback_query") if isinstance(update, dict) else None
        if not isinstance(cq, dict):
            return None
        callback_id = cq.get("id")
        de = str(obter(cq, "from", "id", defeito=""))
        chat = str(obter(cq, "message", "chat", "id", defeito=""))
        message_id = obter(cq, "message", "message_id")
        if de != self.chat or chat != self.chat:
            self._responder(callback_id, "Não autorizado.")
            log.warning("Telegram: clique ignorado (nao veio do chat privado configurado)")
            return "nao_autorizado"
        dados = str(cq.get("data") or "")
        mv = re.fullmatch(r"([vyk]):([A-Za-z0-9]{30,50})", dados)  # o mesmo formato que MINT_VALIDO
        if mv:
            return self._processar_venda(mv.group(1), mv.group(2), callback_id, message_id)
        m = re.fullmatch(r"([arsn]):([0-9a-f]{32})", dados)
        if not m:
            self._responder(callback_id, "Botão inválido.")
            return "invalido"
        acao, proposal_id = m.groups()
        if not self.pendente(proposal_id):
            self._responder(callback_id, "Esta proposta já foi decidida ou expirou.", alerta=True)
            self._mudar_botoes(message_id, None)
            return "fechada"
        if acao == "a":
            self._mudar_botoes(message_id, self.botoes_confirmar(proposal_id))
            self._responder(callback_id, "Confirma a compra Paper?")
            return "confirmar"
        if acao == "n":
            self._mudar_botoes(message_id, self.botoes_proposta(proposal_id))
            self._responder(callback_id, "Cancelado: a proposta continua pendente.")
            return "voltou"
        decisao = "aprovar" if acao == "s" else "recusar"
        try:
            ok, mensagem = self.decidir(proposal_id, decisao)
        except Exception:
            log.exception("Falha ao decidir proposta pelo Telegram; nada foi executado")
            ok, mensagem = False, "erro interno; nada foi executado"
        if not ok:
            self._responder(callback_id, f"Não foi possível: {mensagem}", alerta=True)
            self._mudar_botoes(message_id, None)
            return "recusado_pelo_bot"
        log.info("Proposta PAPER %s %s no Telegram", proposal_id[:12], "aprovada" if decisao == "aprovar" else "recusada")
        self._responder(callback_id, "Aprovada: o bot vai revalidar e comprar em Paper." if decisao == "aprovar"
                        else "Recusada.")
        self._escrever_estado(proposal_id, "⏳ Aprovada no Telegram; a revalidar…" if decisao == "aprovar"
                              else "❌ Recusada no Telegram.", message_id=message_id)
        return decisao

    def _processar_venda(self, acao, mint, callback_id, message_id):
        aberta = callable(self.posicao_aberta) and callable(self.vender) and self.posicao_aberta(mint)
        if not aberta:
            self._responder(callback_id, "Esta posição já está fechada.", alerta=True)
            self._mudar_botoes(message_id, None)
            return "venda_fechada"
        if acao == "v":
            self._mudar_botoes(message_id, self.botoes_confirmar_venda(mint))
            self._responder(callback_id, "Confirma a venda Paper agora?")
            return "confirmar_venda"
        if acao == "k":
            self._mudar_botoes(message_id, self.botoes_venda(mint))
            self._responder(callback_id, "Cancelado: a posição continua aberta.")
            return "venda_cancelada"
        try:
            ok, mensagem = self.vender(mint)
        except Exception:
            log.exception("Falha ao pedir venda manual pelo Telegram; nada foi vendido")
            ok, mensagem = False, "erro interno; nada foi vendido"
        if not ok:
            self._responder(callback_id, f"Não foi possível: {mensagem}", alerta=True)
            self._mudar_botoes(message_id, self.botoes_venda(mint))
            return "venda_recusada"
        log.info("Venda manual PAPER pedida no Telegram para %s", mint)
        self._responder(callback_id, "A vender em Paper; recebes a mensagem de venda em segundos.")
        self._mudar_botoes(message_id, None)
        return "vender"

    def _desligar(self, erro):
        self.ativo = False
        self.desligado_motivo = erro
        log.warning("Botoes do Telegram desligados (%s); as propostas continuam no dashboard", erro)
        if callable(self.avisar):
            try:
                self.avisar("⚠️ [PAPER] Os botões Aprovar/Recusar no Telegram foram desligados ("
                            + ("outro programa está a ler este bot do Telegram" if erro == "http_409" else erro)
                            + "). As propostas continuam no dashboard privado.")
            except Exception:
                pass

    def ciclo(self, timeout=25):
        """Um pedido getUpdates. Devolve o numero de cliques tratados, ou o erro."""
        corpo = {"timeout": timeout, "allowed_updates": ["callback_query"]}
        if self.offset is not None:
            corpo["offset"] = self.offset
        resultado, erro = self._api("getUpdates", corpo, timeout=timeout + 10)
        if erro:
            if erro in self.ERROS_FATAIS:
                self._desligar(erro)
            return erro
        tratados = 0
        for update in resultado if isinstance(resultado, list) else []:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(update_id, int):
                self.offset = max(self.offset or 0, update_id + 1)
            try:
                if self.processar(update) is not None:
                    tratados += 1
            except Exception:
                log.exception("Erro a tratar um clique do Telegram")
        return tratados

    def descartar_antigos(self):
        """Ao arrancar, ignora cliques feitos enquanto o bot estava parado."""
        resultado, erro = self._api("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["callback_query"]})
        if erro:
            if erro in self.ERROS_FATAIS:
                self._desligar(erro)
            return False
        ids = [u.get("update_id") for u in resultado or [] if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
        if ids:
            self.offset = max(ids) + 1
        return True

    def _correr(self):
        while not self._stop.is_set() and self.ativo and not self.descartar_antigos():
            self._stop.wait(5)
        while not self._stop.is_set() and self.ativo:
            r = self.ciclo()
            if isinstance(r, str):
                self._stop.wait(5)

    def iniciar(self):
        if not self.ativo or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._correr, name="telegram-botoes", daemon=True)
        self._thread.start()

    def parar(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Textos das mensagens do Telegram (so texto: nao decidem nem alteram nada)
# ---------------------------------------------------------------------------
def _link_dexscreener(mint):
    return f"https://dexscreener.com/solana/{mint}" if isinstance(mint, str) and MINT_VALIDO.fullmatch(mint) else ""


def motivo_legivel(motivo):
    """Traduz o motivo tecnico de uma venda (ex.: 'STOP_LOSS -17.0%') para uma frase curta."""
    m = str(motivo or "").strip()
    x = re.match(r"STOP_LOSS ([+-][\d.]+)%", m)
    if x:
        return f"stop-loss (preço a {x.group(1)}%)"
    x = re.match(r"TAKE_PROFIT ([+-][\d.]+)%", m)
    if x:
        return f"alvo atingido (preço a {x.group(1)}%)"
    x = re.match(r"TRAILING ([+-][\d.]+)% \(pico ([+-][\d.]+)%\)", m)
    if x:
        return f"trailing stop (saiu a {x.group(1)}%; pico {x.group(2)}%)"
    x = re.match(r"TEMPO (\d+)min \(([+-][\d.]+)%\)", m)
    if x:
        return f"tempo máximo ({x.group(1)} min; preço a {x.group(2)}%)"
    if m.startswith("RUG"):
        return "possível rug: " + m[3:].strip().replace("preco", "preço")
    if m.startswith("MANUAL"):
        return "venda manual (decisão tua no Telegram)"
    return m or "—"


def texto_proposta(cfg, simbolo, mint, pontuacao, valor_usd, par, segundos=90, com_botoes=False):
    par = par if isinstance(par, dict) else {}
    minimo = cfg.get("pontuacao_minima")
    L = [f"🔔 [PAPER] NOVA PROPOSTA — ${simbolo}",
         f"Pontuação: {pontuacao}/100" + (f" (mínimo {num(minimo):.0f})" if minimo is not None else ""),
         f"Valor a simular: ${num(valor_usd):.2f}", ""]
    liq, mcap = num(obter(par, "liquidity", "usd")), num(par.get("marketCap")) or num(par.get("fdv"))
    partes = ([f"Liquidez: ${liq:,.0f}"] if liq > 0 else []) + ([f"Market cap: ${mcap:,.0f}"] if mcap > 0 else [])
    if partes:
        L.append(" · ".join(partes))
    criado = num(par.get("pairCreatedAt"))
    compras, vendas = num(obter(par, "txns", "h1", "buys")), num(obter(par, "txns", "h1", "sells"))
    partes = []
    if criado > 0:
        minutos = max(0.0, (agora() - criado / 1000.0) / 60.0)
        partes.append("Idade: " + (f"{minutos:.0f} min" if minutos < 120 else f"{minutos / 60:.1f} h"))
    if compras + vendas > 0:
        partes.append(f"Compras na última hora: {compras / (compras + vendas) * 100:.0f}%")
    if partes:
        L.append(" · ".join(partes))
    partes = [f"{num(v):+.0f}% em {nome}" for v, nome in ((obter(par, "priceChange", "m5"), "5 min"),
                                                         (obter(par, "priceChange", "h1"), "1 h")) if v is not None]
    if partes:
        L.append("Preço: " + " · ".join(partes))
    L += ["", f"⏳ Expira em {segundos} segundos.",
          "Aprova ou recusa nos botões em baixo, ou no dashboard privado." if com_botoes
          else "Aprova ou recusa no dashboard privado."]
    link = _link_dexscreener(mint)
    return "\n".join(L + ([link] if link else []))


def texto_compra(cfg, simbolo, mint, valor_usd, preco_entrada, pontuacao, liquidez, com_botao=False):
    saidas = []
    if cfg.get("stop_loss_pct") is not None:
        saidas.append(f"stop {num(cfg['stop_loss_pct']):.0f}%")
        saidas.append(f"alvo +{num(cfg['take_profit_pct']):.0f}%" if cfg.get("take_profit_pct") else "sem alvo fixo")
        saidas.append(f"trailing {num(cfg['trailing_ativar_pct']):.0f}/{num(cfg.get('trailing_distancia_pct')):.0f}"
                      if cfg.get("trailing_ativar_pct") else "sem trailing")
        if cfg.get("tempo_maximo_min"):
            saidas.append(f"máximo {num(cfg['tempo_maximo_min']):.0f} min")
    L = [f"🟢 [PAPER] COMPRA — ${simbolo}",
         f"Valor: ${num(valor_usd):.2f} · Pontuação: {pontuacao}/100",
         f"Preço de entrada: ${preco_txt(preco_entrada)}"]
    if num(liquidez) > 0:
        L.append(f"Liquidez: ${num(liquidez):,.0f}")
    if saidas:
        L.append("Saídas: " + " · ".join(saidas))
    if com_botao:
        L.append("Para vender antes, usa o botão em baixo.")
    link = _link_dexscreener(mint)
    return "\n".join(L + ([link] if link else []))


def texto_venda(simbolo, motivo, pnl, pnl_pct, duracao_min, carteira):
    icone = "⚠️" if str(motivo or "").startswith("RUG") else "✅" if pnl > 0 else "🔴"
    duracao = "menos de 1 min" if duracao_min < 1 else f"{duracao_min:.0f} min"
    return "\n".join([f"{icone} [PAPER] VENDA — ${simbolo}",
                      f"Resultado: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f} ({pnl_pct:+.1f}%)",
                      f"Motivo: {motivo_legivel(motivo)}",
                      f"Duração: {duracao}",
                      f"Carteira: ${num(carteira):.2f}"])


# ============================================================================
# Leitura dos pares da DexScreener (partilhado pelo Scanner e pelo Risco)
# ============================================================================
def extrair_pares(dados):
    """Aceita o formato novo (lista) e o antigo ({"pairs": [...]})."""
    if isinstance(dados, list):
        pares = dados
    elif isinstance(dados, dict):
        pares = dados.get("pairs") or []
    else:
        pares = []
    return [p for p in pares if isinstance(p, dict)] if isinstance(pares, list) else []


def buscar_pares(http, enderecos):
    """
    Vai buscar os pares de varios tokens (em grupos de 30).
    Devolve {mint: [pares]} ou None se TODOS os pedidos falharem.
    """
    enderecos = [e for e in dict.fromkeys(enderecos) if isinstance(e, str) and e]
    if not enderecos:
        return {}
    resultado = {e: [] for e in enderecos}
    algum_ok = False
    for i in range(0, len(enderecos), MAX_ENDERECOS_POR_PEDIDO):
        grupo = enderecos[i:i + MAX_ENDERECOS_POR_PEDIDO]
        lista = ",".join(grupo)
        dados = http.get_json(URL_PARES_V1.format(lista), "pares")
        if dados is None:
            # tenta o formato antigo da API
            dados = http.get_json(URL_PARES_LEGACY.format(lista), "pares")
        if dados is None:
            for e in grupo:
                resultado[e] = None  # None = sem resposta (diferente de [] = o token nao tem par)
            continue
        algum_ok = True
        for par in extrair_pares(dados):
            if str(par.get("chainId", "solana")).lower() != "solana":
                continue
            mint = obter(par, "baseToken", "address")
            if mint in resultado:  # so interessa quando o nosso token e o token base
                resultado[mint].append(par)
    return resultado if algum_ok else None


def melhor_par(pares):
    """Escolhe o par com mais liquidez."""
    validos = [p for p in pares if num(p.get("priceUsd")) > 0]
    if not validos:
        return None
    return max(validos, key=lambda p: num(obter(p, "liquidity", "usd")))


# ============================================================================
# AGENTE 1 - SCANNER
# ============================================================================
class Scanner:
    """Procura tokens novos de Solana nos perfis e boosts mais recentes."""

    def __init__(self, cfg, http):
        self.cfg = cfg
        self.http = http
        self.respondeu = True
        self.ultimos_mints = []
        self.ultimos_pares = {}
        self.last_received_count = 0
        self.last_received_unique_mints = []
        self.last_new_mints = []
        self.last_reanalysis_mints = []
        self.last_pair_errors = []
        # GeckoTerminal: inativo por defeito; main() ativa-o no modo Paper com mercado real.
        self.gecko_ativo = False
        self.origem_gecko = set()      # mints encontrados so pelo GeckoTerminal (modo observacao)
        self.gecko_ultimo = None
        self.last_gecko_count = 0
        # O Bot poe aqui uma funcao que le o preco rapido da posicao aberta entre os pedidos do scanner.
        self.entre_pedidos = None

    def _ceder_a_vez(self):
        funcao = getattr(self, "entre_pedidos", None)
        if callable(funcao):
            try:
                funcao()
            except Exception:
                log.exception("Erro ao ceder a vez ao preco rapido")

    def tokens_gecko(self):
        """Pools novas de Solana no GeckoTerminal com liquidez minima. Devolve lista de mints."""
        if not self.gecko_ativo or not self.cfg.get("fonte_geckoterminal"):
            return []
        if (self.gecko_ultimo is not None
                and time.monotonic() - self.gecko_ultimo < max(30, num(self.cfg.get("intervalo_gecko_seg"), 60))):
            return []
        self.gecko_ultimo = time.monotonic()
        dados = self.http.get_json(URL_GECKO_NOVAS_POOLS, "gecko", tentativas=1)
        lista = dados.get("data") if isinstance(dados, dict) else None
        if not isinstance(lista, list):
            self.last_gecko_count = 0
            return []
        minimo = num(self.cfg.get("liquidez_minima_usd"), 0)
        mints = []
        for pool in lista[:40]:
            if not isinstance(pool, dict):
                continue
            liq = num(obter(pool, "attributes", "reserve_in_usd"))
            ident = obter(pool, "relationships", "base_token", "data", "id")
            if liq < minimo or not isinstance(ident, str) or not ident.startswith("solana_"):
                continue
            mint = ident[len("solana_"):]
            if MINT_VALIDO.fullmatch(mint) and mint not in mints and mint not in MINTS_BASE_IGNORADOS:
                mints.append(mint)
        self.last_gecko_count = len(mints)
        return mints

    def tokens_recentes(self):
        """Devolve a lista de enderecos (mints) de Solana encontrados agora."""
        mints = []
        recebidos = 0
        self.respondeu = False
        for url in (URL_PERFIS, URL_BOOSTS):
            self._ceder_a_vez()
            dados = self.http.get_json(url, "perfis")
            self.respondeu = self.respondeu or dados is not None
            if isinstance(dados, dict):  # por seguranca, caso venha embrulhado
                dados = dados.get("data") or dados.get("tokens") or []
            if not isinstance(dados, list):
                continue
            for item in dados:
                if not isinstance(item, dict):
                    continue
                if str(item.get("chainId", "")).lower() != "solana":
                    continue
                mint = item.get("tokenAddress")
                if isinstance(mint, str) and 30 <= len(mint) <= 50:
                    recebidos += 1
                    if mint not in mints:
                        mints.append(mint)
        # Fonte extra (observacao): so acrescenta mints que o DexScreener nao trouxe.
        for mint in mints:
            self.origem_gecko.discard(mint)
        self._ceder_a_vez()
        try:
            extra = self.tokens_gecko()
        except Exception:
            log.exception("GeckoTerminal ignorado neste ciclo")
            extra = []
        for mint in extra:
            if mint not in mints:
                recebidos += 1
                mints.append(mint)
                self.origem_gecko.add(mint)
        if len(self.origem_gecko) > 5000:
            self.origem_gecko = set(list(self.origem_gecko)[-2000:])
        self.last_received_count = recebidos
        self.last_received_unique_mints = list(mints)
        return mints

    def procurar(self, vistos, pendentes, ignorar=(), observar=()):
        """
        Devolve (candidatos, sem_par):
          candidatos = lista de (mint, melhor_par) prontos para o Analista
          sem_par    = mints novos que ainda nao tem par negociavel
        Devolve (None, None) se a API de pares estiver em baixo.
        """
        ignorar = set(ignorar)
        recentes = self.tokens_recentes()
        pendentes_set = set(pendentes)
        novos = [m for m in recentes if m not in vistos and m not in ignorar and m not in pendentes_set]
        reanalisar = [m for m in pendentes if m not in ignorar]
        self.last_new_mints = list(dict.fromkeys(novos))
        self.last_reanalysis_mints = list(dict.fromkeys(reanalisar))
        self.last_pair_errors = []
        analisar = list(dict.fromkeys(novos + reanalisar))
        a_ver = list(dict.fromkeys(analisar + [m for m in observar if m and m not in ignorar]))
        self.ultimos_mints = a_ver
        self.ultimos_pares = {}
        if not a_ver:
            return [], []
        self._ceder_a_vez()
        pares = buscar_pares(self.http, a_ver)
        if pares is None:
            self.ultimos_pares = {mint: None for mint in a_ver}
            return None, None
        self.ultimos_pares = pares
        candidatos, sem_par = [], []
        # Observation-only mints share this batch request but are never re-analysed.
        for mint in analisar:
            lista = pares.get(mint)
            if lista is None:  # o pedido falhou para este token: tenta outra vez no proximo ciclo
                self.last_pair_errors.append(mint)
                continue
            par = melhor_par(lista)
            if par is None:
                sem_par.append(mint)
            else:
                candidatos.append((mint, par))
        return candidatos, sem_par


# ============================================================================
# AGENTE 2 - ANALISTA
# ============================================================================
class Analista:
    """Da uma pontuacao de 0 a 100 a cada token e explica porque."""

    CRITERIOS = (
        ("preco", "Preço"), ("idade_par", "Idade do par"),
        ("liquidez", "Liquidez"), ("market_cap", "Market cap/FDV"),
        ("volume_h1", "Volume 1h"), ("transacoes_h1", "Transações 1h"),
        ("variacao_h1", "Variação 1h"), ("variacao_m5_max", "Subida m5 máxima"),
        ("variacao_m5_min", "Queda m5 máxima"), ("dados_score", "Dados da pontuação"),
        ("rugcheck", "RugCheck"), ("pontuacao_minima", "Pontuação mínima"),
    )

    def __init__(self, cfg, http):
        self.cfg = cfg
        self.http = http

    @staticmethod
    def _numero_presente(valor):
        if valor is None or isinstance(valor, bool) or valor == "":
            return None, "missing" if valor is None else "invalid"
        try:
            resultado = float(valor)
        except (TypeError, ValueError, OverflowError):
            return None, "invalid"
        if not math.isfinite(resultado):
            return None, "invalid"
        return resultado, "valid"

    @staticmethod
    def _resultado(status, motivo, *, aprovado=None, pontuacao=None, reavaliar=False,
                   criterios=None, bloqueio=None, campos_ausentes=None, campos_invalidos=None,
                   detalhes=None, pontuacao_parcial=None, erro=None):
        legacy = {
            "approved": "aprovada", "rejected": "rejeitada", "pending_data": "dados_indisponiveis",
            "technical_error": "erro_tecnico", "no_pair": "sem_par", "historical_approval": "aprovada_anteriormente",
        }
        return {
            "status": status, "classification": status, "estado_analise": legacy.get(status, status),
            "aprovado": aprovado, "pontuacao": pontuacao,
            "score_calculated": pontuacao is not None,
            "motivo": motivo, "resultado_analise": motivo, "reavaliar": bool(reavaliar),
            "detalhes": detalhes or {}, "criteria": criterios or [], "blocking_criterion": bloqueio,
            "missing_fields": campos_ausentes or [], "invalid_fields": campos_invalidos or [],
            "partial_score": pontuacao_parcial, "technical_error": erro,
        }

    @classmethod
    def _preencher_nao_avaliados(cls, criterios, motivo):
        incluidos = {item["id"] for item in criterios}
        for identificador, rotulo in cls.CRITERIOS:
            if identificador not in incluidos:
                criterios.append({"id": identificador, "name": rotulo, "observed": None,
                                  "threshold": None, "status": "not_evaluated",
                                  "reason": motivo or "Critério posterior ao primeiro bloqueio."})

    @staticmethod
    def _adicionar_criterio(criterios, identificador, nome, observado, limiar,
                            estado="passed", reason=None):
        criterios.append({"id": identificador, "name": nome, "observed": observado,
                          "threshold": limiar, "status": estado, "reason": reason})

    @classmethod
    def _bloquear(cls, criterios, identificador, nome, observado, limiar, motivo,
                  status="rejected", reavaliar=False, campos_ausentes=None, campos_invalidos=None,
                  pontuacao_parcial=None):
        estado_criterio = "pending_data" if status == "pending_data" else "rejected"
        cls._adicionar_criterio(criterios, identificador, nome, observado, limiar,
                               estado_criterio, motivo)
        bloqueio = {"id": identificador, "name": nome, "observed": observado,
                    "threshold": limiar, "status": estado_criterio}
        cls._preencher_nao_avaliados(criterios, "Não avaliado após o primeiro critério bloqueador.")
        return cls._resultado(status, motivo, aprovado=False if status == "rejected" else None,
                              reavaliar=reavaliar, criterios=criterios, bloqueio=bloqueio,
                              campos_ausentes=campos_ausentes, campos_invalidos=campos_invalidos,
                              pontuacao_parcial=pontuacao_parcial)

    def rugcheck(self, mint):
        """Consulta o RugCheck. Devolve None se nao estiver disponivel."""
        if not self.cfg["usar_rugcheck"]:
            return None
        try:
            dados = self.http.get_json(URL_RUGCHECK.format(mint), "rugcheck", tentativas=2)
        except Exception as exc:
            log.warning("RugCheck indisponivel (%s); analise permanece pendente", type(exc).__name__)
            return None
        if not isinstance(dados, dict) or not isinstance(dados.get("risks"), list):
            return None
        riscos = dados["risks"]
        if any(not isinstance(r, dict) or not isinstance(r.get("level"), str) for r in riscos):
            return None
        perigos = [str(r.get("name", "?")) for r in riscos
                   if str(r.get("level", "")).lower() == "danger"]
        score = dados.get("score_normalised")
        if score is None:
            score = dados.get("score")
            if score is not None:
                # score bruto: quanto maior, pior. Aproximacao para 0-100.
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    return None
                try:
                    raw_score = float(score)
                except (TypeError, ValueError, OverflowError):
                    return None
                if not math.isfinite(raw_score):
                    return None
                score = min(100.0, raw_score / 100.0)
        elif isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        try:
            score = float(score)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            return None
        return {"score": score, "perigos": perigos, "n_riscos": len(riscos)}

    def analisar(self, mint, par):
        cfg = self.cfg
        criterios = []
        try:
            if not isinstance(par, dict):
                return self._resultado("no_pair", "Não foi encontrado um par utilizável.",
                                       criterios=self._criterios_indisponiveis("Par inexistente."),
                                       campos_ausentes=["pair"] , reavaliar=True)
            return self._analisar(mint, par, cfg, criterios)
        except Exception as e:  # dados estranhos nunca podem parar o bot
            log.exception("Erro a analisar %s", mint)
            self._adicionar_criterio(criterios, "erro_tecnico", "Erro técnico", type(e).__name__, None,
                                     "technical_error", str(e))
            self._preencher_nao_avaliados(criterios, "Não avaliado devido a erro técnico.")
            return self._resultado("technical_error", f"Erro técnico durante a análise ({type(e).__name__}).",
                                   criterios=criterios, erro={"type": type(e).__name__, "message": str(e)},
                                   reavaliar=True)

    @classmethod
    def _criterios_indisponiveis(cls, reason):
        return [{"id": identifier, "name": name, "observed": None, "threshold": None,
                 "status": "not_evaluated", "reason": reason}
                for identifier, name in cls.CRITERIOS]

    def _analisar(self, mint, par, cfg, criterios):
        def ler(caminho):
            raw = obter(par, *caminho, defeito=None)
            return (*self._numero_presente(raw), raw)

        preco, estado_preco, _ = ler(("priceUsd",))
        criado, estado_criado, _ = ler(("pairCreatedAt",))
        liq, estado_liq, _ = ler(("liquidity", "usd"))
        mcap, estado_mcap, _ = ler(("marketCap",))
        fdv_alt, estado_fdv, _ = ler(("fdv",))
        fdv = mcap if estado_mcap == "valid" else fdv_alt if estado_fdv == "valid" else None
        fdv_source = "marketCap" if estado_mcap == "valid" else "fdv"
        vol_m5, estado_vol_m5, _ = ler(("volume", "m5"))
        vol_h1, estado_vol_h1, _ = ler(("volume", "h1"))
        c_m5, estado_c_m5, _ = ler(("txns", "m5", "buys"))
        v_m5, estado_v_m5, _ = ler(("txns", "m5", "sells"))
        c_h1, estado_c_h1, _ = ler(("txns", "h1", "buys"))
        v_h1, estado_v_h1, _ = ler(("txns", "h1", "sells"))
        var_m5, estado_var_m5, _ = ler(("priceChange", "m5"))
        var_h1, estado_var_h1, _ = ler(("priceChange", "h1"))

        def pending(identificador, nome, estado, campos, reavaliar=True, limiar=None):
            ausentes = [campo for campo, situacao in campos if situacao == "missing"]
            invalidos = [campo for campo, situacao in campos if situacao == "invalid"]
            texto = (f"{nome} indisponível; análise incompleta." if estado == "missing" else
                     f"{nome} inválido; análise incompleta.")
            return self._bloquear(criterios, identificador, nome, None, limiar, texto,
                                  status="pending_data", reavaliar=reavaliar,
                                  campos_ausentes=ausentes, campos_invalidos=invalidos)

        # ---------- Filtros eliminatorios ----------
        if estado_preco != "valid":
            return pending("preco", "Preço", estado_preco, [("priceUsd", estado_preco)], limiar="> 0")
        if preco <= 0:
            return self._bloquear(criterios, "preco", "Preço", preco, "> 0", "Preço igual a zero.")
        self._adicionar_criterio(criterios, "preco", "Preço", preco, "> 0")
        if estado_criado != "valid" or criado <= 0:
            estado = estado_criado if estado_criado != "valid" else "invalid"
            return pending("idade_par", "Idade do par", estado, [("pairCreatedAt", estado)], limiar={"min": cfg["idade_minima_min"], "max": cfg["idade_maxima_horas"] * 60})
        idade_min = (agora() - criado / 1000.0) / 60.0
        if idade_min < cfg["idade_minima_min"]:
            return self._bloquear(criterios, "idade_par", "Idade do par", idade_min,
                                  {"min": cfg["idade_minima_min"], "max": cfg["idade_maxima_horas"] * 60},
                                  f"Demasiado novo ({idade_min:.0f} min).", reavaliar=True)
        if idade_min > cfg["idade_maxima_horas"] * 60:
            return self._bloquear(criterios, "idade_par", "Idade do par", idade_min,
                                  {"min": cfg["idade_minima_min"], "max": cfg["idade_maxima_horas"] * 60},
                                  f"Demasiado antigo ({idade_min / 60:.1f} h).")
        self._adicionar_criterio(criterios, "idade_par", "Idade do par", idade_min,
                                 {"min": cfg["idade_minima_min"], "max": cfg["idade_maxima_horas"] * 60})
        if estado_liq != "valid":
            return pending("liquidez", "Liquidez", estado_liq, [("liquidity.usd", estado_liq)], limiar=cfg["liquidez_minima_usd"])
        if liq < cfg["liquidez_minima_usd"]:
            return self._bloquear(criterios, "liquidez", "Liquidez", liq, cfg["liquidez_minima_usd"],
                                  f"Liquidez baixa (${liq:,.0f}).",
                                  reavaliar=liq >= cfg["liquidez_minima_usd"] * 0.6)
        self._adicionar_criterio(criterios, "liquidez", "Liquidez", liq, cfg["liquidez_minima_usd"])
        if fdv is None:
            estados = [("marketCap", estado_mcap), ("fdv", estado_fdv)]
            ausentes = [n for n, s in estados if s == "missing"]
            invalidos = [n for n, s in estados if s == "invalid"]
            return self._bloquear(criterios, "market_cap", "Market cap/FDV", None, {"min": cfg["fdv_minimo_usd"], "max": cfg["fdv_maximo_usd"]},
                                  "Market cap/FDV indisponível; análise incompleta.",
                                  status="pending_data", reavaliar=True,
                                  campos_ausentes=ausentes, campos_invalidos=invalidos)
        if fdv < cfg["fdv_minimo_usd"]:
            return self._bloquear(criterios, "market_cap", "Market cap/FDV", fdv,
                                  {"min": cfg["fdv_minimo_usd"], "max": cfg["fdv_maximo_usd"]},
                                  f"Market cap baixo (${fdv:,.0f}).")
        if fdv > cfg["fdv_maximo_usd"]:
            return self._bloquear(criterios, "market_cap", "Market cap/FDV", fdv,
                                  {"min": cfg["fdv_minimo_usd"], "max": cfg["fdv_maximo_usd"]},
                                  f"Market cap alto (${fdv:,.0f}).")
        self._adicionar_criterio(criterios, "market_cap", "Market cap/FDV", fdv,
                                 {"min": cfg["fdv_minimo_usd"], "max": cfg["fdv_maximo_usd"]},
                                 reason=f"Usado {fdv_source}.")
        if estado_vol_h1 != "valid":
            return pending("volume_h1", "Volume 1h", estado_vol_h1, [("volume.h1", estado_vol_h1)], limiar=cfg["volume_h1_minimo_usd"])
        if vol_h1 < cfg["volume_h1_minimo_usd"]:
            return self._bloquear(criterios, "volume_h1", "Volume 1h", vol_h1,
                                  cfg["volume_h1_minimo_usd"], f"Volume 1h baixo (${vol_h1:,.0f}).", reavaliar=True)
        self._adicionar_criterio(criterios, "volume_h1", "Volume 1h", vol_h1, cfg["volume_h1_minimo_usd"])
        if estado_c_h1 != "valid" or estado_v_h1 != "valid":
            estado = "invalid" if "invalid" in (estado_c_h1, estado_v_h1) else "missing"
            return pending("transacoes_h1", "Transações 1h", estado,
                           [("txns.h1.buys", estado_c_h1), ("txns.h1.sells", estado_v_h1)], limiar=cfg["txns_h1_minimo"])
        if c_h1 + v_h1 < cfg["txns_h1_minimo"]:
            return self._bloquear(criterios, "transacoes_h1", "Transações 1h", c_h1 + v_h1,
                                  cfg["txns_h1_minimo"], f"Poucas transações 1h ({c_h1 + v_h1:.0f}).", reavaliar=True)
        self._adicionar_criterio(criterios, "transacoes_h1", "Transações 1h", c_h1 + v_h1, cfg["txns_h1_minimo"])
        if estado_var_h1 != "valid":
            return pending("variacao_h1", "Variação 1h", estado_var_h1, [("priceChange.h1", estado_var_h1)], limiar=cfg["subida_h1_maxima_pct"])
        if var_h1 > cfg["subida_h1_maxima_pct"]:
            return self._bloquear(criterios, "variacao_h1", "Variação 1h", var_h1,
                                  f"≤ {cfg['subida_h1_maxima_pct']}%", f"Já subiu {var_h1:.0f}% em 1h.")
        self._adicionar_criterio(criterios, "variacao_h1", "Variação 1h", var_h1,
                                 f"≤ {cfg['subida_h1_maxima_pct']}%")
        if estado_var_m5 != "valid":
            return pending("variacao_m5_max", "Variação m5", estado_var_m5, [("priceChange.m5", estado_var_m5)], limiar={"max": cfg["subida_m5_maxima_pct"], "min": cfg["queda_m5_maxima_pct"]})
        if var_m5 > cfg["subida_m5_maxima_pct"]:
            return self._bloquear(criterios, "variacao_m5_max", "Subida m5 máxima", var_m5,
                                  f"≤ {cfg['subida_m5_maxima_pct']}%", f"Já subiu {var_m5:.0f}% em 5min.")
        self._adicionar_criterio(criterios, "variacao_m5_max", "Subida m5 máxima", var_m5,
                                 f"≤ {cfg['subida_m5_maxima_pct']}%")
        if var_m5 < cfg["queda_m5_maxima_pct"]:
            return self._bloquear(criterios, "variacao_m5_min", "Queda m5 máxima", var_m5,
                                  f"≥ {cfg['queda_m5_maxima_pct']}%", f"A cair {var_m5:.0f}% em 5min.")
        self._adicionar_criterio(criterios, "variacao_m5_min", "Queda m5 máxima", var_m5,
                                 f"≥ {cfg['queda_m5_maxima_pct']}%")

        campos_score = (("volume.m5", estado_vol_m5), ("txns.m5.buys", estado_c_m5),
                        ("txns.m5.sells", estado_v_m5))
        faltam = [(n, s) for n, s in campos_score if s != "valid"]
        if faltam:
            estado = "invalid" if any(s == "invalid" for _, s in faltam) else "missing"
            return pending("dados_score", "Dados necessários para a pontuação", estado, campos_score, limiar="volume.m5 e transacoes m5 obrigatorios")
        self._adicionar_criterio(criterios, "dados_score", "Dados necessários para a pontuação",
                                 "completos", "volume.m5 e transações m5 disponíveis")

        # ---------- Pontuacao ----------
        d = {}
        # Liquidez (0-20)
        d["liquidez"] = 8 + 12 * escala(liq, cfg["liquidez_minima_usd"], 80000)
        # Relacao market cap / liquidez (0-10): muito alta = pouca liquidez para o tamanho
        racio = fdv / liq if liq > 0 else 999
        d["mcap/liq"] = 10 if 2 <= racio <= 15 else 6 if racio <= 40 and racio > 15 else 5 if racio < 2 else 2
        # Idade (0-10)
        d["idade"] = 10 if 15 <= idade_min <= 360 else 6
        # Volume (0-20)
        d["volume"] = 12 * escala(vol_h1 / liq if liq > 0 else 0, 0.2, 2.0) + 8 * escala(vol_m5, 500, 5000)
        # Pressao compradora (0-20)
        r_h1 = c_h1 / (c_h1 + v_h1) if (c_h1 + v_h1) > 0 else 0.5
        pts = 10 * escala(r_h1, 0.45, 0.65)
        if c_m5 + v_m5 >= 5:
            pts += 10 * escala(c_m5 / (c_m5 + v_m5), 0.45, 0.65)
        else:
            pts += 3  # poucos dados nos ultimos 5 min: valor neutro-baixo
        d["compras"] = pts
        # Momento (0-10): penaliza o que ja "pumpou" e o que esta a cair
        if 0 <= var_m5 <= 15:
            m = 5
        elif -5 <= var_m5 < 0:
            m = 3
        elif 15 < var_m5 <= 40:
            m = 2
        else:
            m = 0
        if 0 <= var_h1 <= 60:
            m += 5
        elif var_h1 > 60 or -20 <= var_h1 < 0:
            m += 2
        d["momento"] = m / 2.0
        acumulacao = score_acumulacao(par)
        d["acumulacao"] = acumulacao["score"] / 2.0

        parcial = sum(d.values())
        # RugCheck (0-10) - so consulta se o token ainda tiver hipotese de passar
        rc = None
        if cfg["usar_rugcheck"] and parcial + 10 >= cfg["pontuacao_minima"]:
            rc = self.rugcheck(mint)
        if rc is None:
            if parcial + 10 >= cfg["pontuacao_minima"]:
                return self._bloquear(criterios, "rugcheck", "RugCheck", None,
                                      cfg["rugcheck_score_maximo"],
                                      ("RugCheck indisponivel ou desativado; analise incompleta."
                                       if cfg["usar_rugcheck"] else
                                       "RugCheck obrigatorio desativado; analise incompleta."),
                                      status="pending_data", reavaliar=True,
                                      campos_ausentes=["rugcheck"])
            d["rugcheck"] = 5  # componente opcional desativado / token sem hipotese de passar
            rc_txt = "rugcheck=desativado" if not cfg["usar_rugcheck"] else "rugcheck=n/a"
            self._adicionar_criterio(criterios, "rugcheck", "RugCheck", None,
                                     cfg["rugcheck_score_maximo"], "not_evaluated",
                                     "Não consultado: mesmo a pontuação máxima não atingiria o mínimo." if cfg["usar_rugcheck"] else "Desativado na configuração.")
        else:
            if rc["perigos"] and cfg["rugcheck_rejeitar_perigo"]:
                return self._bloquear(criterios, "rugcheck", "RugCheck", rc["perigos"],
                                      "sem riscos level=danger", "RugCheck reportou perigo: " + ", ".join(rc["perigos"][:3]) + ".",
                                      pontuacao_parcial=parcial)
            if rc["score"] > cfg["rugcheck_score_maximo"]:
                return self._bloquear(criterios, "rugcheck", "RugCheck", rc["score"],
                                      cfg["rugcheck_score_maximo"], f"RugCheck risco {rc['score']:.0f}/100.",
                                      pontuacao_parcial=parcial)
            self._adicionar_criterio(criterios, "rugcheck", "RugCheck", rc["score"],
                                     cfg["rugcheck_score_maximo"])
            s = rc["score"]
            d["rugcheck"] = 10 if s <= 10 else 7 if s <= 30 else 4 if s <= 50 else 0
            rc_txt = f"rugcheck={s:.0f}"

        total = int(round(max(0, min(100, sum(d.values())))))
        aprovado = total >= cfg["pontuacao_minima"]
        partes = " ".join(f"{k}={v:.0f}" for k, v in d.items())
        self._adicionar_criterio(criterios, "pontuacao_minima", "Pontuação mínima", total,
                                 cfg["pontuacao_minima"], "passed" if aprovado else "rejected")
        motivo = (f"{'APROVADO' if aprovado else 'REPROVADO'} {total}/100 [{partes}] "
                  f"acumulacao={acumulacao['label']}({acumulacao['score']:.1f}/10) "
                  f"liq=${liq:,.0f} mcap=${fdv:,.0f} idade={idade_min:.0f}min "
                  f"vol1h=${vol_h1:,.0f} compras1h={r_h1 * 100:.0f}% m5={var_m5:+.0f}% h1={var_h1:+.0f}% {rc_txt}")
        self._preencher_nao_avaliados(criterios, None)
        return self._resultado("approved" if aprovado else "rejected", motivo,
                               aprovado=aprovado, pontuacao=total,
                               reavaliar=(not aprovado) and total >= cfg["pontuacao_minima"] - 15,
                               criterios=criterios, detalhes=d,
                               bloqueio=None if aprovado else {"id": "pontuacao_minima", "name": "Pontuação mínima",
                                   "observed": total, "threshold": cfg["pontuacao_minima"], "status": "rejected"},
                               ) | {"acumulacao": acumulacao}


# ============================================================================
# Carteira simulada: saldo, posicoes, ficheiros
# ============================================================================
CABECALHO_CSV = ["data_hora", "tipo", "simbolo", "mint", "preco_mercado", "preco_efetivo",
                 "valor_usd", "tokens", "custos_usd", "pnl_usd", "pnl_pct", "motivo",
                 "pontuacao", "duracao_min", "saldo_depois", "slippage_base_pct",
                 "slippage_base_aplicado_pct", "impacto_mercado_pct", "slippage_total_pct",
                 "taxa_pct", "taxa_estimada_usd", "debito_total_usd", "quantidade_liquida",
                 "liquidez_usd", "liquidez_estado", "fallback_execucao"]


class StateIntegrityError(RuntimeError):
    """Existing state cannot be loaded without risking loss of Paper data."""


def estado_novo(cfg):
    return {
        "versao": 1,
        "inicio": agora(),
        "operational_session": {
            "id": uuid.uuid4().hex,
            "fx_usd_per_eur": None,
            "fx_reference_date": None,
            "fx_locked_at_utc": None,
            "fx_source": None,
            "fx_generation": 0,
            "fx_history": [],
        },
        "paper_state_revision": 0,
        "paper_ops_applied": [],
        "saldo_inicial": float(cfg["saldo_inicial_usd"]),
        "saldo": float(cfg["saldo_inicial_usd"]),
        "posicoes": {},
        "vistos": {},
        "pendentes": {},
        "acoes": [],
        "sinais": [],      # tokens aprovados pelo Analista (aparecem na plataforma web)
        "historico": [],   # evolucao do valor total (grafico da plataforma web)
        "hist_passo": 30,  # segundos entre pontos do grafico (aumenta sozinho)
        "sinais_ts": [],   # hora de cada sinal das ultimas 24h
        "por_hora": {},    # quantos tokens novos foram analisados em cada hora (ultimas 48h)
        "scanner_pending": {}, # classificacao e motivo persistidos dos mints em espera
        "scanner_counters": {"daily": {}},
        "percursos": {},   # sinais cujo preco esta a ser gravado para o laboratorio
        "n_percursos": 0,  # quantos percursos completos ja foram gravados
        "ultimo_ciclo": 0, # ultima vez que o bot estava a correr (para detetar pausas)
        "stats": {"fechados": 0, "ganhos": 0, "perdas": 0, "pnl_total": 0.0,
                  "custos_total": 0.0, "melhor": 0.0, "pior": 0.0, "por_motivo": {}},
    }


def carregar_estado(cfg):
    with state_access_lock(cfg["ficheiro_estado"]):
        return _carregar_estado_com_lock(cfg)


def _carregar_estado_com_lock(cfg):
    caminho = cfg["ficheiro_estado"]
    if os.path.exists(caminho):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                dados = json.load(f)
            base = estado_novo(cfg)
            if isinstance(dados, dict) and isinstance(dados.get("posicoes"), dict):
                saldo_existente = dados.get("saldo")
                if (isinstance(saldo_existente, bool) or not isinstance(saldo_existente, (int, float))
                        or not math.isfinite(saldo_existente)):
                    raise ValueError("saldo ausente ou invalido; estado preservado")
                if "operational_session" in dados and not isinstance(dados["operational_session"], dict):
                    raise ValueError("operational_session invalida; estado preservado")
                if "paper_state_revision" in dados:
                    revision_existente = dados["paper_state_revision"]
                    if (isinstance(revision_existente, bool) or not isinstance(revision_existente, int)
                            or revision_existente < 0):
                        raise ValueError("paper_state_revision invalida; estado preservado")
                if "paper_ops_applied" in dados:
                    applied_existente = dados["paper_ops_applied"]
                    if (not isinstance(applied_existente, list)
                            or any(not isinstance(item, str) for item in applied_existente)):
                        raise ValueError("paper_ops_applied invalido; estado preservado")
                for chave, valor in base.items():  # garante que nao falta nenhum campo
                    atual = dados.get(chave)
                    numeros = isinstance(valor, (int, float)) and isinstance(atual, (int, float)) \
                        and not isinstance(atual, bool)
                    if chave not in dados or not (numeros or type(atual) is type(valor)):
                        dados[chave] = valor
                for chave, valor in base["stats"].items():
                    dados["stats"].setdefault(chave, valor)
                sessao = dados.get("operational_session")
                if not isinstance(sessao, dict):
                    dados["operational_session"] = base["operational_session"]
                else:
                    if not sessao.get("id"):
                        sessao["id"] = base["operational_session"]["id"]
                    for chave, valor in base["operational_session"].items():
                        sessao.setdefault(chave, valor)
                revision = dados.get("paper_state_revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise ValueError("paper_state_revision invalida")
                applied = dados.get("paper_ops_applied")
                if not isinstance(applied, list) or any(not isinstance(item, str) for item in applied):
                    raise ValueError("paper_ops_applied invalido")
                dados["saldo"] = num(dados.get("saldo"), base["saldo"])
                return dados
            raise ValueError("formato inesperado")
        except Exception as e:
            raise StateIntegrityError(
                f"{caminho} existe mas nao pode ser validado; preservado sem alteracao: {e}"
            ) from e
    return estado_novo(cfg)


def guardar_estado(cfg, estado, strict=False):
    """Atomically persist state. Strict callers receive storage errors."""
    try:
        with state_access_lock(cfg["ficheiro_estado"]):
            atomic_write_json(cfg["ficheiro_estado"], estado)
        return True
    except (OSError, ValueError, TypeError, PaperOperationError, RuntimeError) as e:
        if strict:
            raise
        log.error("Nao consegui guardar o estado: %s", e)
        return False


def registar_trade(cfg, linha):
    raise PaperOperationError(
        "gravacao direta em trades.csv desativada; usar o diario idempotente Paper"
    )


def ler_trades_paper(cfg):
    """Le o historico Paper Trading sem alterar o CSV; None indica que nao foi possivel le-lo."""
    caminho = cfg["ficheiro_trades"]
    if not os.path.exists(caminho):
        return []
    try:
        with open(caminho, "r", newline="", encoding="utf-8") as f:
            leitor = csv.DictReader(f)
            if not leitor.fieldnames or not {"tipo", "mint", "data_hora"}.issubset(leitor.fieldnames):
                return None
            resultado = []
            for linha in leitor:
                if not isinstance(linha, dict):
                    continue
                item = {k: linha.get(k) or "" for k in
                        ("data_hora", "tipo", "simbolo", "mint", "motivo")}
                for k in ("preco_mercado", "preco_efetivo", "valor_usd", "tokens", "custos_usd",
                          "pnl_usd", "pnl_pct", "pontuacao", "duracao_min"):
                    bruto = linha.get(k)
                    try:
                        valor = float(bruto) if bruto not in (None, "") else None
                    except (TypeError, ValueError):
                        valor = None
                    item[k] = valor if valor is not None and math.isfinite(valor) else None
                if item["tipo"] in ("COMPRA", "VENDA", "ANULADA"):
                    resultado.append(item)
            return resultado
    except (OSError, csv.Error, UnicodeError):
        return None


def slippage_total(cfg, valor_usd, liquidez_usd):
    """Slippage base + impacto no preco (ordem grande numa pool pequena = pior preco)."""
    s = cfg["slippage_pct"] / 100.0
    if cfg["simular_impacto_preco"]:
        if liquidez_usd > 0:
            s += valor_usd / (liquidez_usd / 2.0)  # so metade da liquidez esta do "nosso" lado
        else:
            s = 1.0
    return max(0.0, min(s, 0.95))


def simular_venda(cfg, pos, preco, liquidez):
    """Quanto receberiamos se vendessemos agora (ja com slippage e taxa)."""
    bruto = pos["tokens"] * preco
    s = slippage_total(cfg, bruto, liquidez)
    preco_ef = preco * (1 - s)
    receita = pos["tokens"] * preco_ef * (1 - cfg["taxa_pct"] / 100.0)
    return max(0.0, receita), preco_ef, max(0.0, bruto - receita)


def _fechar_sinal_no_estado(estado, mint, pnl_pct, motivo):
    for sinal in reversed(estado.get("sinais", [])):
        if sinal.get("mint") == mint and sinal.get("estado") == "comprado":
            sinal["estado"], sinal["resultado"], sinal["saida"] = "fechado", round(pnl_pct, 2), motivo
            sinal["ts_fecho"], sinal["hora_fecho"] = agora(), hora_txt()
            return True
    return False


# ============================================================================
# AGENTE 3 - EXECUTOR
# ============================================================================
def _decimal_canonico(valor):
    """String decimal que round-trips para o mesmo valor numerico do Executor."""
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as erro:
        raise ValueError("valor decimal indisponivel") from erro
    if not numero.is_finite():
        raise ValueError("valor decimal nao finito")
    texto = format(numero.normalize(), "f")
    return texto if texto not in ("", "-0") else "0"


def _json_canonico(valor):
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc_iso(timestamp):
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class PaperExecutionPlan:
    """Immutable canonical output of the Executor's existing Paper cost calculation."""
    canonical_json: str

    def fields(self):
        return json.loads(self.canonical_json)


@dataclass(frozen=True)
class PaperProposal:
    """Immutable canonical proposal; JSON strings freeze nested pair and analysis data."""
    proposal_id: str
    confirmation_code: str
    mint: str
    symbol: str
    pair_address: str
    amount_usd: str
    price_usd: str
    liquidity_usd: str
    score: object
    analysis_summary: str
    created_at_utc: str
    expires_at_utc: str
    created_epoch: float
    expires_epoch: float
    expires_monotonic: float
    pair_json: str
    analysis_json: str
    execution_json: str

    @property
    def pair(self):
        return json.loads(self.pair_json)

    @property
    def analysis(self):
        return json.loads(self.analysis_json)

    def public_fields(self):
        plan = json.loads(self.execution_json)
        return {
            "proposal_id": self.proposal_id, "mint": self.mint, "symbol": self.symbol,
            "pair_address": self.pair_address, "amount_usd": self.amount_usd,
            "price_usd": self.price_usd, "liquidity_usd": self.liquidity_usd,
            "reference_price_usd": plan["reference_price_usd"],
            "slippage_base_pct": plan["slippage_base_pct"],
            "slippage_base_applied_pct": plan["slippage_base_applied_pct"],
            "market_impact_pct": plan["market_impact_pct"],
            "slippage_total_pct": plan["slippage_total_pct"],
            "effective_price_usd": plan["effective_price_usd"],
            "taxa_pct": plan["taxa_pct"],
            "estimated_fee_usd": plan["estimated_fee_usd"],
            "debit_total_usd": plan["debit_total_usd"],
            "net_tokens": plan["net_tokens"],
            "liquidity_observed_usd": plan["liquidity_observed_usd"],
            "liquidity_status": plan["liquidity_status"],
            "impact_fallback": plan["impact_fallback"],
            "score": self.score, "analysis_summary": self.analysis_summary,
            "created_at_utc": self.created_at_utc, "expires_at_utc": self.expires_at_utc,
        }

    def terminal_lines(self):
        data = self.public_fields()
        return (
            f"ID completo: {data['proposal_id']}", f"Mint: {data['mint']}",
            f"Par: {data['pair_address']}",
            f"Preco de referencia USD: {data['reference_price_usd']}",
            f"Slippage base configurado (%): {data['slippage_base_pct']}",
            f"Slippage base aplicado (%): {data['slippage_base_applied_pct']}",
            f"Impacto de mercado aplicado (%): {data['market_impact_pct'] or 'Indisponivel'}",
            f"Slippage total aplicado (%): {data['slippage_total_pct']}",
            f"Preco efetivo simulado USD: {data['effective_price_usd']}",
            f"Taxa estimada ({data['taxa_pct']}%): {data['estimated_fee_usd']} USD",
            f"Debito total USD: {data['debit_total_usd']}",
            f"Quantidade liquida estimada: {data['net_tokens']}",
            f"Liquidez observada USD: {data['liquidity_observed_usd'] or 'Indisponivel'}",
            f"Estado da liquidez: {data['liquidity_status']}",
            f"Fallback de slippage: {'sim' if data['impact_fallback'] else 'nao'}",
            f"Criada UTC: {data['created_at_utc']}", f"Expira UTC: {data['expires_at_utc']}",
            f"Analise: score={data['score']} | {data['analysis_summary']}",
        )

    def __getitem__(self, key):
        if key == "pair":
            return self.pair
        if key == "analysis":
            return self.analysis
        if key == "created_at":
            return self.created_epoch
        if key == "expires_at":
            return self.expires_epoch
        return getattr(self, key)


class ManualApprovalManager:
    """One-use, in-memory approvals bound to a single Paper order proposal."""
    def __init__(self, ttl_seconds=90, terminal_command_handler=None):
        self.ttl_seconds = max(15, min(int(ttl_seconds), 300))
        self._lock = threading.Lock()
        self._pending = {}
        self._pending_mints = {}
        self._statuses = {}
        self._permits = {}
        self._issued_ids = set()
        self._issued_codes = set()
        self._forced_reasons = {}
        self._dashboard_decided = {}  # proposal_id -> "aprovar" | "recusar" (decisoes feitas no dashboard)
        self._origem_decisao = {}     # proposal_id -> "dashboard" | "telegram"
        self._terminal_command_handler = terminal_command_handler
        self._terminal_buffer = ""
        self._requests = queue.Queue()
        self._results = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            print("Comandos Paper no terminal: PARAR_PAPER <motivo> | REARMAR_PAPER <ID_PARAGEM> <motivo>")
            self._thread = threading.Thread(target=self._terminal_worker,
                                           name="paper-manual-approval", daemon=True)
            self._thread.start()

    def propose(self, mint, par, analysis, amount, execution_plan=None):
        with self._lock:
            now_mono, now_epoch = time.monotonic(), float(agora())
            existing_id = self._pending_mints.get(mint)
            if existing_id in self._pending:
                existing = self._pending[existing_id]
                if self._statuses.get(existing_id) == "pending" and existing.expires_monotonic > now_mono:
                    return existing
                self._statuses[existing_id] = "expired"
                if self._pending_mints.get(mint) == existing_id:
                    self._pending_mints.pop(mint, None)
            price = _decimal_canonico(num(par.get("priceUsd")))
            amount_text = _decimal_canonico(amount)
            liquidity = _decimal_canonico(num(obter(par, "liquidity", "usd")))
            if Decimal(price) <= 0 or Decimal(amount_text) <= 0:
                raise ValueError("proposta exige preco e valor positivos")
            pair_address = str(par.get("pairAddress") or "")
            if not pair_address:
                raise ValueError("proposta sem pairAddress")
            if not isinstance(execution_plan, PaperExecutionPlan):
                raise ValueError("proposta exige plano financeiro imutavel do Executor")
            plan = execution_plan.fields()
            if (plan.get("mint") != str(mint) or plan.get("pair_address") != pair_address
                    or plan.get("debit_total_usd") != amount_text
                    or plan.get("reference_price_usd") != price
                    or plan.get("liquidity_calc_usd") != liquidity):
                raise ValueError("plano financeiro nao corresponde aos dados da proposta")
            pair_copy, analysis_copy = copy.deepcopy(par), copy.deepcopy(analysis)
            proposal_id = uuid.uuid4().hex
            while proposal_id in self._issued_ids:
                proposal_id = uuid.uuid4().hex
            code = secrets.token_hex(16).upper()
            while code in self._issued_codes:
                code = secrets.token_hex(16).upper()
            proposal = PaperProposal(
                proposal_id, code, str(mint), simbolo_limpo(par), pair_address, amount_text,
                price, liquidity, analysis_copy.get("pontuacao"),
                str(analysis_copy.get("motivo") or "Resumo indisponivel"),
                _utc_iso(now_epoch), _utc_iso(now_epoch + self.ttl_seconds),
                now_epoch, now_epoch + self.ttl_seconds, now_mono + self.ttl_seconds,
                _json_canonico(pair_copy), _json_canonico(analysis_copy), execution_plan.canonical_json,
            )
            self._issued_ids.add(proposal_id)
            self._issued_codes.add(code)
            self._pending[proposal_id] = proposal
            self._statuses[proposal_id] = "pending"
            self._pending_mints[mint] = proposal_id
            if self._thread is not None and self._thread.is_alive():
                self._requests.put(proposal_id)
            return proposal

    def public_pending(self):
        with self._lock:
            now = time.monotonic()
            return [proposal.public_fields() for pid, proposal in self._pending.items()
                    if self._statuses.get(pid) == "pending" and proposal.expires_monotonic > now]

    @staticmethod
    def _read_line_until(deadline, stop_event):
        chars = ""
        if os.name == "nt":
            import msvcrt
            while time.monotonic() < deadline and not stop_event.is_set():
                if not msvcrt.kbhit():
                    time.sleep(0.05)
                    continue
                char = msvcrt.getwch()
                if char in ("\r", "\n"):
                    print()
                    return chars
                if char == "\003":
                    return None
                if char == "\b":
                    chars = chars[:-1]
                    continue
                if char.isprintable():
                    chars += char
                    print(char, end="", flush=True)
            return None
        import select
        while time.monotonic() < deadline and not stop_event.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], min(0.1, max(0, deadline-time.monotonic())))
            if ready:
                char = os.read(sys.stdin.fileno(), 1).decode(
                    getattr(sys.stdin, "encoding", None) or "utf-8", errors="replace"
                )
                if not char:
                    return None
                if char in ("\r", "\n"):
                    return chars
                if char in ("\b", "\x7f"):
                    chars = chars[:-1]
                    continue
                if char == "\x03":
                    return None
                chars += char
        return None

    @staticmethod
    def _read_terminal_char(timeout, stop_event):
        """Read terminal input in short slices; Scanner/Risk never waits for input."""
        if stop_event.is_set():
            return None
        if os.name == "nt":
            import msvcrt
            if not msvcrt.kbhit():
                time.sleep(max(0.01, min(timeout, 0.1)))
                return None
            char = msvcrt.getwch()
            if char == "\003":
                return "\003"
            if char in ("\r", "\n"):
                print()
                return "\n"
            if char == "\b":
                print("\b \b", end="", flush=True)
                return "\b"
            if char.isprintable():
                print(char, end="", flush=True)
                return char
            return None
        import select
        try:
            ready, _, _ = select.select([sys.stdin], [], [], max(0.0, min(timeout, 0.1)))
            if not ready:
                return None
            data = os.read(sys.stdin.fileno(), 1)
            if not data:
                return "\004"
            return data.decode(getattr(sys.stdin, "encoding", None) or "utf-8", errors="replace")
        except (OSError, ValueError):
            return "\004"

    def _terminal_worker(self):
        if self._terminal_command_handler is None:
            # Preserve the simple approval-only worker for isolated manager fixtures.
            while not self._stop.is_set():
                try:
                    proposal_id = self._requests.get(timeout=0.2)
                except queue.Empty:
                    continue
                if proposal_id is None:
                    return
                with self._lock:
                    proposal = self._pending.get(proposal_id)
                    if not proposal or self._statuses.get(proposal_id) != "pending":
                        continue
                    remaining = proposal.expires_monotonic - time.monotonic()
                if remaining <= 0:
                    self._results.put((proposal_id, ""))
                    continue
                try:
                    answer = self._read_line_until(time.monotonic() + remaining, self._stop)
                except Exception:
                    log.exception("Erro a ler confirmacao manual; proposta recusada")
                    answer = None
                self._results.put((proposal_id, answer.strip() if isinstance(answer, str) else ""))
            return
        active_id = None
        active_deadline = None
        while not self._stop.is_set():
            if active_id is None:
                try:
                    proposal_id = self._requests.get_nowait()
                except queue.Empty:
                    proposal_id = None
                if proposal_id is None and self._stop.is_set():
                    return
                if proposal_id is not None:
                    with self._lock:
                        proposal = self._pending.get(proposal_id)
                        if proposal and self._statuses.get(proposal_id) == "pending":
                            active_id = proposal_id
                            active_deadline = proposal.expires_monotonic
                            print("\nPROPOSTA PAPER")
                            for line in proposal.terminal_lines():
                                print(line)
                            print(f"Confirme apenas no terminal: APROVAR {proposal.proposal_id} {proposal.confirmation_code}")
                            print("Comandos terminais: PARAR_PAPER <motivo> | REARMAR_PAPER <ID_PARAGEM> <motivo>")
                            print("Resposta ausente, incompleta, incorreta ou expirada rejeita a proposta.")
            if active_id is not None and active_id in self._dashboard_decided:
                print(f"\nProposta {active_id[:12]} decidida no {self._origem_decisao.get(active_id, 'dashboard')} "
                      f"({self._dashboard_decided[active_id]}).")
                active_id = active_deadline = None
                self._terminal_buffer = ""
            if active_id is not None and time.monotonic() >= active_deadline:
                self._forced_reasons[active_id] = "proposta expirada"
                self._results.put((active_id, ""))
                active_id = active_deadline = None
            try:
                char = self._read_terminal_char(0.05, self._stop)
            except Exception:
                log.exception("Erro a ler terminal Paper; propostas recusadas")
                char = "\004"
            if char in ("\003", "\004"):
                self._stop.set()
                if active_id is not None:
                    self._forced_reasons[active_id] = "interrupção ou EOF no terminal"
                    self._results.put((active_id, ""))
                    active_id = None
                continue
            if char == "\b":
                self._terminal_buffer = self._terminal_buffer[:-1]
                continue
            if char not in (None, "\n"):
                self._terminal_buffer = (self._terminal_buffer + char)[-512:]
                continue
            if char != "\n":
                continue
            answer, self._terminal_buffer = self._terminal_buffer.strip(), ""
            if not answer:
                continue
            upper = answer.upper()
            if upper.startswith("PARAR_PAPER ") or upper.startswith("REARMAR_PAPER "):
                parts = answer.split(maxsplit=2)
                action = "activate" if parts[0].upper() == "PARAR_PAPER" else "rearm"
                argument = (answer.partition(" ")[2].strip() if action == "activate" else
                            (parts[1] + " " + parts[2] if action == "rearm" and len(parts) > 2 else ""))
                try:
                    result = (self._terminal_command_handler(action, argument)
                              if callable(self._terminal_command_handler) else (False, "comando indisponível"))
                    ok, message = result if isinstance(result, tuple) else (bool(result), "")
                except Exception as exc:
                    log.exception("Falha no comando terminal de emergência Paper")
                    ok, message = False, type(exc).__name__
                print(("Paragem Paper atualizada: " if ok else "Comando Paper recusado: ") + str(message))
                if action == "activate":
                    self.invalidate_pending("paragem de emergência ativa")
                    active_id = active_deadline = None
                continue
            if active_id is not None:
                self._results.put((active_id, answer))
                active_id = active_deadline = None
            else:
                print("Sem proposta ativa. Comando: PARAR_PAPER <motivo> ou REARMAR_PAPER <ID_PARAGEM> <motivo>.")

    def esta_pendente(self, proposal_id):
        """True se a proposta ainda espera decisao (nao decidida, nao expirada)."""
        with self._lock:
            proposal = self._pending.get(proposal_id)
            return (proposal is not None and self._statuses.get(proposal_id) == "pending"
                    and proposal_id not in self._dashboard_decided
                    and proposal.expires_monotonic > time.monotonic())

    def decidir_dashboard(self, proposal_id, decisao, origem="dashboard"):
        """Decisao explicita vinda do dashboard local (Paper). Usa a mesma fila que o terminal.

        Aprovar equivale a escrever no terminal a linha APROVAR <id> <codigo>; o Executor continua a
        revalidar tudo (plano, limites, paragem de emergencia) antes de executar. Devolve (ok, mensagem).
        """
        if decisao not in ("aprovar", "recusar"):
            return False, "decisao invalida"
        with self._lock:
            proposal = self._pending.get(proposal_id)
            if proposal is None or self._statuses.get(proposal_id) != "pending":
                return False, "proposta inexistente ou ja decidida"
            if proposal_id in self._dashboard_decided:
                return False, f"proposta ja decidida no {self._origem_decisao.get(proposal_id, 'dashboard')}"
            if proposal.expires_monotonic <= time.monotonic():
                return False, "proposta expirada"
            self._dashboard_decided[proposal_id] = decisao
            self._origem_decisao[proposal_id] = "telegram" if origem == "telegram" else "dashboard"
            if decisao == "aprovar":
                self._results.put((proposal_id, f"APROVAR {proposal.proposal_id} {proposal.confirmation_code}"))
                return True, "aprovada; o Executor vai revalidar e executar em Paper"
        self.cancel(proposal_id, "recusada no Telegram" if origem == "telegram" else "recusada no dashboard")
        return True, "recusada"

    def invalidate_pending(self, reason):
        with self._lock:
            pending_ids = {proposal_id for proposal_id, status in self._statuses.items()
                           if status in ("pending", "approved")}
            for token, proposal_id in list(self._permits.items()):
                if proposal_id in pending_ids:
                    self._permits.pop(token, None)
            for proposal_id, proposal in self._pending.items():
                if self._statuses.get(proposal_id) not in ("pending", "approved"):
                    continue
                self._statuses[proposal_id] = "forced_rejected"
                self._forced_reasons[proposal_id] = str(reason)
                self._pending_mints.pop(proposal.mint, None)
                self._results.put((proposal_id, ""))

    def cancel(self, proposal_id, reason):
        with self._lock:
            proposal = self._pending.get(proposal_id)
            if proposal is None or self._statuses.get(proposal_id) not in ("pending", "approved"):
                return False
            self._statuses[proposal_id] = "forced_rejected"
            self._forced_reasons[proposal_id] = str(reason)
            self._pending_mints.pop(proposal.mint, None)
            for token, pid in list(self._permits.items()):
                if pid == proposal_id:
                    self._permits.pop(token, None)
            self._results.put((proposal_id, ""))
            return True

    def drain_results(self):
        found = []
        while True:
            try:
                proposal_id, answer = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                proposal = self._pending.get(proposal_id)
                if proposal is None:
                    continue
                status = self._statuses.get(proposal_id)
                if status == "forced_rejected":
                    accepted = False
                    reason = self._forced_reasons.pop(proposal_id, "proposta invalidada")
                    self._statuses[proposal_id] = "rejected"
                    found.append((proposal, None, accepted, reason))
                    continue
                if status != "pending":
                    continue
                accepted = (proposal.expires_monotonic > time.monotonic()
                            and answer == f"APROVAR {proposal.proposal_id} {proposal.confirmation_code}")
                reason = (("aprovada no Telegram" if self._origem_decisao.get(proposal_id) == "telegram"
                           else "aprovada no dashboard") if self._dashboard_decided.get(proposal_id) == "aprovar"
                          else "aprovada manualmente") if accepted else (
                    "proposta expirada" if proposal.expires_monotonic <= time.monotonic()
                    else "ID ou codigo incorreto")
                self._statuses[proposal_id] = "approved" if accepted else "rejected"
                if self._pending_mints.get(proposal.mint) == proposal_id:
                    self._pending_mints.pop(proposal.mint, None)
                token = None
                if accepted:
                    token = secrets.token_urlsafe(32)
                    self._permits[token] = proposal_id
                found.append((proposal, token, accepted, reason))
        return found

    def consume(self, token, mint, par, amount=None):
        if not isinstance(token, str):
            return False
        with self._lock:
            proposal_id = self._permits.pop(token, None)  # consumed even when validation fails
            proposal = self._pending.get(proposal_id)
            if proposal is None or proposal.expires_monotonic <= time.monotonic():
                return False
            current_plan = par.get("_paper_execution_plan")
            return (proposal.mint == str(mint)
                    and proposal.pair_address == str(par.get("pairAddress") or "")
                    and proposal.execution_json == current_plan
                    and proposal.amount_usd == (_decimal_canonico(amount) if amount is not None else ""))

    def proposal_id_for_token(self, token):
        with self._lock:
            return self._permits.get(token) if isinstance(token, str) else None

    def discard(self, token):
        if not isinstance(token, str):
            return False
        with self._lock:
            return self._permits.pop(token, None) is not None

    def close(self):
        self._stop.set()
        self._requests.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        with self._lock:
            self._pending.clear()
            self._pending_mints.clear()
            self._statuses.clear()
            self._permits.clear()
            self._issued_ids.clear()
            self._issued_codes.clear()
            self._forced_reasons.clear()
            self._origem_decisao.clear()


class Executor:
    """Simula compras e vendas. Nao toca em dinheiro real."""

    def __init__(self, cfg, estado, bot):
        self.cfg = cfg
        self.estado = estado
        self.bot = bot
        self._purchase_lock = threading.Lock()
        self._buy_commit_capability = object()
        self.bot.paper_operations.bind_buy_guard(self._validar_commit_compra,
                                                 self._buy_commit_capability)

    def capital(self):
        """Saldo livre + custo das posicoes abertas."""
        return self.estado["saldo"] + sum(p["custo"] for p in self.estado["posicoes"].values())

    def execution_conditions(self):
        return {
            "taxa_pct": _decimal_canonico(self.cfg.get("taxa_pct")),
            "slippage_pct": _decimal_canonico(self.cfg.get("slippage_pct")),
            "simular_impacto_preco": bool(self.cfg.get("simular_impacto_preco")),
        }

    def _validar_commit_compra(self, authorization, current_state, candidate_state, trade_row):
        """Final storage-boundary validation; raw PaperOperationStore BUYs are refused."""
        if (not isinstance(authorization, dict)
                or authorization.get("manual_confirmation_consumed") is not True
                or authorization.get("execution_plan") is None
                or not authorization.get("proposal_id")):
            return False
        emergency_check = getattr(self.bot, "_emergency_stop_is_active", None)
        if not callable(emergency_check) or emergency_check():
            return False
        amount, _reason = self.pode_comprar()
        if amount <= 0:
            return False
        try:
            plan = json.loads(authorization["execution_plan"])
            if _json_canonico(authorization.get("plan_fields")) != authorization["execution_plan"]:
                return False
            debit = Decimal(plan["debit_total_usd"])
            fx = validate_locked_session_rate(current_state.get("operational_session", {}))
            eur_limit = min(Decimal(str(self.cfg["max_eur_per_operation"])), MAX_OPERATION_EUR)
            max_usd = usd_cap_from_eur(eur_limit, fx)
            positions_before = current_state.get("posicoes", {})
            positions_after = candidate_state.get("posicoes", {})
            new_mints = set(positions_after) - set(positions_before)
            if (debit > max_usd or debit != Decimal(_decimal_canonico(amount))
                    or len(positions_before) >= MAX_OPEN_POSITIONS
                    or len(positions_after) > MAX_OPEN_POSITIONS or len(new_mints) != 1):
                return False
            mint = next(iter(new_mints))
            fields = authorization.get("plan_fields")
            if not isinstance(fields, dict) or fields.get("mint") != mint:
                return False
            position = positions_after[mint]
            checks = (
                Decimal(_decimal_canonico(position.get("custo"))) == debit,
                Decimal(_decimal_canonico(position.get("tokens"))) == Decimal(fields["net_tokens"]),
                Decimal(_decimal_canonico(position.get("preco_efetivo"))) == Decimal(fields["effective_price_usd"]),
                Decimal(_decimal_canonico(trade_row.get("valor_usd"))) == debit,
                str(trade_row.get("preco_efetivo")) == fields["effective_price_usd"],
                str(trade_row.get("quantidade_liquida")) == fields["net_tokens"],
                str(trade_row.get("debito_total_usd")) == fields["debit_total_usd"],
            )
            cash_delta = Decimal(_decimal_canonico(current_state.get("saldo"))) - Decimal(
                _decimal_canonico(candidate_state.get("saldo")))
            cash_before = Decimal(_decimal_canonico(current_state.get("saldo")))
            cash_after = Decimal(_decimal_canonico(candidate_state.get("saldo")))
            # State balances are existing floats; accept only their bounded binary
            # representation error, never a meaningful debit discrepancy.
            tolerance = max(Decimal("1e-12"), max(abs(cash_before), abs(cash_after), Decimal(1))
                            * Decimal("1e-15"))
            return all(checks) and abs(cash_delta - debit) <= tolerance
        except (KeyError, TypeError, ValueError, InvalidOperation, EcbRateError):
            return False

    def preparar_execucao(self, mint, par, amount):
        """Calculate one canonical buy plan using the same Paper formulas as the Executor."""
        cfg = self.cfg
        valor = float(amount)
        preco = num(par.get("priceUsd"))
        liquidez_raw = obter(par, "liquidity", "usd")
        liquidez = num(liquidez_raw)
        pair_address = str(par.get("pairAddress") or "")
        if not pair_address or preco <= 0 or valor <= 0:
            raise ValueError("plano exige mint, par, preco e valor validos")

        liquidity_observed = None
        liquidity_status = "unavailable"
        if liquidez_raw is not None:
            try:
                parsed_liquidity = Decimal(str(liquidez_raw))
                if parsed_liquidity.is_finite() and parsed_liquidity >= 0:
                    liquidity_observed = _decimal_canonico(parsed_liquidity)
                    liquidity_status = "observed_zero" if parsed_liquidity == 0 else "observed"
                else:
                    liquidity_status = "invalid"
            except (InvalidOperation, TypeError, ValueError):
                liquidity_status = "invalid"

        slippage_fraction = slippage_total(cfg, valor, liquidez)
        impact_enabled = bool(cfg["simular_impacto_preco"])
        if impact_enabled and liquidez > 0:
            base_applied = min(max(cfg["slippage_pct"] / 100.0, 0.0), slippage_fraction)
            market_impact_applied = max(0.0, slippage_fraction - base_applied)
            impact_fallback = False
        elif impact_enabled:
            # The existing slippage_total() replaces base slippage with its 95% fallback.
            base_applied = 0.0
            market_impact_applied = None
            impact_fallback = True
        else:
            base_applied = slippage_fraction
            market_impact_applied = 0.0
            impact_fallback = False

        effective_price = preco * (1 + slippage_fraction)
        fee_pct = cfg["taxa_pct"]
        estimated_fee = valor * fee_pct / 100.0
        net_tokens = (valor - estimated_fee) / effective_price
        costs = valor - net_tokens * preco
        if not all(math.isfinite(v) for v in
                   (effective_price, estimated_fee, net_tokens, costs, slippage_fraction)):
            raise ValueError("plano financeiro nao finito")

        fields = {
            "mint": str(mint), "pair_address": pair_address,
            "reference_price_usd": _decimal_canonico(preco),
            "slippage_base_pct": _decimal_canonico(cfg["slippage_pct"]),
            "slippage_base_applied_pct": _decimal_canonico(base_applied * 100.0),
            "market_impact_pct": (_decimal_canonico(market_impact_applied * 100.0)
                                  if market_impact_applied is not None else None),
            "slippage_total_pct": _decimal_canonico(slippage_fraction * 100.0),
            "effective_price_usd": _decimal_canonico(effective_price),
            "taxa_pct": _decimal_canonico(fee_pct),
            "estimated_fee_usd": _decimal_canonico(estimated_fee),
            "debit_total_usd": _decimal_canonico(valor),
            "net_tokens": _decimal_canonico(net_tokens),
            "costs_usd": _decimal_canonico(costs),
            "liquidity_observed_usd": liquidity_observed,
            "liquidity_calc_usd": _decimal_canonico(liquidez),
            "liquidity_status": liquidity_status,
            "impact_fallback": impact_fallback,
            "execution_conditions": self.execution_conditions(),
        }
        return PaperExecutionPlan(_json_canonico(fields))

    def pode_comprar(self):
        """Devolve (valor_a_investir, motivo_se_nao_puder)."""
        cfg, e = self.cfg, self.estado
        operational_path = cfg.get("_operational_config_path")
        if not cfg.get("limites_operacionais_ativos") or not operational_path:
            return 0.0, "politica operacional Paper nao carregada; compras bloqueadas"
        emergency_check = getattr(self.bot, "_emergency_stop_is_active", None)
        if not callable(emergency_check) or emergency_check():
            return 0.0, "paragem de emergência Paper ativa; novas propostas e compras bloqueadas"
        audit_journal = getattr(self.bot, "paper_audit", None)
        if audit_journal is None or audit_journal.blocked:
            return 0.0, f"diário de auditoria Paper indisponível; compras bloqueadas: {getattr(audit_journal, 'error', 'ausente')}"
        if cfg.get("erro_config_operacional"):
            return 0.0, "configuracao operacional indisponivel; compras bloqueadas"
        try:
            policy = load_operational_config(operational_path)
        except OperationalConfigError:
            return 0.0, "configuracao Paper invalida ou indisponivel; compras bloqueadas"
        cfg["mode"] = policy["mode"]
        cfg["live_trading_enabled"] = policy["live_trading_enabled"]
        cfg["private_keys_in_application"] = policy["private_keys_in_application"]
        cfg["manual_confirmation_required"] = policy["manual_confirmation_required"]
        cfg["max_eur_per_operation"] = policy["max_eur_per_operation"]
        cfg["max_posicoes_abertas"] = policy["max_open_positions"]
        cfg["max_slippage_pct"] = policy["max_slippage_pct"]
        if (cfg.get("mode") != "paper" or cfg.get("live_trading_enabled") is not False
                or cfg.get("private_keys_in_application") is not False
                or cfg.get("manual_confirmation_required") is not True):
            return 0.0, "modo Paper ou confirmacao manual obrigatoria indisponivel; compras bloqueadas"
        if (cfg.get("max_posicoes_abertas") != MAX_OPEN_POSITIONS
                or cfg.get("max_eur_per_operation") is None
                or Decimal(str(cfg["max_eur_per_operation"])) > MAX_OPERATION_EUR):
            return 0.0, "limites operacionais excedem a politica Paper; compras bloqueadas"
        if cfg.get("max_slippage_pct") is None:
            return 0.0, "limite máximo de slippage ainda não foi aprovado; propostas bloqueadas"
        store = getattr(self.bot, "paper_operations", None)
        if store is None or store.blocked:
            return 0.0, "diario Paper pendente ou indisponivel; compras bloqueadas"
        if not getattr(self.bot, "_fx_rate_persisted", False):
            return 0.0, "taxa BCE ainda nao confirmada em estado.json; compras bloqueadas"
        if len(e["posicoes"]) >= MAX_OPEN_POSITIONS:
            return 0.0, f"maximo de {MAX_OPEN_POSITIONS} posicoes abertas"
        capital = self.capital()
        limite = cfg["parar_compras_se_perda_pct"]
        if limite and capital <= e["saldo_inicial"] * (1 - limite / 100.0):
            return 0.0, f"capital caiu mais de {limite}% - compras suspensas"
        investido = capital - e["saldo"]
        folga = capital * cfg["exposicao_maxima_pct"] / 100.0 - investido
        valor_planeado = min(capital * cfg["tamanho_posicao_pct"] / 100.0, folga)
        sessao = e.get("operational_session")
        if not isinstance(sessao, dict) or not sessao.get("fx_usd_per_eur"):
            return 0.0, "taxa BCE USD/EUR indisponivel; compras bloqueadas"
        try:
            fx = validate_locked_session_rate(sessao)
            eur_limit = min(Decimal(str(cfg["max_eur_per_operation"])), MAX_OPERATION_EUR)
            teto = float(usd_cap_from_eur(eur_limit, fx))
        except (EcbRateError, KeyError, TypeError, ValueError, InvalidOperation):
            return 0.0, "taxa BCE USD/EUR invalida ou desatualizada; compras bloqueadas"
        valor = min(valor_planeado, teto)
        if e["saldo"] < valor:
            return 0.0, "saldo livre insuficiente para financiar a ordem completa"
        if valor < cfg["posicao_minima_usd"]:
            return 0.0, "exposicao maxima atingida ou saldo insuficiente"
        return valor, ""

    def bloqueio_plano_financeiro(self, plan):
        """Fail closed unless liquidity and impact are observed and within approved cap."""
        fields = plan.fields() if isinstance(plan, PaperExecutionPlan) else plan
        if fields.get("liquidity_status") != "observed":
            return "liquidez indisponível, zero ou inválida; proposta bloqueada"
        try:
            liquidity = Decimal(fields["liquidity_observed_usd"])
            impact = Decimal(fields["market_impact_pct"])
            total = Decimal(fields["slippage_total_pct"])
            cap = Decimal(str(self.cfg.get("max_slippage_pct")))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return "estimativa de impacto ou limite de slippage indisponível; proposta bloqueada"
        if not liquidity.is_finite() or liquidity <= 0:
            return "liquidez observada inválida; proposta bloqueada"
        if fields.get("impact_fallback") or not impact.is_finite():
            return "não existe estimativa fiável de impacto; proposta bloqueada"
        if not fields.get("execution_conditions", {}).get("simular_impacto_preco"):
            return "simulação de impacto desativada; proposta bloqueada"
        if not total.is_finite() or total > cap:
            return f"slippage total excede o limite operacional aprovado ({cap}%); proposta bloqueada"
        return ""

    def comprar(self, mint, par, analise, approval_token=None, execution_plan=None):
        cfg, e = self.cfg, self.estado
        simbolo = simbolo_limpo(par)
        with self._purchase_lock:
            approvals = getattr(self.bot, "manual_approvals", None)
            if mint in e["posicoes"]:
                if approvals is not None:
                    approvals.discard(approval_token)
                return None
            valor, motivo = self.pode_comprar()
            if valor <= 0:
                if approvals is not None:
                    approvals.discard(approval_token)
                self.bot.acao(f"{simbolo}: aprovado ({analise['pontuacao']}) mas nao comprado - {motivo}")
                return None
            plan = self.preparar_execucao(mint, par, valor)
            plan_fields = plan.fields()
            bloqueio_plano = self.bloqueio_plano_financeiro(plan)
            if bloqueio_plano:
                if approvals is not None:
                    approvals.discard(approval_token)
                return None
            approval_par = dict(par)
            approval_par["_paper_execution_conditions"] = self.execution_conditions()
            approval_par["_paper_execution_plan"] = plan.canonical_json
            proposal_id = approvals.proposal_id_for_token(approval_token) if approvals is not None else None
            if approvals is None or not approvals.consume(approval_token, mint, approval_par, valor):
                self.bot.acao(f"{simbolo}: compra bloqueada - falta aprovacao manual valida e nao utilizada")
                return None
            if execution_plan is not None and execution_plan.canonical_json != plan.canonical_json:
                self.bot.acao(f"{simbolo}: compra bloqueada - plano aprovado diverge do Executor")
                return None
            preco = float(plan_fields["reference_price_usd"])
            preco_ef = float(plan_fields["effective_price_usd"])
            liq = float(plan_fields["liquidity_calc_usd"])
            tokens = float(plan_fields["net_tokens"])
            valor = float(plan_fields["debit_total_usd"])
            if tokens <= 0:
                return None
            candidato = copy.deepcopy(e)
            candidato["saldo"] -= valor
            pos = {
                "mint": mint, "simbolo": simbolo,
                "par": str(par.get("pairAddress", "")),
                "url": str(par.get("url", "")),
                "preco_entrada": preco,
                "preco_efetivo": preco_ef,
                "tokens": tokens, "custo": valor,
                "liq_entrada": liq, "liq_atual": liq,
                "preco_atual": preco, "pico": preco,
                "trailing": False, "aberta_em": agora(),
                "pontuacao": analise["pontuacao"], "sem_dados": 0,
            }
            candidato["posicoes"][mint] = pos
            linha = {
            "data_hora": data_txt(), "tipo": "COMPRA", "simbolo": simbolo, "mint": mint,
            "preco_mercado": plan_fields["reference_price_usd"],
            "preco_efetivo": plan_fields["effective_price_usd"],
            "valor_usd": plan_fields["debit_total_usd"], "tokens": plan_fields["net_tokens"],
            "custos_usd": plan_fields["costs_usd"], "pnl_usd": "", "pnl_pct": "",
            "motivo": "pontuacao", "pontuacao": analise["pontuacao"], "duracao_min": "",
            "saldo_depois": _decimal_canonico(candidato["saldo"]),
            "slippage_base_pct": plan_fields["slippage_base_pct"],
            "slippage_base_aplicado_pct": plan_fields["slippage_base_applied_pct"],
            "impacto_mercado_pct": plan_fields["market_impact_pct"] or "",
            "slippage_total_pct": plan_fields["slippage_total_pct"],
            "taxa_pct": plan_fields["taxa_pct"],
            "taxa_estimada_usd": plan_fields["estimated_fee_usd"],
            "debito_total_usd": plan_fields["debit_total_usd"],
            "quantidade_liquida": plan_fields["net_tokens"],
            "liquidez_usd": plan_fields["liquidity_observed_usd"] or "",
            "liquidez_estado": plan_fields["liquidity_status"],
            "fallback_execucao": "slippage_cap_95pct" if plan_fields["impact_fallback"] else "none",
            }
            authorization = {
                "capability": self._buy_commit_capability,
                "manual_confirmation_consumed": True,
                "proposal_id": proposal_id,
                "execution_plan": plan.canonical_json,
                "plan_fields": plan_fields,
            }
            operation_id = self.bot.paper_operations.commit(
                e, candidato, linha, "BUY", authorization=authorization,
                metadata={"proposal_id": authorization["proposal_id"]})
            self.bot._last_paper_operation_id = operation_id
            pos = e["posicoes"][mint]
        self.bot.acao(f"COMPRA {simbolo} debito=${plan_fields['debit_total_usd']} "
                      f"preco_efetivo=${plan_fields['effective_price_usd']} "
                      f"(pontuacao {analise['pontuacao']})")
        try:  # o aviso nunca pode estragar uma compra que ja ficou registada
            tgd = getattr(self.bot, "tg_decisoes", None)
            if tgd is not None and tgd.ativo and callable(tgd.vender) and MINT_VALIDO.fullmatch(mint):
                self.bot.telegram.enviar(
                    texto_compra(cfg, simbolo, mint, plan_fields["debit_total_usd"], preco_ef,
                                 analise.get("pontuacao"), liq, com_botao=True),
                    botoes=tgd.botoes_venda(mint),
                    ao_enviar=lambda mid, texto, chave="pos:" + mint: tgd.registar_mensagem(chave, mid, texto))
            else:
                self.bot.telegram.enviar(texto_compra(cfg, simbolo, mint, plan_fields["debit_total_usd"], preco_ef,
                                                      analise.get("pontuacao"), liq))
        except Exception:
            log.warning("Aviso de compra por Telegram falhou")
        return pos

    def anular(self, mint, motivo):
        """Desfaz uma posicao como se nunca tivesse sido aberta (devolve o valor investido).
        Nao conta para a taxa de acerto nem para o P&L."""
        e = self.estado
        with self._purchase_lock:
            candidato = copy.deepcopy(e)
            pos = candidato["posicoes"].pop(mint, None)
            if pos is None:
                return False
            candidato["saldo"] += pos["custo"]
            for sinal in reversed(candidato["sinais"]):
                if sinal.get("mint") == mint and sinal.get("estado") == "comprado":
                    sinal["estado"], sinal["saida"] = "anulado", motivo
                    break
            linha = {
            "data_hora": data_txt(), "tipo": "ANULADA", "simbolo": pos["simbolo"], "mint": mint,
            "preco_mercado": "", "preco_efetivo": "", "valor_usd": f"{pos['custo']:.4f}", "tokens": "",
            "custos_usd": "0", "pnl_usd": "0", "pnl_pct": "0", "motivo": motivo,
            "pontuacao": pos.get("pontuacao", ""), "duracao_min": "",
            "saldo_depois": f"{candidato['saldo']:.4f}",
            }
            self.bot.paper_operations.commit(e, candidato, linha, "CANCEL")
        return True

    def vender(self, mint, preco, liquidez, motivo, perda_total=False):
        cfg, e = self.cfg, self.estado
        with self._purchase_lock:
            candidato = copy.deepcopy(e)
            pos = candidato["posicoes"].pop(mint, None)
            if pos is None:
                return None
            if perda_total:
                receita, preco_ef, custos = 0.0, 0.0, 0.0
            else:
                receita, preco_ef, custos = simular_venda(cfg, pos, preco, liquidez)
            candidato["saldo"] += receita
            pnl = receita - pos["custo"]
            pnl_pct = pnl / pos["custo"] * 100.0 if pos["custo"] else 0.0
            duracao = (agora() - pos["aberta_em"]) / 60.0
            st = candidato["stats"]
            st["fechados"] += 1
            st["ganhos" if pnl > 0 else "perdas"] += 1
            st["pnl_total"] += pnl
            st["custos_total"] += custos + (pos["custo"] - pos["tokens"] * pos["preco_entrada"])
            st["melhor"] = max(st["melhor"], pnl_pct)
            st["pior"] = min(st["pior"], pnl_pct)
            chave = motivo.split(" ")[0]
            pm = st["por_motivo"].setdefault(chave, {"n": 0, "pnl": 0.0})
            pm["n"] += 1
            pm["pnl"] += pnl
            _fechar_sinal_no_estado(candidato, mint, pnl_pct, motivo)
            linha = {
            "data_hora": data_txt(), "tipo": "VENDA", "simbolo": pos["simbolo"], "mint": mint,
            "preco_mercado": preco_txt(preco), "preco_efetivo": preco_txt(preco_ef),
            "valor_usd": f"{receita:.4f}", "tokens": f"{pos['tokens']:.4f}",
            "custos_usd": f"{custos:.4f}", "pnl_usd": f"{pnl:.4f}", "pnl_pct": f"{pnl_pct:.2f}",
            "motivo": motivo, "pontuacao": pos.get("pontuacao", ""), "duracao_min": f"{duracao:.1f}",
            "saldo_depois": f"{candidato['saldo']:.4f}",
            }
            self.bot.paper_operations.commit(e, candidato, linha, "SELL")
        self.bot.acao(f"VENDA {pos['simbolo']} [{motivo}] P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)")
        try:  # o aviso nunca pode estragar uma venda que ja ficou registada
            self.bot.telegram.enviar(texto_venda(pos["simbolo"], motivo, pnl, pnl_pct, duracao, e["saldo"]))
        except Exception:
            log.warning("Aviso de venda por Telegram falhou")
        return pnl


# ============================================================================
# AGENTE 4 - RISCO
# ============================================================================
class Risco:
    """Vigia as posicoes abertas e decide quando vender."""

    def __init__(self, cfg, estado, http, executor):
        self.cfg = cfg
        self.estado = estado
        self.http = http
        self.executor = executor
        self.observability = None
        self.data_source = "DexScreener"
        self._last_observed_pair = {}
        # mint -> {"preco", "ts", ...}: preco de venda real (Jupiter) lido pelo Bot.ciclo_rapido
        self.precos_rapidos = {}

    @staticmethod
    def _condition(conditions, name, observed, raw, threshold, result, order, reason=None):
        conditions.append({
            "condition_name": name,
            "observed_value": observed,
            "raw_observed_value": raw,
            "threshold_value": threshold,
            "result": result,
            "evaluation_order": order,
            "reason": reason,
        })

    @staticmethod
    def _mark_remaining(conditions, reason, cfg):
        already = {item["condition_name"] for item in conditions}
        thresholds = (
            ("liquidity_minimum", cfg["rug_liquidez_minima_usd"]),
            ("liquidity_drop_pct", cfg["rug_queda_liquidez_pct"]),
            ("sudden_price_drop", cfg["rug_queda_preco_pct"]),
            ("stop_loss", cfg["stop_loss_pct"]),
            ("take_profit", cfg["take_profit_pct"]),
            ("trailing_stop", cfg["trailing_distancia_pct"]),
            ("max_position_time", cfg["tempo_maximo_min"]),
        )
        for order, (name, threshold) in enumerate(thresholds, 1):
            if name not in already:
                Risco._condition(conditions, name, None, None, threshold,
                                 "not_evaluated", order, reason)

    @staticmethod
    def _optional_number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _unavailable_conditions(cfg, reason):
        rules = (
            ("liquidity_minimum", cfg["rug_liquidez_minima_usd"], 1),
            ("liquidity_drop_pct", cfg["rug_queda_liquidez_pct"], 2),
            ("sudden_price_drop", cfg["rug_queda_preco_pct"], 3),
            ("stop_loss", cfg["stop_loss_pct"], 4),
            ("take_profit", cfg["take_profit_pct"], 5),
            ("trailing_stop", cfg["trailing_distancia_pct"], 6),
            ("max_position_time", cfg["tempo_maximo_min"], 7),
        )
        result = [{"condition_name": name, "observed_value": None,
                   "raw_observed_value": None, "threshold_value": threshold,
                   "result": "unavailable", "evaluation_order": order, "reason": reason}
                  for name, threshold, order in rules]
        result.append({"condition_name": "pair_change", "observed_value": None,
                       "raw_observed_value": None, "threshold_value": None,
                       "result": "unavailable", "evaluation_order": 0,
                       "reason": "sem observação para determinar mudança de par"})
        result.append({"condition_name": "missing_or_invalid_data", "observed_value": None,
                       "raw_observed_value": None, "threshold_value": None,
                       "result": "unavailable", "evaluation_order": 8, "reason": reason})
        return result

    def registrar_indisponibilidade(self, reason):
        """Record a failed already-fetched quote response without retrying it."""
        for mint, pos in list(self.estado["posicoes"].items()):
            try:
                self._emit_observation(pos, mint,
                                       conditions=self._unavailable_conditions(self.cfg, reason),
                                       unavailable_reason=reason)
            except Exception:
                try:
                    self.observability.note_caller_error()
                except Exception:
                    pass

    def _emit_observation(self, pos, mint, *, pair=None, raw_price=None, raw_liquidity=None,
                          conditions=None, exit_reason=None, unavailable_reason=None,
                          previous_pair_address=None, data_source=None):
        observer = self.observability
        if observer is None:
            return
        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            price = self._optional_number(raw_price)
            liquidity = self._optional_number(raw_liquidity)
            pair_address = pair.get("pairAddress") if isinstance(pair, dict) else None
            pair_address = pair_address if isinstance(pair_address, str) and pair_address else None
            entry_pair = pos.get("par") or None
            pair_condition = next((c for c in (conditions or [])
                                   if c.get("condition_name") == "pair_change"), None)
            pair_changed = bool(pair_condition and pair_condition.get("result") == "triggered")
            missing = []
            invalid = []
            for field, raw_value, parsed in (("priceUsd", raw_price, price),
                                             ("liquidity.usd", raw_liquidity, liquidity)):
                if raw_value is None:
                    missing.append(field)
                elif parsed is None:
                    invalid.append(field)
            if pair_address is None:
                missing.append("pairAddress")
            if unavailable_reason:
                missing.append("pair_data")
            incomplete = bool(missing or invalid)
            tokens = self._optional_number(pos.get("tokens"))
            position_value = self._optional_number(price * tokens) if price is not None and tokens is not None else None
            observer.record_evaluation({
                "event_id": str(uuid.uuid4()),
                "evaluated_at_utc": stamp,
                "mint": mint,
                "position_id": f"{mint}:{pos.get('aberta_em')}" if pos.get("aberta_em") is not None else None,
                "entry_price": self._optional_number(pos.get("preco_entrada")),
                "observed_price": price,
                "entry_liquidity_usd": self._optional_number(pos.get("liq_entrada")),
                "observed_liquidity_usd": liquidity,
                "entry_cost_usd": self._optional_number(pos.get("custo")),
                "token_quantity": self._optional_number(pos.get("tokens")),
                "position_value_usd": position_value,
                "pair_address": pair_address,
                "entry_pair_address": entry_pair,
                "previous_pair_address": previous_pair_address,
                "data_source": data_source or self.data_source,
                # DexScreener pair data here has no source-update timestamp or RPC slot.
                "source_timestamp_utc": None,
                "observation_age_seconds": None,
                "rpc_slot": None,
                "missing_fields": missing,
                "invalid_fields": invalid,
                "quality_status": ("simulated" if self.data_source == "demo_simulated" else
                                    "unavailable" if unavailable_reason else "incomplete" if incomplete else "valid"),
                "freshness_status": ("not_applicable" if self.data_source == "demo_simulated" else
                                     "unknown_source_timestamp" if pair is not None else "unavailable"),
                "pair_changed": pair_changed,
                "data_status": "unavailable" if unavailable_reason else "incomplete" if incomplete else "available",
                "priority_condition": next((c["condition_name"] for c in (conditions or [])
                                            if c.get("condition_name") in {
                                                "liquidity_minimum", "liquidity_drop_pct",
                                                "sudden_price_drop", "stop_loss", "take_profit",
                                                "trailing_stop", "max_position_time"}
                                            and c.get("result") == "triggered"), None),
                "exit_reason": exit_reason,
                "sem_dados_count": pos.get("sem_dados"),
                "conditions": conditions or [],
            })
        except Exception:
            # Keep even diagnostic failures off the synchronous risk/logging path.
            try:
                observer.note_caller_error()
            except Exception:
                pass

    def decidir(self, pos, preco, liq, conditions=None, raw_price=None, raw_liquidity=None):
        """Devolve o motivo de venda ou None para manter a posicao."""
        conditions = [] if conditions is None else conditions
        cfg = self.cfg
        variacao = (preco / pos["preco_entrada"] - 1) * 100.0 if pos["preco_entrada"] > 0 else 0.0
        anterior = num(pos.get("preco_atual"), preco)
        # 1) Rug: a liquidez desapareceu ou o preco caiu a pique
        if liq < cfg["rug_liquidez_minima_usd"]:
            self._condition(conditions, "liquidity_minimum", liq, raw_liquidity,
                            cfg["rug_liquidez_minima_usd"], "triggered", 1)
            self._mark_remaining(conditions, "liquidity_minimum acionada antes desta regra", cfg)
            return f"RUG liquidez ${liq:,.0f}"
        self._condition(conditions, "liquidity_minimum", liq, raw_liquidity,
                        cfg["rug_liquidez_minima_usd"], "not_triggered", 1)
        if pos["liq_entrada"] > 0 and liq < pos["liq_entrada"] * (1 - cfg["rug_queda_liquidez_pct"] / 100.0):
            drop = 100 - liq / pos["liq_entrada"] * 100
            self._condition(conditions, "liquidity_drop_pct", drop, raw_liquidity,
                            cfg["rug_queda_liquidez_pct"], "triggered", 2)
            self._mark_remaining(conditions, "liquidity_drop_pct acionada antes desta regra", cfg)
            return f"RUG liquidez caiu {100 - liq / pos['liq_entrada'] * 100:.0f}%"
        if pos["liq_entrada"] > 0:
            drop = 100 - liq / pos["liq_entrada"] * 100
            self._condition(conditions, "liquidity_drop_pct", drop, raw_liquidity,
                            cfg["rug_queda_liquidez_pct"], "not_triggered", 2)
        else:
            self._condition(conditions, "liquidity_drop_pct", None, raw_liquidity,
                            cfg["rug_queda_liquidez_pct"], "unavailable", 2,
                            "liquidez de entrada não disponível")
        if anterior > 0 and preco < anterior * (1 - cfg["rug_queda_preco_pct"] / 100.0):
            drop = (1 - preco / anterior) * 100
            self._condition(conditions, "sudden_price_drop", drop, raw_price,
                            cfg["rug_queda_preco_pct"], "triggered", 3)
            self._mark_remaining(conditions, "sudden_price_drop acionada antes desta regra", cfg)
            return f"RUG preco caiu {100 - preco / anterior * 100:.0f}% de repente"
        if anterior > 0:
            drop = (1 - preco / anterior) * 100
            self._condition(conditions, "sudden_price_drop", drop, raw_price,
                            cfg["rug_queda_preco_pct"], "not_triggered", 3,
                            "comparação usa preço atual como anterior se não existir amostra anterior" if pos.get("preco_atual") is None else None)
        else:
            self._condition(conditions, "sudden_price_drop", None, raw_price,
                            cfg["rug_queda_preco_pct"], "unavailable", 3, "preço anterior inválido")
        # 2) Stop loss
        if variacao <= cfg["stop_loss_pct"]:
            self._condition(conditions, "stop_loss", variacao, raw_price,
                            cfg["stop_loss_pct"], "triggered", 4)
            self._mark_remaining(conditions, "stop_loss acionado antes desta regra", cfg)
            return f"STOP_LOSS {variacao:+.1f}%"
        self._condition(conditions, "stop_loss", variacao, raw_price,
                        cfg["stop_loss_pct"], "not_triggered", 4)
        # 3) Take profit
        if cfg["take_profit_pct"] and variacao >= cfg["take_profit_pct"]:
            self._condition(conditions, "take_profit", variacao, raw_price,
                            cfg["take_profit_pct"], "triggered", 5)
            self._mark_remaining(conditions, "take_profit acionado antes desta regra", cfg)
            return f"TAKE_PROFIT {variacao:+.1f}%"
        self._condition(conditions, "take_profit", variacao, raw_price,
                        cfg["take_profit_pct"], "not_triggered" if cfg["take_profit_pct"] else "not_evaluated", 5,
                        None if cfg["take_profit_pct"] else "take profit desativado")
        # 4) Trailing stop
        pico = max(num(pos.get("pico")), preco)
        ganho_pico = (pico / pos["preco_entrada"] - 1) * 100.0 if pos["preco_entrada"] > 0 else 0.0
        if cfg["trailing_ativar_pct"] and (pos.get("trailing") or ganho_pico >= cfg["trailing_ativar_pct"]):
            pos["trailing"] = True
            if preco <= pico * (1 - cfg["trailing_distancia_pct"] / 100.0):
                drawdown = (1 - preco / pico) * 100 if pico > 0 else None
                self._condition(conditions, "trailing_stop", drawdown, raw_price,
                                cfg["trailing_distancia_pct"], "triggered", 6)
                self._mark_remaining(conditions, "trailing_stop acionado antes desta regra", cfg)
                return f"TRAILING {variacao:+.1f}% (pico {ganho_pico:+.1f}%)"
            drawdown = (1 - preco / pico) * 100 if pico > 0 else None
            self._condition(conditions, "trailing_stop", drawdown, raw_price,
                            cfg["trailing_distancia_pct"], "not_triggered", 6)
        elif cfg["trailing_ativar_pct"]:
            self._condition(conditions, "trailing_stop", ganho_pico, raw_price,
                            cfg["trailing_ativar_pct"], "not_triggered", 6,
                            "ganho ainda não atingiu o limiar de ativação")
        else:
            self._condition(conditions, "trailing_stop", None, raw_price,
                            cfg["trailing_distancia_pct"], "not_evaluated", 6, "trailing stop desativado")
        # 5) Tempo maximo
        minutos = (agora() - pos["aberta_em"]) / 60.0
        if minutos >= cfg["tempo_maximo_min"]:
            self._condition(conditions, "max_position_time", minutos, None,
                            cfg["tempo_maximo_min"], "triggered", 7)
            return f"TEMPO {minutos:.0f}min ({variacao:+.1f}%)"
        self._condition(conditions, "max_position_time", minutos, None,
                        cfg["tempo_maximo_min"], "not_triggered", 7)
        return None

    def _preco_rapido_valido(self, mint):
        info = self.precos_rapidos.get(mint)
        if not isinstance(info, dict) or num(info.get("preco")) <= 0:
            return None
        if agora() - num(info.get("ts")) > num(self.cfg.get("preco_rapido_validade_seg"), 12):
            return None
        return num(info.get("preco"))

    def verificar(self, pares=None, rapido=False):
        """Atualiza os precos e fecha o que for preciso. Devolve o n.o de vendas.

        rapido=True: chamada pelo ciclo rapido (Jupiter) com os ultimos pares da DexScreener ja lidos;
        nesse modo nunca conta falhas de dados (isso fica so para as leituras reais da DexScreener)."""
        posicoes = self.estado["posicoes"]
        if not posicoes:
            return 0
        if pares is None:
            pares = buscar_pares(self.http, list(posicoes.keys()))
        if pares is None:
            log.warning("Risco: sem resposta da API de precos; tento outra vez no proximo ciclo.")
            self.registrar_indisponibilidade("API sem resposta")
            return 0
        vendas = 0
        for mint in list(posicoes.keys()):
            pos = posicoes[mint]
            try:
                lista = pares.get(mint)
                if rapido and (lista is None or self._preco_rapido_valido(mint) is None):
                    continue
                if lista is None:  # sem resposta da API: nao conta como "o par desapareceu"
                    conditions = self._unavailable_conditions(self.cfg, "mint ausente na resposta")
                    self._emit_observation(pos, mint, conditions=conditions,
                                           unavailable_reason="mint sem resposta")
                    continue
                # prefere o mesmo par em que "comprou"; se nao existir, o de maior liquidez
                par = next((p for p in lista if str(p.get("pairAddress", "")) == pos["par"]
                            and num(p.get("priceUsd")) > 0), None) or melhor_par(lista)
                if par is None and rapido:
                    continue
                if par is None:
                    pos["sem_dados"] = int(num(pos.get("sem_dados"))) + 1
                    reason = None
                    if pos["sem_dados"] >= self.cfg["falhas_sem_dados_max"]:
                        reason = "RUG par desapareceu (perda total)"
                    conditions = self._unavailable_conditions(self.cfg, "nenhum par com preço positivo; regras não avaliadas")
                    missing_condition = next(c for c in conditions if c["condition_name"] == "missing_or_invalid_data")
                    missing_condition.update(observed_value=pos["sem_dados"],
                                             threshold_value=self.cfg["falhas_sem_dados_max"],
                                             result="triggered" if reason else "not_triggered",
                                             reason=reason or "contador de falhas ainda abaixo do limite")
                    self._emit_observation(pos, mint, conditions=conditions, exit_reason=reason,
                                           unavailable_reason="nenhum par com preço utilizável")
                    if reason:
                        self.executor.vender(mint, 0.0, 0.0, reason, perda_total=True)
                        vendas += 1
                    continue
                if not rapido:
                    pos["sem_dados"] = 0
                    pos["preco_ds"] = num(par.get("priceUsd"))
                raw_price = par.get("priceUsd")
                fonte_preco = "dexscreener"
                preco_jup = self._preco_rapido_valido(mint)
                if preco_jup is not None:  # preco de venda real e mais recente do que a DexScreener
                    raw_price = preco_jup
                    fonte_preco = "jupiter"
                raw_liquidity = obter(par, "liquidity", "usd", defeito=None)
                preco = num(raw_price)
                liq = num(raw_liquidity)
                conditions = []
                pair_address = par.get("pairAddress")
                previous_pair = self._last_observed_pair.get(mint, pos.get("par"))
                pair_changed = bool(pair_address and previous_pair and str(pair_address) != str(previous_pair))
                if pair_address:
                    self._last_observed_pair[mint] = str(pair_address)
                conditions.append({"condition_name": "pair_change", "observed_value": 1 if pair_changed else 0,
                                   "raw_observed_value": str(pair_address) if pair_address else None,
                                   "threshold_value": 1, "result": "triggered" if pair_changed else "not_triggered",
                                   "evaluation_order": 0,
                                   "reason": "observational event only; not a trading trigger"})
                motivo = self.decidir(pos, preco, liq, conditions, raw_price, raw_liquidity)
                missing_now = (self._optional_number(raw_price) is None or
                               self._optional_number(raw_liquidity) is None or
                               not isinstance(pair_address, str) or not pair_address)
                conditions.append({"condition_name": "missing_or_invalid_data",
                                   "observed_value": None, "raw_observed_value": None,
                                   "threshold_value": None,
                                   "result": "unavailable" if missing_now else "not_triggered",
                                   "evaluation_order": 8,
                                   "reason": "um ou mais campos da observação estão ausentes/inválidos" if missing_now else None})
                self._emit_observation(pos, mint, pair=par, raw_price=raw_price,
                                       raw_liquidity=raw_liquidity, conditions=conditions,
                                       exit_reason=motivo, previous_pair_address=previous_pair,
                                       data_source=("Jupiter+DexScreener" if fonte_preco == "jupiter"
                                                    and self.data_source == "DexScreener" else None))
                pos["preco_atual"] = preco
                pos["liq_atual"] = liq
                pos["fonte_preco"] = fonte_preco
                pos["pico"] = max(num(pos.get("pico")), preco)
                hist = pos.setdefault("hist", [pos["preco_entrada"]])  # para o grafico da plataforma
                hist_t = pos.get("hist_t")
                if not isinstance(hist_t, list) or len(hist_t) != len(hist):
                    hist_t = pos["hist_t"] = [round(num(pos.get("aberta_em"), agora()), 1)] * len(hist)
                hist.append(preco)
                hist_t.append(round(agora(), 1))
                del hist[:-600]
                del hist_t[:-600]
                if motivo:
                    self.executor.vender(mint, preco, liq, motivo)
                    vendas += 1
            except Exception:
                log.exception("Erro a verificar a posicao %s", mint)
        return vendas


# ============================================================================
# PLATAFORMA WEB - pagina com os sinais em direto (so leitura)
# ============================================================================
MINT_VALIDO = re.compile(r"[A-Za-z0-9]{30,50}")
IMG_VALIDA = re.compile(r"https://[a-z0-9.-]*dexscreener\.com/[\w\-./?=&%]{1,300}")


def faisca_do_par(par):
    """Pontos para o mini-grafico: preco relativo ha 24h, 6h, 1h, 5min e agora (dados da DexScreener)."""
    pontos = []
    for chave in ("h24", "h6", "h1", "m5"):
        v = obter(par, "priceChange", chave)
        if v is not None and num(v) > -99.9:
            pontos.append(round(1.0 / (1.0 + num(v) / 100.0), 5))
    return pontos + [1.0]


def imagem_do_par(par):
    """Icone do token (so aceita enderecos da propria DexScreener)."""
    url = obter(par, "info", "imageUrl", defeito="")
    return url if isinstance(url, str) and IMG_VALIDA.fullmatch(url) else ""

PAGINA_HTML = r'''<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#041011">
<title>MemeBot · Scanner de Meme Coins</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%23f7a531'/%3E%3Crect x='8' y='11' width='16' height='12' rx='4' fill='%23041011'/%3E%3Ccircle cx='13' cy='17' r='1.6' fill='%23f7a531'/%3E%3Ccircle cx='19' cy='17' r='1.6' fill='%23f7a531'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:ital,wght@0,400;0,500;0,600;0,700;1,800&display=swap" rel="stylesheet">
<style>
:root{--bg:#041011;--lin:rgba(94,234,212,.15);--txt:#f4f7f6;--mut:#93a8a6;--tq:#22e0c8;--ou:#f7a531;--ou2:rgba(247,165,49,.15);
--vd:#2ee6a8;--vm:#ff6b6b;--vm2:rgba(255,107,107,.15);--R:min(100%,440px)}
*{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--txt);font:15px/1.4 Poppins,Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-variant-numeric:tabular-nums;min-height:100vh;overflow-x:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
svg.i{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
.ceu{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden;background:radial-gradient(60% 50% at 45% 55%,rgba(18,92,90,.35),transparent 70%)}
.ceu i{position:absolute;border-radius:50%;filter:blur(120px);animation:deriva 24s ease-in-out infinite alternate}
.ceu i:nth-child(1){width:50vw;height:50vw;left:-12vw;top:-22vw;background:rgba(34,224,200,.16)}
.ceu i:nth-child(2){width:46vw;height:46vw;right:-16vw;bottom:-24vw;background:rgba(247,165,49,.15);animation-duration:30s;animation-direction:alternate-reverse}
.ceu svg{position:absolute;left:0;bottom:0;width:100%;height:42vh;opacity:.5}
.ceu path{fill:none;stroke-width:1;animation:ondula 9s ease-in-out infinite alternate}
.ceu path:nth-child(2){animation-delay:-3s}.ceu path:nth-child(3){animation-delay:-6s}
@keyframes ondula{to{transform:translateY(-14px)}}
@keyframes deriva{to{transform:translate3d(8%,10%,0) scale(1.2)}}
@keyframes roda{to{transform:rotate(360deg)}}
@keyframes onda{from{transform:scale(.5);opacity:.9}to{transform:scale(2);opacity:0}}
@keyframes surge{from{opacity:0;transform:translateY(10px)}}
.app{position:relative;z-index:1;display:grid;min-height:100vh}
main{padding:calc(16px + env(safe-area-inset-top)) 14px calc(96px + env(safe-area-inset-bottom));min-width:0;max-width:1560px;width:100%;margin:0 auto;overflow-x:clip}
/* ---------- menu ---------- */
.lado{display:none}
.marca{text-align:left;margin:0 0 26px 6px;position:relative}
.mascote{display:block;object-fit:contain;flex:none;filter:drop-shadow(0 0 10px rgba(46,230,168,.42))}
.marca .mascote{width:56px;height:67px;margin-bottom:5px}
.mascote-radar{width:66%;height:66%;filter:drop-shadow(0 0 8px rgba(46,230,168,.5))}
.pro{position:absolute;left:66px;top:10px;display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:var(--ou);padding:3px 11px;border-radius:99px;border:1px solid rgba(247,165,49,.6);background:var(--ou2)}
.marca b{display:block;font-size:27px;font-weight:700;letter-spacing:-.02em;line-height:1.1;margin-top:2px}
.marca small{color:var(--ou);font-size:14.5px}
.nav{display:grid;gap:5px}
.nav button{display:flex;align-items:center;gap:16px;padding:13px 18px;border-radius:16px;color:#dfe7e6;font-size:15.5px;text-align:left;border:1.5px solid transparent;transition:all .25s}
.nav svg{width:25px;height:25px;color:var(--mut);transition:color .25s}
.nav button.on{font-weight:600;color:#fff;background:linear-gradient(90deg,rgba(247,165,49,.34),rgba(247,165,49,.08));border-color:rgba(255,196,110,.9);box-shadow:0 0 26px rgba(247,165,49,.42),inset 0 0 18px rgba(247,165,49,.18)}
.nav button.on svg{color:var(--ou)}
@media(hover:hover){.nav button:not(.on):hover{background:rgba(94,234,212,.06)}}
.nav em,.fundo em{font-style:normal;font-size:12.5px;font-weight:700;color:#1a1203;background:var(--ou);border-radius:8px;padding:1px 9px;margin-left:auto;box-shadow:0 0 12px rgba(247,165,49,.6)}
.fundo{position:fixed;left:8px;right:8px;bottom:calc(8px + env(safe-area-inset-bottom));z-index:5;display:flex;gap:2px;padding:5px;border-radius:20px;background:rgba(5,16,17,.9);border:1px solid var(--lin);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);overflow-x:auto;scrollbar-width:none}
.fundo::-webkit-scrollbar{display:none}
.fundo button{flex:1 1 0;min-width:0;display:grid;justify-items:center;gap:2px;padding:7px 0;border-radius:13px;font-size:clamp(8.5px,2.35vw,11px);letter-spacing:-.02em;font-weight:500;color:var(--mut);position:relative;transition:all .25s}
.fundo svg{width:21px;height:21px}
.fundo button.on{color:var(--ou);background:var(--ou2);box-shadow:inset 0 0 0 1px rgba(247,165,49,.5)}
.fundo em{position:absolute;top:1px;right:2px;font-size:10px;padding:0 5px;margin:0}
/* ---------- cabecalho ---------- */
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
.logo-m{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;margin-bottom:8px}
.logo-m .mascote{width:25px;height:30px}
.logo-m span{font-size:10.5px;font-weight:600;color:var(--ou);padding:1px 8px;border-radius:99px;border:1px solid rgba(247,165,49,.6)}
h1{font-size:clamp(26px,7.4vw,43px);font-weight:800;font-style:italic;letter-spacing:-.02em;line-height:1.05}
h1 em{color:var(--ou);text-shadow:0 0 30px rgba(247,165,49,.5)}
.sub{color:#c3d0ce;font-size:clamp(13.5px,1.25vw,18px);margin-top:4px}
.chips{display:flex;gap:10px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:9px;padding:9px 17px;border-radius:99px;font-size:14.5px;font-weight:500;border:1px solid rgba(94,234,212,.28);background:rgba(6,18,19,.7);white-space:nowrap}
.chip.vivo{color:var(--ou);border-color:rgba(247,165,49,.75);box-shadow:0 0 18px rgba(247,165,49,.22),inset 0 0 12px rgba(247,165,49,.1)}
.chip.fora{color:var(--vm);border-color:rgba(255,107,107,.5)}
.chip.demo{color:#1a1203;background:var(--ou);border-color:var(--ou);font-weight:600}
.chip.modo{color:var(--vd);border-color:rgba(46,230,168,.45);background:rgba(6,18,19,.7);font-weight:600}
.chip svg{width:19px;height:19px;color:var(--ou)}
.chip.vivo svg,.gira{animation:roda 5s linear infinite}
.pt{width:11px;height:11px;border-radius:50%;background:currentColor;position:relative;box-shadow:0 0 10px currentColor}
.vivo .pt::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:1.5px solid currentColor;animation:onda 1.8s ease-out infinite}
.sol{width:22px;height:17px;background:linear-gradient(180deg,#9945ff 0 26%,transparent 26% 37%,#7b8cf0 37% 63%,transparent 63% 74%,#14f195 74%);transform:skewX(-20deg);border-radius:2px}
/* ---------- cartoes ---------- */
.cx{background:linear-gradient(180deg,rgba(14,30,31,.86),rgba(6,16,17,.9));border:1px solid var(--lin);border-radius:20px;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:16px}
.st{display:flex;align-items:center;gap:13px;padding:13px 15px;min-width:0;--k:var(--tq);border:1.5px solid color-mix(in srgb,var(--k) 55%,transparent);
background:linear-gradient(120deg,color-mix(in srgb,var(--k) 16%,rgba(8,20,21,.92)),rgba(6,16,17,.92) 60%);box-shadow:0 0 22px color-mix(in srgb,var(--k) 14%,transparent),inset 0 0 26px color-mix(in srgb,var(--k) 8%,transparent)}
.st>svg{width:42px;height:42px;color:var(--k);filter:drop-shadow(0 0 10px var(--k));stroke-width:1.6}
.st div{min-width:0;flex:1}
.st b{display:block;font-size:clamp(20px,2vw,26px);font-weight:700;letter-spacing:-.01em;line-height:1.15}
.st span{font-size:13px;color:#cfd9d7;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st .ex{flex:none;color:var(--k);opacity:.9}
.st .ex svg{width:24px;height:24px}
.vista{display:none}.vista.on{display:block;animation:surge .45s ease both}
.palco{display:grid;grid-template-columns:minmax(0,1fr);grid-template-areas:"radar" "lista" "detalhe" "melhor";gap:16px;margin-bottom:16px;align-items:start}
/* ---------- radar ---------- */
.zona{position:relative;display:grid;justify-items:center;align-content:start;gap:12px;min-width:0;padding:0}
.radar{position:relative;width:var(--R);aspect-ratio:1}
.globo{position:absolute;inset:3%;border-radius:50%;overflow:hidden;
background:radial-gradient(circle at 36% 28%,rgba(120,255,235,.34),rgba(16,84,84,.5) 42%,rgba(5,24,26,.92) 76%);
box-shadow:0 0 70px rgba(34,224,200,.32),inset 0 0 70px rgba(34,224,200,.26),inset -26px -30px 70px rgba(0,0,0,.6)}
.mapa{position:absolute;inset:0;background-image:radial-gradient(circle,rgba(140,255,238,.85) 1px,transparent 1.5px);background-size:8px 8px;opacity:.6;
-webkit-mask-image:radial-gradient(18% 14% at 30% 30%,#000 60%,transparent),radial-gradient(11% 17% at 36% 60%,#000 60%,transparent),radial-gradient(16% 11% at 58% 28%,#000 60%,transparent),radial-gradient(10% 15% at 56% 54%,#000 60%,transparent),radial-gradient(19% 13% at 76% 34%,#000 60%,transparent),radial-gradient(9% 7% at 82% 66%,#000 60%,transparent);
mask-image:radial-gradient(18% 14% at 30% 30%,#000 60%,transparent),radial-gradient(11% 17% at 36% 60%,#000 60%,transparent),radial-gradient(16% 11% at 58% 28%,#000 60%,transparent),radial-gradient(10% 15% at 56% 54%,#000 60%,transparent),radial-gradient(19% 13% at 76% 34%,#000 60%,transparent),radial-gradient(9% 7% at 82% 66%,#000 60%,transparent);
animation:gira-mapa 60s linear infinite}
@keyframes gira-mapa{to{background-position:160px 0}}
.globo svg{position:absolute;inset:0;width:100%;height:100%}
.globo svg *{fill:none;stroke:rgba(140,255,238,.34);stroke-width:.4;vector-effect:non-scaling-stroke}
.varre{position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 0deg,transparent 0 80%,rgba(247,165,49,.06) 84%,rgba(247,165,49,.55) 99.5%,#ffe2a8 100%);animation:roda 4s linear infinite}
.aro{position:absolute;inset:1.6%;border-radius:50%;background:conic-gradient(from 210deg,rgba(34,224,200,.75),rgba(247,165,49,1) 14%,rgba(34,224,200,.35) 30%,rgba(34,224,200,.9) 55%,rgba(247,165,49,.9) 68%,rgba(34,224,200,.5) 82%,rgba(34,224,200,.75));
-webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 3px),#000 calc(100% - 2.5px));mask:radial-gradient(farthest-side,transparent calc(100% - 3px),#000 calc(100% - 2.5px));animation:roda 16s linear infinite;filter:drop-shadow(0 0 6px rgba(247,165,49,.6))}
.nucleo{position:absolute;left:50%;top:50%;width:19%;aspect-ratio:1;transform:translate(-50%,-50%);border-radius:50%;display:grid;place-items:center;color:var(--ou);
background:rgba(4,14,15,.86);border:2px solid var(--ou);box-shadow:0 0 30px rgba(247,165,49,.75),inset 0 0 16px rgba(247,165,49,.35)}
.nucleo::after{content:"";position:absolute;inset:-2px;border-radius:50%;border:1.5px solid var(--ou);animation:onda 2.4s ease-out infinite}
.nucleo .robo{width:66%;height:auto;animation:none;filter:none}
.base{position:absolute;left:2%;right:2%;bottom:-13%;height:24%;pointer-events:none}
.base::before{content:"";position:absolute;left:22%;right:22%;top:-60%;bottom:50%;background:linear-gradient(180deg,transparent,rgba(247,165,49,.3));filter:blur(14px)}
.base i{position:absolute;inset:26% 17%;border-radius:50%;border:2px solid rgba(255,190,90,.95);box-shadow:0 0 26px rgba(247,165,49,.75),inset 0 0 26px rgba(247,165,49,.45);animation:respira 3.2s ease-in-out infinite}
.base i:nth-child(2){inset:14% 9%;border:1.5px solid rgba(34,224,200,.7);box-shadow:0 0 22px rgba(34,224,200,.4),inset 0 0 18px rgba(34,224,200,.2);animation-delay:.5s}
.base i:nth-child(3){inset:0;border:1px solid rgba(34,224,200,.35);box-shadow:0 0 26px rgba(34,224,200,.2);animation-delay:1s}
@keyframes respira{50%{opacity:.5;transform:scale(1.025)}}
.blips{position:absolute;inset:3%;pointer-events:none}
.blip-orbita{position:absolute;inset:0;z-index:2;pointer-events:none;transform-origin:50% 50%;transform:scaleY(.64) rotate(var(--fase));animation:orbita-moeda var(--dur) linear infinite}
.blip-orbita::before{content:"";position:absolute;left:50%;top:50%;width:var(--diametro);height:var(--diametro);border:1px solid color-mix(in srgb,var(--orb-cor) 20%,transparent);border-radius:50%;transform:translate(-50%,-50%);opacity:.62;pointer-events:none}
.blip{position:absolute;left:calc(50% + var(--raio));top:50%;width:26px;height:26px;margin:0;z-index:2;display:grid;place-items:center;padding:2px;border:1.5px solid var(--k);border-radius:50%;cursor:pointer;pointer-events:auto;color:#eafffb;background:radial-gradient(circle at 32% 25%,rgba(255,255,255,.24),rgba(10,28,31,.92) 62%);box-shadow:0 0 9px color-mix(in srgb,var(--k) 68%,transparent),inset 0 0 8px color-mix(in srgb,var(--k) 30%,transparent);transform:translate(-50%,-50%) rotate(var(--fase-neg)) scaleY(1.5625);animation:contra-orbita-moeda var(--dur) linear infinite;transition:filter .2s,box-shadow .2s}
.blip::before{content:"";position:absolute;inset:-5px;border:1px solid color-mix(in srgb,var(--k) 72%,transparent);border-radius:50%;box-shadow:0 0 9px color-mix(in srgb,var(--k) 40%,transparent);pointer-events:none;animation:blip-pulso 2.8s ease-in-out infinite}
.blip-img{display:block;width:100%;height:100%;border-radius:50%;object-fit:cover;background:#071514;pointer-events:none;transition:opacity .2s}
.blip-fallback{position:absolute;inset:0;display:grid;place-items:center;border-radius:50%;font-size:9px;font-weight:700;line-height:1;text-shadow:0 1px 3px #041011;pointer-events:none}
.blip:hover,.blip:focus-visible,.blip.selecionado{z-index:4;filter:brightness(1.22);box-shadow:0 0 0 4px color-mix(in srgb,var(--k) 30%,transparent),0 0 17px var(--k);outline:none}
.blip.analise{--k:#22e0c8}.blip.aprovada{--k:#f7a531}.blip.rejeitada{--k:#ff6b6b}.blip.entra{animation:contra-orbita-moeda var(--dur) linear infinite,blip-entrada .45s ease-out both}
@keyframes blip-pulso{50%{opacity:.45;transform:scale(1.12)}}
.blip-orbita.analise{--orb-cor:#22e0c8}.blip-orbita.aprovada{--orb-cor:#f7a531}.blip-orbita.rejeitada{--orb-cor:#ff6b6b}
.blip-orbita.destaque .blip::after{content:"✦";position:absolute;right:-5px;top:-5px;width:11px;height:11px;border:1px solid #f7a531;border-radius:50%;display:grid;place-items:center;background:#201806;color:#ffd788;font-size:8px;line-height:1;box-shadow:0 0 8px rgba(247,165,49,.55)}
.blip-cartao{position:absolute;left:calc(100% + 10px);top:50%;width:154px;display:grid;gap:5px;padding:8px 9px;border:1px solid color-mix(in srgb,var(--k) 46%,transparent);border-radius:11px;background:rgba(5,16,24,.94);box-shadow:0 8px 24px rgba(0,0,0,.34),0 0 16px color-mix(in srgb,var(--k) 13%,transparent);color:#eaf4f4;text-align:left;opacity:0;visibility:hidden;pointer-events:none;transform:translateY(-50%) translateX(4px);transition:opacity .16s,transform .16s,visibility .16s;z-index:8}
.blip-cartao-esq{left:auto;right:calc(100% + 10px);transform:translateY(-50%) translateX(-4px)}
.blip.cartao-visivel .blip-cartao{opacity:1;visibility:visible;transform:translateY(-50%);pointer-events:auto}
.blip-card-head{display:flex;align-items:center;justify-content:space-between;gap:5px;min-width:0}
.blip-identidade{min-width:0;display:grid;line-height:1.15}.blip-identidade b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;font-weight:650}.blip-identidade small{color:#9eb5b7;font-size:9px}
.blip-status{flex:none;padding:2px 5px;border-radius:99px;background:color-mix(in srgb,var(--k) 14%,transparent);color:var(--k);font-size:8px;white-space:nowrap}
.blip-card-grid{display:grid;grid-template-columns:1fr 1fr;gap:3px 8px}.blip-card-grid span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9eb5b7;font-size:8px}.blip-card-grid b{color:#edf5f4;font-size:9px;font-weight:550}
:where(.blip-card-grid) span{display:grid;line-height:1.25}.blip-card-grid b{overflow:hidden;text-overflow:ellipsis}
.blip-card-motivo{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;padding-top:4px;border-top:1px solid rgba(255,255,255,.08);color:#ffb3b3;font-size:8px;line-height:1.3;white-space:normal}
.blip-melhor{display:none;color:#ffd788;font-size:8px;white-space:nowrap}.blip-orbita.destaque .blip-melhor{display:inline}
@keyframes orbita-moeda{from{transform:scaleY(.64) rotate(var(--fase))}to{transform:scaleY(.64) rotate(calc(var(--fase) + 360deg))}}
@keyframes contra-orbita-moeda{from{transform:translate(-50%,-50%) rotate(var(--fase-neg)) scaleY(1.5625)}to{transform:translate(-50%,-50%) rotate(calc(var(--fase-neg) - 360deg)) scaleY(1.5625)}}
@keyframes blip-entrada{from{opacity:0;filter:blur(3px)}to{opacity:1;filter:none}}
.radar-legenda{display:flex;justify-content:center;flex-wrap:wrap;gap:7px 14px;width:min(100%,480px);font-size:12px;color:#cfd9d7}
.radar-legenda span{display:flex;align-items:center;gap:6px}.radar-legenda i{width:8px;height:8px;border-radius:50%;background:#22e0c8;box-shadow:0 0 8px currentColor}.radar-legenda .ok i{background:#f7a531}.radar-legenda .no i{background:#ff6b6b}
.bl{position:absolute;width:10px;height:10px;margin:-5px;border-radius:50%;background:var(--tq);opacity:.6;animation:pisca 4s linear infinite;--k:var(--tq)}
.bl.rej{width:9px;height:9px;margin:-4.5px;background:var(--vm);--k:var(--vm);opacity:.85}
.bl.sin{width:14px;height:14px;margin:-7px;background:var(--ou);--k:var(--ou);opacity:1}
.bl.abr{background:var(--tq);--k:var(--tq)}
.bl.sin::before{content:"";position:absolute;inset:-11px;background:linear-gradient(var(--k) 0 0) 0 0/9px 1.5px,linear-gradient(var(--k) 0 0) 0 0/1.5px 9px,linear-gradient(var(--k) 0 0) 100% 0/9px 1.5px,linear-gradient(var(--k) 0 0) 100% 0/1.5px 9px,linear-gradient(var(--k) 0 0) 0 100%/9px 1.5px,linear-gradient(var(--k) 0 0) 0 100%/1.5px 9px,linear-gradient(var(--k) 0 0) 100% 100%/9px 1.5px,linear-gradient(var(--k) 0 0) 100% 100%/1.5px 9px;background-repeat:no-repeat}
.bl.foco::after{content:"";position:absolute;inset:-6px;border-radius:50%;border:1.5px solid var(--k);animation:onda 1.6s ease-out infinite}
.bl.entra{animation:pisca 4s linear infinite,nasce .7s cubic-bezier(.2,1.6,.4,1) both}
@keyframes pisca{0%{box-shadow:0 0 0 0 var(--k),0 0 20px 7px var(--k);filter:brightness(1.9)}12%{box-shadow:0 0 0 7px transparent,0 0 9px 2px var(--k);filter:none}100%{box-shadow:0 0 8px 1px var(--k)}}
@keyframes nasce{from{transform:scale(0)}}
#fio{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible;display:none}
#fio line{stroke:var(--ou);stroke-width:1.5;filter:drop-shadow(0 0 4px var(--ou));stroke-dasharray:4 4;animation:corre 1s linear infinite}
@keyframes corre{to{stroke-dashoffset:-8}}
.aviso{position:relative;width:min(100%,440px);min-height:112px;margin:0;padding:16px 18px;border:1.5px solid rgba(255,190,90,.8);background:linear-gradient(180deg,rgba(24,24,16,.94),rgba(8,14,14,.95));box-shadow:0 0 34px rgba(247,165,49,.3),inset 0 0 22px rgba(247,165,49,.08);z-index:2}
.aviso.novo{animation:surge .5s ease both,clarao 2.4s ease-out both}
.radar-info{width:min(100%,440px);min-height:104px;padding:12px 15px;border-color:rgba(94,234,212,.26);transition:opacity .18s,transform .18s}
.radar-info h3{font-size:15px;margin-bottom:7px}.radar-info p{font-size:13px;color:#cfd9d7;overflow-wrap:anywhere}.radar-info .radar-meta{display:flex;flex-wrap:wrap;gap:5px 12px;margin-bottom:5px;font-size:13px}
.radar-info .radar-status{font-weight:700}.radar-info .radar-status.analise{color:var(--tq)}.radar-info .radar-status.aprovada{color:var(--ou)}.radar-info .radar-status.rejeitada{color:var(--vm)}.radar-info .radar-status.indisponivel{color:#aab4c4}
.radar-operacao{margin-top:10px;padding-top:10px;border-top:1px solid rgba(94,234,212,.2)}.radar-operacao h4{font-size:13px;margin-bottom:6px;color:var(--ou)}
.radar-operacao dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 12px;margin:0}.radar-operacao dl div{min-width:0}.radar-operacao dt{font-size:11px;color:var(--mut)}.radar-operacao dd{margin:0;font-size:12.5px;overflow-wrap:anywhere}
.trade-row{display:grid!important;grid-template-columns:minmax(0,1fr) auto;align-items:start!important;gap:5px 12px!important}.trade-row>div{display:grid;gap:3px;min-width:0}.trade-row strong{color:var(--txt);font-weight:600}.trade-row small{font-size:12px;overflow-wrap:anywhere}.trade-row .trade-result{text-align:right;color:var(--txt);font-weight:600;white-space:nowrap}
.aviso.saindo,.radar-info.saindo{opacity:.55;transform:translateY(3px)}
.scanner-list-title{display:flex;align-items:center;justify-content:space-between;padding:5px 4px 12px}.scanner-list-title h2{font-size:18px;font-weight:650}.scanner-list-title small{font-size:12px;color:var(--mut)}
.scanner-filtros{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px;margin-bottom:10px}
.scanner-filtros button{min-width:0;padding:8px 5px;border:1px solid rgba(94,234,212,.18);border-radius:10px;background:rgba(6,18,19,.52);font-size:12px;color:#cfd9d7;white-space:nowrap;transition:background .2s,border-color .2s,color .2s}
.scanner-filtros button.on{color:#fff;border-color:rgba(247,165,49,.65);background:rgba(247,165,49,.14)}
.lista-moedas{display:grid;gap:7px;max-height:min(64vh,690px);overflow:auto;padding:2px 2px 3px;scrollbar-width:thin;scrollbar-color:rgba(94,234,212,.3) transparent}
.moeda-linha{width:100%;min-width:0;display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"nome estado" "meta estado";gap:4px 8px;align-items:center;padding:10px 11px;border:1px solid rgba(94,234,212,.16);border-radius:13px;background:linear-gradient(120deg,rgba(10,27,28,.8),rgba(6,16,17,.82));text-align:left;color:var(--txt);cursor:pointer;transition:border-color .18s,background .18s,transform .18s}
.moeda-linha:hover,.moeda-linha:focus-visible{border-color:rgba(94,234,212,.55);background:rgba(15,40,40,.88);outline:none}
.moeda-linha.selecionada{border-color:rgba(247,165,49,.72);box-shadow:inset 3px 0 var(--ou),0 0 14px rgba(247,165,49,.12)}
.moeda-nome{grid-area:nome;display:flex;align-items:center;gap:7px;min-width:0}.moeda-nome b{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.moeda-nome small{font-size:11px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.moeda-nome em{font-style:normal;font-size:9px;color:var(--ou);border:1px solid rgba(247,165,49,.35);border-radius:20px;padding:1px 6px;flex:none}
.moeda-meta{grid-area:meta;display:flex;flex-wrap:wrap;gap:3px 12px;color:var(--mut);font-size:11px}.moeda-meta b{font-weight:550;color:#dfe7e6}
.estado-moeda{grid-area:estado;justify-self:end;align-self:center;font-size:10px;font-weight:650;padding:4px 7px;border-radius:20px;white-space:nowrap;color:var(--tq);background:rgba(34,224,200,.1)}
.estado-moeda.aprovada{color:var(--ou);background:rgba(247,165,49,.12)}.estado-moeda.rejeitada{color:var(--vm);background:rgba(255,107,107,.1)}.estado-moeda.indisponivel,.estado-moeda.historica,.estado-moeda.descoberta{color:#aab4c4;background:rgba(170,180,196,.1)}
.scanner-counter-panel{margin:0 0 10px;color:var(--mut);font-size:11px}.scanner-counter-panel summary{cursor:pointer;padding:5px 7px;border:1px solid rgba(170,180,196,.12);border-radius:8px}.scanner-counter-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 10px;padding:8px}.scanner-counter-grid span{display:flex;justify-content:space-between;gap:8px}.scanner-counter-grid b{color:var(--txt)}
.blip-orbita.indisponivel,.blip-orbita.historica,.blip-orbita.descoberta,.blip-orbita.sem_par,.blip-orbita.erro_tecnico{--orb-cor:#9aa6b7}.blip.indisponivel,.blip.historica,.blip.descoberta,.blip.sem_par,.blip.erro_tecnico{--k:#9aa6b7}
.scanner-col{grid-area:lista;display:grid;gap:12px;min-width:0}.zona{grid-area:radar}.radar-info{grid-area:detalhe;width:100%;max-width:none}.melhor{grid-area:melhor}
.aviso{width:100%;min-height:0;padding:12px 15px;transition:opacity .22s,transform .22s}
.aviso{padding-right:52px}
.aviso h3{margin-bottom:5px}.aviso p{font-size:12px;color:var(--mut)}.aviso .bt{width:max-content;margin-top:8px;padding:7px 12px;font-size:12px}
.aviso.saindo{opacity:0;transform:translateY(-5px)}
.radar-info[hidden],.aviso[hidden]{display:none!important}
.radar-info-head{position:relative;display:flex;align-items:center;min-height:32px;margin:0 0 7px;padding-right:38px}
.radar-info-head h3{margin:0;font-size:15px}
.scanner-fechar{position:absolute;top:8px;right:8px;z-index:3;display:grid;place-items:center;width:32px;height:32px;padding:0;border:1px solid rgba(147,168,166,.35);border-radius:10px;background:rgba(5,15,16,.82);color:#dfe7e6;font-size:23px;line-height:1;transition:color .18s,border-color .18s,background .18s}
.scanner-fechar:hover,.scanner-fechar:focus-visible{color:var(--ou);border-color:rgba(247,165,49,.7);background:rgba(247,165,49,.12);outline:2px solid rgba(247,165,49,.35);outline-offset:2px}
.radar-info-head .scanner-fechar{top:0;right:0}
@keyframes clarao{from{box-shadow:0 0 0 1px var(--ou),0 0 70px rgba(247,165,49,.7)}}
.aviso h3{font-size:16px;font-weight:600;display:flex;gap:9px;align-items:center;margin-bottom:10px}
.aviso h3::before{content:"✦";color:var(--ou);text-shadow:0 0 10px var(--ou)}
.duo{display:flex;align-items:center;gap:13px;min-width:0}
.duo>div:last-child{min-width:0}
.tok{font-size:24px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:9px;min-width:0;line-height:1.2}
.tok>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gr{font-size:24px;font-weight:700;line-height:1.2}.gr small{font-size:15px;font-weight:600}
.bt{display:flex;align-items:center;justify-content:center;gap:9px;margin-top:13px;padding:10px 14px;border-radius:99px;font-weight:600;font-size:14.5px;text-decoration:none;color:var(--txt);border:1.5px solid rgba(255,190,90,.85);background:rgba(247,165,49,.1);transition:background .25s,transform .15s}
.bt:active{transform:scale(.97)}
@media(hover:hover){.bt:hover{background:rgba(247,165,49,.26)}}
.bt.cheio{color:#1a1203;border:0;background:linear-gradient(180deg,#ffd166,#f59e0b);box-shadow:0 0 34px rgba(247,165,49,.6),inset 0 1px 0 rgba(255,255,255,.5);font-size:clamp(16px,1.5vw,20px);padding:17px 30px;margin:0;border-radius:20px;white-space:nowrap}
.sobe{color:var(--vd)}.desce{color:var(--vm)}
.av{width:var(--t,42px);height:var(--t,42px);border-radius:50%;flex:none;display:grid;place-items:center;font-weight:800;font-size:calc(var(--t,42px)*.42);color:#061211;position:relative;overflow:hidden;border:2px solid rgba(255,255,255,.16);box-shadow:0 0 12px rgba(0,0,0,.4)}
.av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.tag{font-size:12px;font-weight:600;padding:2px 9px;border-radius:8px;white-space:nowrap;flex:none}
.tag.novo{color:#fff;background:#c0453f}.tag.comprado{color:var(--vd);background:rgba(46,230,168,.15)}
.tag.sem_vaga,.tag.anulado,.tag.aguarda_confirmacao{color:var(--mut);background:rgba(147,168,166,.15)}.tag.aguarda_confirmacao{color:#ffd166;background:rgba(247,165,49,.1)}.tag.ganho{color:#04130d;background:var(--vd)}.tag.perda{color:var(--vm);background:var(--vm2)}
/* ---------- painel de listas ---------- */
.painel{padding:12px;min-width:0;border-color:rgba(94,234,212,.26);border-bottom-color:rgba(255,107,107,.4);border-right-color:rgba(255,107,107,.25)}
.manual-proposals{margin-top:10px;padding:12px;border-color:rgba(247,165,49,.45)}
.manual-proposals h3{font-size:14px;margin:0 0 8px;color:#ffd166}
.manual-proposal{display:grid;gap:3px;padding:9px 0;border-top:1px solid var(--lin);font-size:12px;overflow-wrap:anywhere}
.manual-proposal small{color:var(--mut)}
.abas{display:flex;gap:4px;margin-bottom:10px;align-items:center}
.abas .rolo{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0;padding:6px}
.abas .rolo::-webkit-scrollbar{display:none}
.abas button{display:flex;align-items:center;gap:9px;padding:11px 16px;border-radius:15px;font-weight:500;font-size:15px;color:#dfe7e6;white-space:nowrap;border:1.5px solid transparent;transition:all .25s}
.abas svg{width:21px;height:21px}
.abas button.on{font-weight:600;color:#fff;background:linear-gradient(180deg,rgba(247,165,49,.4),rgba(247,165,49,.12));border-color:rgba(255,196,110,.9);box-shadow:0 0 22px rgba(247,165,49,.35)}
.abas button.on svg{color:var(--ou)}
.abas .filtro{padding:11px 15px;border:1px solid rgba(94,234,212,.3);background:rgba(6,18,19,.7);flex:none}
.abas .filtro.on{background:var(--ou2)}
.ln{display:grid;grid-template-columns:30px auto minmax(0,1fr) 70px auto 70px;align-items:center;gap:12px;padding:9px 12px;border-radius:16px;border:1.5px solid transparent;color:inherit;text-decoration:none;min-width:0}
.ln+.ln{margin-top:2px}
.ln.topo{border-color:rgba(255,196,110,.9);background:linear-gradient(90deg,rgba(247,165,49,.26),rgba(247,165,49,.05));box-shadow:0 0 26px rgba(247,165,49,.28),inset 0 0 18px rgba(247,165,49,.08)}
.ln.novo-e{animation:surge .5s ease both}
@media(hover:hover){a.ln:not(.topo):hover{background:rgba(94,234,212,.07)}}
.rk{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-size:13.5px;font-weight:600;color:#cfd9d7;background:rgba(147,168,166,.16)}
.topo .rk{background:none;color:var(--ou);font-size:21px;text-shadow:0 0 10px var(--ou)}
.nm{min-width:0}.nm .tok{font-size:17.5px;font-weight:600}
.nm p{font-size:11.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sp{width:70px;height:26px;display:block}
.sp polyline{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.vr{font-weight:600;font-size:16.5px;text-align:right;white-space:nowrap}
.pn{display:flex;align-items:center;gap:8px;justify-content:flex-end;font-weight:500;font-size:15px}
.pn svg{width:32px;height:32px;transform:rotate(-90deg);flex:none}
.pn circle{fill:none;stroke-width:3.4}
.vazio{padding:34px 16px;text-align:center;color:var(--mut);font-size:14px}
/* ---------- melhor oportunidade ---------- */
.melhor{padding:16px 18px;border:1.5px solid rgba(255,190,90,.7);background:linear-gradient(180deg,rgba(22,24,18,.92),rgba(7,14,14,.94));box-shadow:0 0 44px rgba(247,165,49,.2),inset 0 0 34px rgba(247,165,49,.06);display:grid;gap:16px;align-items:center}
.melhor h3{font-size:16.5px;font-weight:600;margin-bottom:10px;display:flex;gap:9px;align-items:center}
.melhor h3 svg{width:24px;height:24px;color:var(--ou);filter:drop-shadow(0 0 6px var(--ou))}
.melhor .tok{font-size:clamp(26px,2.6vw,34px)}.melhor .gr{font-size:clamp(26px,2.6vw,34px)}.melhor .gr small{font-size:19px}
.melhor .tag{font-size:14px;padding:3px 11px}
.med{position:relative;width:150px;height:104px;margin:0 auto}
.med svg{width:150px;height:150px}
.med path{fill:none;stroke-width:10;stroke-linecap:round}
.med .arco{stroke:url(#gm);stroke-dasharray:251.3;stroke-dashoffset:251.3;transition:stroke-dashoffset 1.2s cubic-bezier(.2,.8,.2,1);filter:drop-shadow(0 0 6px rgba(247,165,49,.6))}
.med div{position:absolute;left:0;right:0;top:57px;text-align:center}
.med b{display:block;font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1}
.med span{font-size:13.5px;color:#cfd9d7}
.trio{display:grid;grid-template-columns:repeat(3,1fr);text-align:center}
.trio div{padding:4px 10px}.trio div+div{border-left:1px solid rgba(147,168,166,.22)}
.trio span{display:block;font-size:13.5px;color:#cfd9d7;white-space:nowrap}.trio b{font-size:clamp(17px,1.6vw,22px);font-weight:600}
.velas{width:100%;height:92px;display:block;min-width:0}
/* ---------- outras vistas ---------- */
.grelha{display:grid;gap:16px}
.bloco{padding:18px}
.rot{font-size:12px;color:var(--mut);letter-spacing:.05em;text-transform:uppercase;font-weight:600}
.valor{font-size:clamp(38px,11vw,58px);font-weight:700;letter-spacing:-.03em;line-height:1.05;margin:6px 0 8px}
.delta{display:inline-flex;font-weight:600;font-size:14px;padding:5px 10px;border-radius:10px}
.delta.sobe{background:rgba(46,230,168,.13)}.delta.desce{background:var(--vm2)}
.graf{width:100%;height:120px;display:block;margin-top:14px;overflow:visible}
.graf .lin{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.quad{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.quad .cx{padding:13px 15px}.quad b{display:block;font-size:20px;font-weight:600;margin-top:2px}
.painel-dados{padding:13px 15px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.painel-dados span{font-size:12px;color:var(--mut)}.painel-dados b{font-size:13px;font-weight:600}
h2{font-size:18px;font-weight:600;display:flex;justify-content:space-between;align-items:baseline;margin:0 0 12px}
h2 small{font-size:12.5px;color:var(--mut);font-weight:400}
.posi{padding:13px 15px}.posi+.posi{margin-top:10px}
.posi .a{display:flex;justify-content:space-between;gap:10px;font-weight:600}
.posi .b{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;color:var(--mut);margin-top:3px}
.barra{height:4px;border-radius:4px;background:rgba(147,168,166,.18);margin-top:10px;position:relative;overflow:hidden}
.barra i{position:absolute;top:0;bottom:0;left:50%;border-radius:4px;transition:width .6s,left .6s}
.rol{padding:4px 16px}
.rol li{list-style:none;font-size:13.5px;color:var(--mut);padding:11px 0;border-bottom:1px solid var(--lin);overflow-wrap:anywhere;display:flex;gap:12px;justify-content:space-between}
.rol li:last-child{border:0}.rol li.c{color:var(--txt)}
.rol li b{font-weight:500;color:var(--txt);white-space:nowrap}
.rol li.al::before{content:"";width:9px;height:9px;border-radius:50%;background:var(--k,var(--ou));box-shadow:0 0 10px var(--k,var(--ou));flex:none;align-self:center}
.rol li.al{justify-content:flex-start}.rol li.al span:last-child{margin-left:auto;white-space:nowrap}
.agentes{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:16px}
.ag{padding:12px 14px;position:relative;overflow:hidden;min-width:0}
.ag h4{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px}.ag h4 span{width:8px;height:8px;border-radius:3px;background:var(--k)}
.ag p{font-size:12px;color:var(--mut);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ag::after{content:"";position:absolute;left:0;bottom:0;height:2px;width:40%;background:linear-gradient(90deg,transparent,var(--k),transparent);animation:varre 2.6s linear infinite;animation-delay:var(--d)}
@keyframes varre{from{transform:translateX(-110%)}to{transform:translateX(360%)}}
.nota{font-size:12.5px;color:var(--mut);margin-top:14px;line-height:1.55}
@media(max-width:699px){.ln{grid-template-columns:auto minmax(0,1fr) auto;grid-template-areas:"av nm vr" "av nm pn";gap:1px 10px;padding:9px 10px}.ln .rk,.ln .sp{display:none}.ln .av{grid-area:av}.ln .nm{grid-area:nm}.ln .vr{grid-area:vr;font-size:15px}
 .ln .pn{grid-area:pn;font-size:12.5px}.ln .pn svg{width:18px;height:18px}.ln .pn circle{stroke-width:4}.ln.topo .tok::before{content:"♛";color:var(--ou);flex:none}.nm .tok{font-size:16px;flex-wrap:wrap;row-gap:2px}
 .abas button{padding:10px 11px;font-size:13.5px;gap:6px}.abas svg{width:17px;height:17px}.chip{padding:7px 12px;font-size:13px}.st>svg{width:32px;height:32px}.st .ex{display:none}.st{padding:12px;gap:10px}.st span{white-space:normal;font-size:11.5px;line-height:1.25}}
@media(max-width:480px){.abas svg{display:none}.abas .filtro svg{display:block}.abas .rolo{padding:6px 2px}}
@media(min-width:700px){
 .stats,.agentes{grid-template-columns:repeat(4,1fr);gap:16px}
 .melhor{grid-template-columns:auto auto minmax(0,1.2fr) minmax(90px,1fr) auto;padding:16px 24px;gap:20px}
 .melhor .duo{max-width:34vw}
 .med{margin:0}
}
@media(min-width:1100px){
 :root{--R:min(100%,47vh,480px)}
 .app{grid-template-columns:282px 1fr}
 .lado{display:flex;flex-direction:column;padding:22px 20px;border-right:1px solid var(--lin);background:linear-gradient(180deg,rgba(5,16,17,.75),rgba(4,12,13,.5));position:sticky;top:0;height:100vh;overflow-y:auto}
 .fundo,.logo-m{display:none}
 main{padding:20px 28px 26px}
 .palco{grid-template-columns:minmax(0,1.22fr) minmax(0,1fr);align-items:start;gap:26px}
 .zona{justify-items:center;padding:0}
 #fio{display:none}
 .grelha{grid-template-columns:1.3fr 1fr;align-items:start}
}
.melhor{display:grid;grid-template-columns:minmax(0,1.4fr) repeat(4,minmax(90px,1fr)) auto;gap:13px;align-items:center;min-width:0;padding:12px 16px}
.melhor-lead{min-width:0}.melhor-lead h3{margin:0 0 5px;font-size:12px;color:var(--mut);font-weight:550}
.melhor-moeda{display:flex;align-items:center;gap:9px;min-width:0}.melhor-moeda .av{--t:34px!important;width:34px;height:34px}
.melhor-moeda b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:16px}.melhor-moeda small{display:block;color:var(--mut);font-size:11px}
.melhor-metrica{min-width:0;padding-left:11px;border-left:1px solid rgba(147,168,166,.2)}.melhor-metrica span{display:block;color:var(--mut);font-size:10px}.melhor-metrica b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px;font-weight:600}
.melhor .melhor-link{margin:0;padding:8px 12px;font-size:12px;white-space:nowrap}.melhor .melhor-link.bt{border-radius:12px}
.melhor .melhor-vazio{grid-column:1/-1;padding:4px;text-align:center;color:var(--mut);font-size:13px}
@media(min-width:900px){
 .palco{grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-areas:"radar lista" "detalhe lista" "melhor melhor";align-items:start;gap:16px 22px}
 .zona{justify-items:center;padding:0}
 .scanner-col{grid-area:lista}
}
@media(max-width:699px){
 .blip{width:22px;height:22px;margin:0}
 .blip-cartao{width:128px;padding:6px 7px}.blip-card-variacao,.blip-card-score{display:none}.blip-card-mcap,.blip-card-idade,.blip-card-motivo{display:none}
 .lista-moedas{max-height:48vh}
 .scanner-filtros{gap:4px}.scanner-filtros button{padding:8px 2px;font-size:11px}
 .melhor{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding:12px}
 .melhor-lead,.melhor .melhor-link{grid-column:1/-1}
 .melhor-metrica{padding-left:8px}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="ceu"><i></i><i></i><svg viewBox="0 0 1200 300" preserveAspectRatio="none"><path d="M0 210C200 150 330 270 560 220S960 120 1200 200" stroke="rgba(34,224,200,.35)"/><path d="M0 240C220 190 380 290 600 250S980 170 1200 240" stroke="rgba(247,165,49,.3)"/><path d="M0 270C260 230 420 300 640 275S1000 220 1200 268" stroke="rgba(34,224,200,.22)"/></svg></div>
<svg width="0" height="0" style="position:absolute"><defs>
 <linearGradient id="gm" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#f7a531"/><stop offset=".72" stop-color="#ffd27a"/><stop offset="1" stop-color="#ff6b6b"/></linearGradient>
 <symbol id="i-dashboard" viewBox="0 0 24 24"><path d="M4 11.5 12 4l8 7.5V20h-5v-5H9v5H4v-8.5Z"/></symbol>
 <symbol id="i-scanner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="1.8"/><path d="M8.2 15.8a5.4 5.4 0 0 1 0-7.6M15.8 8.2a5.4 5.4 0 0 1 0 7.6M5.3 18.7a9.5 9.5 0 0 1 0-13.4M18.7 5.3a9.5 9.5 0 0 1 0 13.4"/></symbol>
 <symbol id="i-oportunidades" viewBox="0 0 24 24"><path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z"/></symbol>
 <symbol id="i-operacoes" viewBox="0 0 24 24"><path d="m12 3 9 4.5-9 4.5-9-4.5L12 3Z"/><path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5"/></symbol>
 <symbol id="i-portfolio" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v9h9"/></symbol>
 <symbol id="i-alertas" viewBox="0 0 24 24"><path d="M6 9a6 6 0 0 1 12 0c0 6 2.5 7.5 2.5 7.5h-17S6 15 6 9ZM10 20a2.2 2.2 0 0 0 4 0"/></symbol>
 <symbol id="i-config" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1"/></symbol>
 <symbol id="i-alvo" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><path d="m12 12 7.5-7.5M16.5 4.5h3v3"/></symbol>
 <symbol id="i-pin" viewBox="0 0 24 24"><path d="m14.5 3 6.5 6.5-3 1-3.5 3.5-.5 4.5-4-4L4 20.5 9.5 14.5l-4-4 4.5-.5L13.5 6.5l1-3.5Z"/></symbol>
 <symbol id="i-alta" viewBox="0 0 24 24"><path d="m3 17 6-6 4 4 8-8M15.5 7H21v5.5"/></symbol>
 <symbol id="i-filtro" viewBox="0 0 24 24"><path d="M4 5h16l-6 7.5V19l-4 1.5v-8L4 5Z"/></symbol>
 <symbol id="i-foguete" viewBox="0 0 24 24"><path d="M5 15c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9-.8-.8-2.1-.8-2.9-.1ZM12 15l-3-3a22 22 0 0 1 2-4A12.9 12.9 0 0 1 22 2c0 2.7-.8 7.5-6 11a22 22 0 0 1-4 2Z"/><path d="M9 12H4s.6-3 2-4c1.6-1.1 5 0 5 0M12 15v5s3-.6 4-2c1.1-1.6 0-5 0-5"/></symbol>
 <symbol id="i-escudo" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/></symbol>
 <symbol id="i-barras" viewBox="0 0 24 24"><path d="M5 20v-6M10 20V8M15 20v-9M20 20V5" stroke-width="2.6"/></symbol>
 <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.2"/></symbol>
 <symbol id="i-ciclo" viewBox="0 0 24 24"><path d="M20 12a8 8 0 0 1-14 5.3M4 12a8 8 0 0 1 14-5.3M18 3v4h-4M6 21v-4h4"/></symbol>
 <symbol id="i-louro" viewBox="0 0 24 24"><path d="M7 4c-3 3-4 7-2 11 1 2 3 3.5 5 4M17 4c3 3 4 7 2 11-1 2-3 3.5-5 4"/><path d="m12 7 1.3 2.7 3 .4-2.2 2 .6 2.9L12 13.6 9.3 15l.6-2.9-2.2-2 3-.4L12 7Z" fill="currentColor" stroke="none"/></symbol>
</defs></svg>
<div class="app">
 <aside class="lado">
  <div class="marca"><img class="mascote" src="/assets/mascote_oficial.png" alt="Mascote oficial MemeBot" decoding="async"><span class="pro">♛ PRO</span><b>MemeBot</b><small>AI Crypto Scanner</small></div>
  <nav class="nav" id="nav"></nav>
 </aside>
 <main>
  <header>
   <div><div class="logo-m"><img class="mascote" src="/assets/mascote_oficial.png" alt="Mascote oficial MemeBot" decoding="async">MemeBot<span>♛ PRO</span></div><h1>Scanner de <em>Meme Coins</em></h1><p class="sub" id="sub">Em tempo real, à procura das próximas grandes oportunidades.</p></div>
   <div class="chips"><span class="chip modo" id="cmode">Modo: Indisponível</span><span class="chip"><i class="sol"></i>Solana</span><span class="chip" id="cvivo"><span class="pt"></span><span id="tvivo">A ligar</span></span><span class="chip"><svg class="i gira" viewBox="0 0 24 24"><use href="#i-ciclo"/></svg><span id="relogio">Indisponível</span></span></div>
  </header>
  <section class="stats">
   <div class="cx st" style="--k:var(--ou)"><svg class="i" viewBox="0 0 24 24"><use href="#i-foguete"/></svg><div><b id="s1">Indisponível</b><span>Moedas escaneadas hoje</span></div><span class="ex"><svg class="i" viewBox="0 0 24 24"><use href="#i-barras"/></svg></span></div>
   <div class="cx st" style="--k:var(--vm)"><svg class="i" viewBox="0 0 24 24"><use href="#i-oportunidades"/></svg><div><b id="s2">Indisponível</b><span>Sinais (24h)</span></div></div>
   <div class="cx st"><svg class="i" viewBox="0 0 24 24"><use href="#i-alvo"/></svg><div><b id="s3">Indisponível</b><span>Posições abertas</span></div></div>
   <div class="cx st" style="--k:var(--ou)"><svg class="i" viewBox="0 0 24 24"><use href="#i-escudo"/></svg><div><b id="s4">Indisponível</b><span id="s4t">Taxa de acerto</span></div><span class="ex" title="Percentagem de trades simulados fechados com lucro"><svg class="i" viewBox="0 0 24 24"><use href="#i-info"/></svg></span></div>
  </section>

  <div class="vista" id="v-scanner">
   <div class="palco">
    <div class="zona" id="zona">
     <div class="radar"><div class="base"><i></i><i></i><i></i></div>
      <div class="globo"><div class="mapa"></div><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="13"/><circle cx="50" cy="50" r="25"/><circle cx="50" cy="50" r="37"/><path d="M50 0v100M0 50h100M15 15l70 70M85 15 15 85"/><ellipse cx="50" cy="50" rx="18" ry="50"/><ellipse cx="50" cy="50" rx="36" ry="50"/><ellipse cx="50" cy="50" rx="50" ry="18"/><ellipse cx="50" cy="50" rx="50" ry="36"/></svg><div class="varre" id="varre"></div></div>
      <div class="aro"></div><div class="blips" id="blips"></div>
      <div class="nucleo"><img class="mascote mascote-radar" src="/assets/mascote_oficial.png" alt="Mascote oficial MemeBot" decoding="async"></div>
     </div>
     <div class="radar-legenda" aria-label="Estados das moedas no radar"><span><i></i>Em análise</span><span class="ok"><i></i>Aprovada</span><span class="no"><i></i>Rejeitada</span></div>
     <svg id="fio"><line id="fiol"/></svg>
    </div>
    <div class="scanner-col">
     <div class="cx painel">
       <div class="scanner-list-title"><div><h2>Moedas analisadas</h2><small>Seleciona uma moeda para ver os detalhes</small></div></div>
       <details class="scanner-counter-panel"><summary>Contadores do Scanner · sessão e dia</summary><div class="scanner-counter-grid" id="scanner-counters"></div></details>
      <div class="abas scanner-filtros" id="abas" role="group" aria-label="Filtrar moedas"><button data-a="all" class="on">Todas</button><button data-a="analise">Em análise</button><button data-a="aprovada">Aprovadas</button><button data-a="rejeitada">Rejeitadas</button></div>
     <div id="lista"></div>
     <section class="cx manual-proposals" id="manual-proposals" aria-live="polite" hidden></section>
     </div>
     <div class="cx aviso" id="aviso" aria-live="polite"></div>
    </div>
    <section class="cx radar-info" id="radar-info" aria-live="polite"><h3>Detalhes da moeda</h3><p>Ainda não há moedas analisadas.</p></section>
    <section class="cx melhor" id="melhor"></section>
   </div>
  </div>

  <div class="vista" id="v-dashboard">
   <div class="grelha">
    <div class="cx bloco"><div class="rot" id="rot-total">Valor total</div><div class="valor" id="total">Indisponível</div><span class="delta sobe" id="delta">Indisponível</span>
     <svg class="graf" id="graf" viewBox="0 0 600 120" preserveAspectRatio="none"><defs><linearGradient id="gr" x1="0" y1="0" x2="0" y2="1"><stop id="g1" offset="0" stop-opacity=".3"/><stop id="g2" offset="1" stop-opacity="0"/></linearGradient></defs><path id="area" fill="url(#gr)"/><path id="linha" class="lin"/></svg></div>
    <div class="quad"><div class="cx"><span class="rot">Saldo virtual livre</span><b id="saldo">Indisponível</b></div><div class="cx"><span class="rot">Lucro / prejuízo realizado</span><b id="real">Indisponível</b></div><div class="cx"><span class="rot">Trades fechados</span><b id="trades">Indisponível</b></div><div class="cx"><span class="rot">Ganhos / perdas</span><b id="gp">Indisponível</b></div></div>
    <div class="cx painel-dados"><span>Estado do bot</span><b id="estado-bot">A ligar</b><span>Última atualização</span><b id="ultima-atualizacao">Indisponível</b></div>
   </div>
   <section class="agentes" id="agentes"></section>
  </div>
  <div class="vista" id="v-oportunidades"><h2>Oportunidades <small id="nop"></small></h2><div class="cx painel" id="todas"></div></div>
  <div class="vista" id="v-operacoes"><h2>Operações <small>compras e vendas simuladas</small></h2><ul class="cx rol" id="acoes"></ul></div>
  <div class="vista" id="v-portfolio"><h2>Portfólio <small id="npos2"></small></h2><div id="pos"></div></div>
  <div class="vista" id="v-alertas"><h2>Alertas <small>sinais novos e posições fechadas</small></h2><ul class="cx rol" id="alertas"></ul></div>
  <div class="vista" id="v-config"><h2>Configurações <small>só leitura</small></h2><ul class="cx rol" id="config"></ul><p class="nota">Para mudar estes valores, edita o bloco CONFIG no topo do ficheiro bot.py e volta a arrancar o bot. O bot funciona em simulação: usa dinheiro fictício e nunca compra nada. A pontuação é calculada com regras fixas (liquidez, volume, idade, compras vs. vendas).</p></div>
 </main>
 <nav class="fundo" id="fundo"></nav>
</div>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=x;return n};
const NS='http://www.w3.org/2000/svg';
const svg=(cls,vb,html)=>{const s=document.createElementNS(NS,'svg');s.setAttribute('class',cls);s.setAttribute('viewBox',vb);s.innerHTML=html;return s};
const ico=n=>svg('i','0 0 24 24',`<use href="#i-${n}"/>`);
const pt=(v,d=1)=>(+v||0).toLocaleString('pt-PT',{minimumFractionDigits:d,maximumFractionDigits:d});
const mil=v=>Math.round(+v||0).toLocaleString('de-DE');
const pct=(v,d=1)=>{v=+v||0;return(v>=0?'+':'−')+pt(Math.abs(v),d)+'%'};
const usd=v=>(v<0?'−':'')+'$'+pt(Math.abs(v),2);
const disponivel=v=>v!==null&&v!==undefined&&v!==''&&typeof v!=='boolean'&&Number.isFinite(Number(v));
const valorOuIndisponivel=(v,fmt)=>disponivel(v)?fmt(Number(v)):'Indisponível';
const sinalado=v=>(v>=0?'+':'−')+'$'+pt(Math.abs(v),2);
const curto=v=>{v=+v||0;const f=(x,d)=>x.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});return v>=1e6?'$'+f(v/1e6,2)+'M':v>=1e3?'$'+f(v/1e3,1)+'K':'$'+f(v,0)};
const calmo=matchMedia('(prefers-reduced-motion:reduce)').matches,largo=matchMedia('(min-width:1100px)');
const hash=s=>{let h=7;for(const c of String(s))h=(h*31+c.charCodeAt(0))>>>0;return h};
const VISTAS=[['dashboard','Dashboard'],['scanner','Scanner'],['oportunidades','Oportunidades'],['operacoes','Operações'],['portfolio','Portfólio'],['alertas','Alertas'],['config','Configurações']];
let D=null,vista='scanner',aba='all',chaves={},alertaVisto=0;
try{alertaVisto=+localStorage.getItem('mb_alerta')||0}catch(e){}
function conta(n,ate,fmt){if(!n)return;if((typeof ate!=='number'&&typeof ate!=='string')||!disponivel(ate)){cancelAnimationFrame(n._r);n._r=null;n._v=null;n.textContent='Indisponível';return}
 const alvo=Number(ate),de=Number.isFinite(n._v)?n._v:alvo;
 if(n._v===alvo){if(n.textContent!==fmt(alvo))n.textContent=fmt(alvo);return}
 n._v=alvo;if(calmo||de===alvo){n.textContent=fmt(alvo);return}
 const t0=performance.now();cancelAnimationFrame(n._r);
 (function f(t){const k=Math.min(1,(t-t0)/700),e=1-Math.pow(1-k,3);n.textContent=fmt(de+(alvo-de)*e);if(k<1)n._r=requestAnimationFrame(f);else n._r=null})(t0)}
function avatar(o,t){const a=el('div','av',(o.simbolo||'?').replace(/[^A-Za-z0-9]/g,'').slice(0,1).toUpperCase()||'?');
 const h=hash(o.mint||o.simbolo)%360;a.style.background=`linear-gradient(135deg,hsl(${h} 85% 70%),hsl(${(h+50)%360} 80% 52%))`;if(t)a.style.setProperty('--t',t+'px');
 if(o.img){const i=new Image();i.alt='';i.referrerPolicy='no-referrer';i.loading='lazy';i.onerror=()=>i.remove();i.src=o.img;a.append(i)}return a}
const temLink=o=>o.mint&&D.modo!=='demo';
function ligar(n,o){if(temLink(o)){n.href='https://dexscreener.com/solana/'+encodeURIComponent(o.mint);n.target='_blank';n.rel='noopener noreferrer'}return n}
function etq(s){if(s.estado==='fechado')return[s.resultado>0?'ganho':'perda','Fechou '+pct(s.resultado)];
 if(D.agora-s.ts<600)return['novo','Novo'];
 if(s.estado==='anulado')return['anulado','Anulado'];
 if(s.estado==='comprado')return['comprado','Comprado'];if(s.estado==='sem_vaga')return['sem_vaga','Sem vaga'];if(s.estado==='aguarda_confirmacao')return['aguarda_confirmacao','Aguarda confirmação'];return null}
function anel(p){const d=el('div','pn');if(!p){d.textContent='—';d.style.color='var(--mut)';return d}
 const c=p>=70?'#22e0c8':'#7d8c8b';d.append(svg('','0 0 36 36',`<circle cx="18" cy="18" r="14" stroke="rgba(147,168,166,.22)"/><circle cx="18" cy="18" r="14" stroke="${c}" stroke-linecap="round" stroke-dasharray="${(87.96*p/100).toFixed(1)} 87.96"/>`),p+'%');return d}
function faisca(v,sobe){const s=svg('sp','0 0 70 26','');if(!Array.isArray(v)||v.length<2)return s;const mn=Math.min(...v),am=(Math.max(...v)-mn)||1;
 const p=v.map((y,i)=>[(3+i/(v.length-1)*62).toFixed(1),(22-(y-mn)/am*18).toFixed(1)]),c=sobe?'#2ee6a8':'#ff6b6b';
 s.innerHTML=`<polyline points="${p.map(q=>q.join(',')).join(' ')}" stroke="${c}"/><circle cx="${p[p.length-1][0]}" cy="${p[p.length-1][1]}" r="2.6" fill="${c}"/>`;return s}
function titulo(o,cls){const t=el('div',cls||'tok');t.append(el('span',null,'$'+o.simbolo));const e=etq(o);if(e)t.append(el('span','tag '+e[0],e[1]));return t}
function subida(o){const g=el('div','gr '+(o.var_h1>=0?'sobe':'desce'),pct(o.var_h1)+' ');g.append(el('small',null,'(1h)'));return g}
/* ---------- listas ---------- */
const vistosLn=new Set();
function fila(o,i,topo,pre,sub){const id=pre+o.id,novo=!vistosLn.has(id);vistosLn.add(id);
 const n=ligar(el(temLink(o)?'a':'div','ln'+(topo?' topo':'')+(novo&&chaves.pronto?' novo-e':'')),o),nm=el('div','nm');nm.append(titulo(o));if(sub)nm.append(el('p',null,sub));
 n.append(el('span','rk',topo?'♛':i+1),avatar(o),nm,faisca(o.spark,o.var_h1>=0),el('div','vr '+(o.var_h1>=0?'sobe':'desce'),pct(o.var_h1)),anel(o.pontuacao));return n}
 function nomeEstadoRadar(o){const s=o.status||o.classification;if(s==='pending_data'||o.estado_analise==='dados_indisponiveis')return'Dados pendentes';const e=estadoRadar(o);return e==='analise'?'Em an\u00e1lise':e==='aprovada'?'Aprovada':e==='rejeitada'?'Rejeitada':e==='historica'?'Aprovada anteriormente':e==='descoberta'?'Nova descoberta':e==='sem_par'?'Sem par':e==='erro_tecnico'?'Erro t\u00e9cnico':'Dados indispon\u00edveis'}
function compradaNoPaper(mint){return(D.posicoes||[]).some(p=>p.mint===mint)||(D.trades||[]).some(t=>t.mint===mint&&t.tipo==='COMPRA')}
function precoLista(o){if(disponivel(o.preco)&&Number.isFinite(Number(o.preco))&&Number(o.preco)>=0)return '$'+Number(o.preco).toLocaleString('en-US',{maximumSignificantDigits:9});const p=(D.posicoes||[]).find(x=>x.mint===o.mint);if(p&&disponivel(p.preco_atual)&&Number(p.preco_atual)>=0)return '$'+Number(p.preco_atual).toLocaleString('en-US',{maximumSignificantDigits:9});const t=[...(D.trades||[])].reverse().find(x=>x.mint===o.mint&&disponivel(x.preco_mercado)&&Number(x.preco_mercado)>=0);return t?'$'+Number(t.preco_mercado).toLocaleString('en-US',{maximumSignificantDigits:9}):'Indispon\u00edvel'}
function selecionarMoeda(mint,irLista=false){const o=moedasRadar().find(x=>x.mint===mint);if(!o)return;if(irLista&&aba!=='all')mudarAba('all');radarSelecionado=mint;radarCartaoAbertoMint=mint;radarDetalheAberto=true;mostrarDetalhe(o);chaves.l=null;linhas();document.querySelectorAll('.blip').forEach(n=>n.classList.toggle('selecionado',n.dataset.mint===mint));atualizarCartoesRadar();if(irLista){const n=[...document.querySelectorAll('.moeda-linha')].find(x=>x.dataset.mint===mint);if(n)n.scrollIntoView({block:'nearest',behavior:'smooth'})}}
function linhas(){renderScannerCounters();const todos=moedasRadar(),l=aba==='all'?todos:todos.filter(o=>estadoRadar(o)===aba),c=$('lista');
 const ch=aba+'|'+radarSelecionado+'|'+JSON.stringify(l.map(o=>[o.mint,o.simbolo,o.preco,o.pontuacao,estadoRadar(o),compradaNoPaper(o.mint)]));if(chaves.l===ch)return;chaves.l=ch;c.className='lista-moedas';
 if(!l.length){const vazio=aba==='all'?'Ainda n\u00e3o h\u00e1 moedas analisadas.':aba==='analise'?'N\u00e3o h\u00e1 moedas em an\u00e1lise.':aba==='aprovada'?'N\u00e3o h\u00e1 moedas aprovadas.':'N\u00e3o h\u00e1 moedas rejeitadas.';c.replaceChildren(el('div','vazio',vazio));return}
 c.replaceChildren(...l.map(o=>{const selecionada=radarSelecionado===o.mint,comprada=compradaNoPaper(o.mint),n=el('button','moeda-linha'+(selecionada?' selecionada':'')+(comprada?' tem-entrada':''));n.type='button';n.dataset.mint=o.mint;n.setAttribute('aria-pressed',String(selecionada));n.setAttribute('aria-label',(o.simbolo||'Moeda')+', '+nomeEstadoRadar(o));
   const nome=el('span','moeda-nome');nome.append(el('b',null,'$'+(o.simbolo||'Indispon\u00edvel')));if(o.nome)nome.append(el('small',null,o.nome));if(comprada)nome.append(el('em',null,o.paper_status==='open'?'Paper aberta':o.paper_status==='closed'?'Paper fechada':'Paper'));const meta=el('span','moeda-meta');
   const score=o.score_calculated===false?'Não calculado':disponivel(o.pontuacao)?Math.round(Number(o.pontuacao))+'/100':'Indisponível';meta.append(el('span',null,'Pre\u00e7o: '),el('b',null,precoLista(o)),el('span',null,'Pontua\u00e7\u00e3o: '),el('b',null,score));
  n.append(nome,meta,el('span','estado-moeda '+estadoRadar(o),nomeEstadoRadar(o)));n.addEventListener('click',()=>selecionarMoeda(o.mint));return n}))}
function todas(){const l=D.sinais,ch=JSON.stringify(l.map(o=>[o.id,o.estado,o.resultado]));$('nop').textContent=l.length?l.length+' sinais':'';if(chaves.t===ch)return;chaves.t=ch;
 $('todas').replaceChildren(...(l.length?l.map((o,i)=>fila(o,i,false,'t',o.hora+' · liq '+curto(o.liq)+(o.saida?' · '+o.saida:''))):[el('div','vazio','Ainda sem oportunidades.')]))}
/* ---------- radar ---------- */
const blips=new Map(),T=4000;let radarSelecionado=null,radarCartaoAbertoMint=null,radarHoverMint=null,radarDetalheAberto=true,avisoSeq=0,avisoFechadoId=null,avisoAtualId=null;
function atualizarCartoesRadar(){const mint=radarHoverMint||radarCartaoAbertoMint;for(const n of blips.values()){const visivel=n.dataset.mint===mint;n.classList.toggle('cartao-visivel',visivel);if(n.orbita)n.orbita.style.zIndex=visivel?'9':document.activeElement===n?'8':'2'}}
 function moedasRadar(){const porMint=new Map(),validos=xs=>(Array.isArray(xs)?xs:[]).filter(o=>o&&typeof o.mint==='string'&&/^[A-Za-z0-9]{30,50}$/.test(o.mint));
  for(const o of validos(D.descobertas))porMint.set(o.mint,{...o,status:o.status||'discovered'});
  for(const o of validos(D.sinais)){const a=porMint.get(o.mint)||{mint:o.mint};a.historical_approval=true;a.historical_score=o.pontuacao;a.paper_status=o.estado==='comprado'?'open':o.estado==='fechado'?'closed':a.paper_status;porMint.set(o.mint,a)}
  for(const t of validos(D.trades)){const a=porMint.get(t.mint)||{mint:t.mint};if(t.tipo==='COMPRA')a.paper_status=a.paper_status==='closed'?'closed':'open';if(t.tipo==='VENDA')a.paper_status='closed';if(!a.simbolo&&t.simbolo)a.simbolo=t.simbolo;porMint.set(t.mint,a)}
  for(const p of validos(D.posicoes)){const a=porMint.get(p.mint)||{mint:p.mint};Object.assign(a,{simbolo:p.simbolo,preco:p.preco_atual,paper_status:'open'});porMint.set(p.mint,a)}
  for(const o of validos(D.recentes))porMint.set(o.mint,{...(porMint.get(o.mint)||{}),...o});
  for(const o of validos(D.em_analise))porMint.set(o.mint,{...(porMint.get(o.mint)||{}),...o,status:'analysis_in_progress'});
  return [...porMint.values()].slice(-40)}
 function estadoRadar(o){const s=o.status||o.classification; if(s==='analysis_in_progress'||o.estado_analise==='em_analise')return'analise';if(s==='approved'||o.estado_analise==='aprovada'||o.aprovado===true)return'aprovada';if(s==='rejected'||o.estado_analise==='rejeitada'||o.aprovado===false)return'rejeitada';if(s==='no_pair'||o.estado_analise==='sem_par')return'sem_par';if(s==='technical_error'||o.estado_analise==='erro_tecnico')return'erro_tecnico';if(s==='historical_approval'||o.estado_analise==='aprovada_anteriormente')return'historica';if(s==='discovered'||o.estado_analise==='descoberta')return'descoberta';if(s==='pending_data'||o.estado_analise==='dados_indisponiveis')return'indisponivel';return o.historical_approval?'historica':'indisponivel'}
 function renderScannerCounters(){const root=$('scanner-counters'),c=D.scanner_counts;if(!root||!c)return;const labels=[['tokens_received','Recebidos'],['unique_new','Mints novos'],['reanalyzed','Reanálises'],['no_pair','Sem par'],['analysis_completed','Concluídas'],['rejected','Rejeições'],['pending_data','Incompletas'],['technical_errors','Erros técnicos']];const items=[];for(const [bucket,title]of [['session','Sessão'],['daily','Hoje']])for(const [k,label]of labels)items.push(el('span',null,title+' · '+label),el('b',null,String((c[bucket]||{})[k]??0)));root.replaceChildren(...items)}
function valorOperacaoUsd(v){return disponivel(v)?usd(Number(v)):'Indispon\u00edvel'}
function precoToken(v){return disponivel(v)&&Number(v)>0?'$'+Number(v).toLocaleString('en-US',{maximumSignificantDigits:9}):'Indispon\u00edvel'}
function quantidadeTokens(v){return disponivel(v)&&Number(v)>0?Number(v).toLocaleString('en-US',{maximumFractionDigits:4}):'Indispon\u00edvel'}
function blocoOperacao(mint){const box=el('div','radar-operacao'),dl=el('dl'),pos=(D.posicoes||[]).find(p=>p.mint===mint),hist=D.trades_disponiveis?(D.trades||[]).filter(t=>t.mint===mint):[];
 const campo=(nome,valor)=>{const c=el('div');c.append(el('dt',null,nome),el('dd',null,valor==null||valor===''?'Indispon\u00edvel':String(valor)));dl.append(c)};
 let compra=hist.filter(t=>t.tipo==='COMPRA').slice(-1)[0];const ultima=hist[hist.length-1];let venda=null,estado='Indispon\u00edvel',investido=null,precoEntrada=null,tokens=null,data=null,valorAtual=null;
 if(pos){estado='Aberta';investido=pos.custo;precoEntrada=pos.preco_efetivo;tokens=pos.tokens;data=compra&&compra.data_hora||null;
  if(!data&&disponivel(pos.aberta_em)&&Number(pos.aberta_em)>0)data=new Date(Number(pos.aberta_em)*1000).toLocaleString('pt-PT');
  if(disponivel(pos.tokens)&&Number(pos.tokens)>0&&disponivel(pos.preco_atual)&&Number(pos.preco_atual)>0)valorAtual=Number(pos.tokens)*Number(pos.preco_atual)}
 else if(ultima&&ultima.tipo==='VENDA'){
  venda=ultima;for(let i=hist.length-2;i>=0;i--)if(hist[i].tipo==='COMPRA'){compra=hist[i];break}
  if(compra){estado='Fechada';investido=compra.valor_usd;precoEntrada=compra.preco_efetivo;tokens=compra.tokens;data=compra.data_hora}
 }else if(ultima&&ultima.tipo==='ANULADA'){estado='Anulada'}
 else if(compra){investido=compra.valor_usd;precoEntrada=compra.preco_efetivo;tokens=compra.tokens;data=compra.data_hora}
 else if(D.trades_disponiveis)estado='Sem entrada';
 campo('Estado da posi\u00e7\u00e3o',estado);campo('Valor investido',valorOperacaoUsd(investido));
 campo(pos?'Valor atual bruto':'Valor atual',pos?valorOperacaoUsd(valorAtual):'Indispon\u00edvel');
 campo('Pre\u00e7o efetivo de entrada',precoToken(precoEntrada));campo('Quantidade de tokens',quantidadeTokens(tokens));campo('Data/hora da entrada',data);
 campo('Data/hora da sa\u00edda',venda?venda.data_hora:'Indispon\u00edvel');
 if(venda){campo('Pre\u00e7o efetivo de sa\u00edda',precoToken(venda.preco_efetivo));campo('Valor recebido',valorOperacaoUsd(venda.valor_usd));campo('Resultado realizado',disponivel(venda.pnl_usd)?valorOperacaoUsd(venda.pnl_usd)+' ('+(disponivel(venda.pnl_pct)?Number(venda.pnl_pct).toFixed(2)+'%':'Indispon\u00edvel')+')':'Indispon\u00edvel')}
 else if(estado==='Fechada'){campo('Pre\u00e7o efetivo de sa\u00edda','Indispon\u00edvel');campo('Valor recebido','Indispon\u00edvel');campo('Resultado realizado','Indispon\u00edvel')}
 box.append(el('h4',null,'Opera\u00e7\u00e3o Paper Trading'),dl);return box}
function botaoFecharScanner(rotulo,acao){const b=el('button','scanner-fechar','\u00d7');b.type='button';b.setAttribute('aria-label',rotulo);b.onclick=acao;return b}
function fecharDetalhe(){radarDetalheAberto=false;radarCartaoAbertoMint=null;const c=$('radar-info');c.hidden=true;c.setAttribute('aria-hidden','true');const n=[...document.querySelectorAll('.moeda-linha')].find(x=>x.dataset.mint===radarSelecionado)||blips.get(radarSelecionado);if(n)n.focus({preventScroll:true});atualizarCartoesRadar()}
function fecharAviso(id){const a=$('aviso'),ch=String(id||'-');avisoFechadoId=ch;a.setAttribute('aria-hidden','true');a.classList.add('saindo');const seq=avisoSeq;setTimeout(()=>{if(seq===avisoSeq&&avisoAtualId===ch){a.hidden=true;a.classList.remove('saindo')}},240)}
 function mostrarDetalhe(o){const c=$('radar-info');if(!o){if(radarSelecionado)return;const h=el('div','radar-info-head');h.append(el('h3',null,'Detalhes da moeda'),botaoFecharScanner('Fechar detalhes',fecharDetalhe));c.replaceChildren(h,el('p',null,moedasRadar().length?'Seleciona um ponto para ver os dados da an\u00e1lise.':'Ainda n\u00e3o h\u00e1 moedas analisadas.'));c.hidden=!radarDetalheAberto;c.setAttribute('aria-hidden',String(c.hidden));return}
  const est=estadoRadar(o),rotulo=nomeEstadoRadar(o),nome=typeof o.nome==='string'&&o.nome.trim()?o.nome.trim():'Indispon\u00edvel';
  const preco=disponivel(o.preco)&&Number.isFinite(Number(o.preco))&&Number(o.preco)>=0?'$'+Number(o.preco).toLocaleString('en-US',{maximumSignificantDigits:8}):'Indispon\u00edvel',score=o.score_calculated===false?'Não calculado':disponivel(o.pontuacao)?Math.round(Number(o.pontuacao))+'/100':'Indispon\u00edvel';
 const resultado=typeof o.resultado_analise==='string'&&o.resultado_analise.trim()?o.resultado_analise:typeof o.nota==='string'&&o.nota.trim()?o.nota:est==='analise'?'An\u00e1lise em curso.':est==='indisponivel'?'Liquidez indisponível ou dados insuficientes.':'Indispon\u00edvel';
  const meta=el('div','radar-meta');meta.append(el('b',null,'$'+(o.simbolo||'Indispon\u00edvel')),el('span',null,nome),el('span',null,'Pre\u00e7o: '+preco),el('span',null,'Pontua\u00e7\u00e3o: '+score),el('span','radar-status '+(est==='indisponivel'||est==='historica'||est==='descoberta'||est==='sem_par'||est==='erro_tecnico'?'indisponivel':est),rotulo));const h=el('div','radar-info-head');h.append(el('h3',null,'Resultado da an\u00e1lise'),botaoFecharScanner('Fechar detalhes',fecharDetalhe));const extras=[];if(o.historical_approval)extras.push(el('p',null,'Aprovada anteriormente; esta informação não confirma aprovação atual.'));if(o.paper_status)extras.push(el('p',null,'Posição Paper: '+(o.paper_status==='open'?'aberta':'fechada')));if(o.blocking_criterion){const b=o.blocking_criterion;extras.push(el('p',null,'Primeiro bloqueio: '+(b.name||b.id||'Indisponível')+' · observado: '+(b.observed??'Indisponível')+' · limiar: '+(b.threshold??'Indisponível')))}if(o.missing_fields&&o.missing_fields.length)extras.push(el('p',null,'Dados indisponíveis: '+o.missing_fields.join(', ')));if(o.invalid_fields&&o.invalid_fields.length)extras.push(el('p',null,'Dados inválidos: '+o.invalid_fields.join(', ')));if(o.technical_error)extras.push(el('p',null,'Erro técnico: '+(o.technical_error.message||o.technical_error.type||'Indisponível')));const criterios=(o.criteria||[]).filter(x=>x.status==='not_evaluated');if(criterios.length)extras.push(el('p',null,'Critérios seguintes: não avaliados ('+criterios.map(x=>x.name).join(', ')+')'));c.replaceChildren(h,meta,el('p',null,resultado),...extras,blocoOperacao(o.mint));c.hidden=!radarDetalheAberto;c.setAttribute('aria-hidden',String(c.hidden))}
// Coordenadas locais de composicao visual; nao representam localizacao geografica.
function alocarPonto(mint){const usado=new Set([...blips.values()].map(n=>Number(n.dataset.slot))),total=48,inicio=hash(mint)%total;
 for(let k=0;k<total;k++){const slot=(inicio+k)%total,ang=slot*2.399963229728653,raio=7+Math.sqrt((slot+.5)/total)*35;if(!usado.has(slot))return{slot,x:50+Math.sin(ang)*raio,y:50-Math.cos(ang)*raio}}return{slot:0,x:50,y:50}}
function fio(){$('fiol').style.display='none'}
function urlImagemRadar(valor){if(typeof valor!=='string'||!valor)return'';try{const u=new URL(valor);return u.protocol==='https:'&&/(^|\.)dexscreener\.com$/i.test(u.hostname)&&!u.username&&!u.password?u.href:''}catch(_){return''}}
function atualizarImagemRadar(n,o){const img=n.querySelector('.blip-img'),fallback=n.querySelector('.blip-fallback'),url=urlImagemRadar(o.img);fallback.textContent=String(o.simbolo||'?').replace(/[^\p{L}\p{N}]/gu,'').slice(0,3).toUpperCase()||'?';if(!url){img.removeAttribute('src');img.hidden=true;img.style.opacity='0';fallback.hidden=false;n.dataset.img='';return}if(n.dataset.img!==url){n.dataset.img=url;img.hidden=false;img.style.opacity='0';fallback.hidden=false;img.src=url}else if(img.complete){const valida=img.naturalWidth>0;img.hidden=!valida;img.style.opacity=valida?'1':'0';fallback.hidden=valida}else{img.hidden=false;img.style.opacity='0';fallback.hidden=false}}
function melhorSinalRadar(){const sinais=D&&Array.isArray(D.sinais)?D.sinais:[],comprados=sinais.filter(s=>s.estado==='comprado').sort((a,b)=>b.pontuacao-a.pontuacao);return comprados[0]||sinais[0]||null}
function valorCartaoRadar(v,fmt){return disponivel(v)&&Number.isFinite(Number(v))?fmt(Number(v)):'\u2014'}
function criterioRadar(o,id){const c=(Array.isArray(o.criteria)?o.criteria:[]).find(x=>x&&x.id===id&&(x.status==='passed'||x.status==='rejected'));return c&&disponivel(c.observed)&&Number(c.observed)>=0?Number(c.observed):null}
function idadeTxt(m){return m<60?Math.round(m)+' min':pt(m/60,1)+' h'}
function atualizarCartaoRadar(n,o,melhor){const c=n.cartaoDados;if(!c)return;c.nome.textContent=typeof o.nome==='string'&&o.nome.trim()?o.nome.trim():'\u2014';c.simbolo.textContent=o.simbolo?'$'+o.simbolo:'\u2014';c.preco.textContent=disponivel(o.preco)&&Number.isFinite(Number(o.preco))&&Number(o.preco)>=0?'$'+Number(o.preco).toLocaleString('en-US',{maximumSignificantDigits:9}):'\u2014';c.variacao.textContent=valorCartaoRadar(o.var_h1,v=>pct(v));c.score.textContent=disponivel(o.pontuacao)?Math.round(Number(o.pontuacao))+'/100':'\u2014';c.liq.textContent=valorCartaoRadar(disponivel(o.liq)&&Number(o.liq)>=0?o.liq:criterioRadar(o,'liquidez'),curto);c.mcap.textContent=valorCartaoRadar(criterioRadar(o,'market_cap'),curto);c.idade.textContent=valorCartaoRadar(criterioRadar(o,'idade_par'),idadeTxt);const est=estadoRadar(o),txt=typeof o.resultado_analise==='string'?o.resultado_analise.trim():'';c.motivo.textContent=txt;c.motivo.hidden=!txt||!['rejeitada','sem_par','erro_tecnico','indisponivel'].includes(est);c.estado.textContent=nomeEstadoRadar(o);c.melhor.hidden=!melhor;n.orbita.classList.toggle('destaque',melhor)}
function radar(){const c=$('blips'),l=moedasRadar(),ids=new Set(l.map(o=>o.mint)),melhor=melhorSinalRadar(),melhorMint=melhor&&melhor.mint;for(const[m,n]of blips)if(!ids.has(m)){n.orbita.remove();blips.delete(m);if(radarHoverMint===m)radarHoverMint=null;if(radarCartaoAbertoMint===m)radarCartaoAbertoMint=null}
 for(const o of l){let n=blips.get(o.mint);if(!n){n=el('button','blip entra');n.type='button';n.dataset.mint=o.mint;n.dataset.novo='1';const p=alocarPonto(o.mint),raio=Math.hypot(p.x-50,p.y-50),fase=Math.atan2(p.y-50,p.x-50)*180/Math.PI,orbita=el('div','blip-orbita');orbita.style.setProperty('--raio',raio.toFixed(2)+'%');orbita.style.setProperty('--diametro',(raio*2).toFixed(2)+'%');orbita.style.setProperty('--fase',fase.toFixed(2)+'deg');orbita.style.setProperty('--fase-neg',(-fase).toFixed(2)+'deg');orbita.style.setProperty('--dur',(30+p.slot%9*1.7).toFixed(1)+'s');n.dataset.slot=String(p.slot);const img=el('img','blip-img');img.alt='';img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';img.hidden=true;const fallback=el('span','blip-fallback','?');const cartao=el('span','blip-cartao'),head=el('span','blip-card-head'),ident=el('span','blip-identidade'),nome=el('b','bc-nome','\u2014'),simbolo=el('small','bc-simbolo','\u2014'),estado=el('span','blip-status','\u2014'),destaque=el('span','blip-melhor','Melhor');ident.append(nome,simbolo);head.append(ident,estado,destaque);const grade=el('span','blip-card-grid'),campo=(cls,titulo)=>{const caixa=el('span',cls);caixa.append(el('small',null,titulo),el('b',null,'\u2014'));grade.append(caixa);return caixa.lastChild};const preco=campo('blip-card-preco','Pre\u00e7o'),variacao=campo('blip-card-variacao','Varia\u00e7\u00e3o'),score=campo('blip-card-score','Score'),liq=campo('blip-card-liq','Liquidez'),mcap=campo('blip-card-mcap','Market cap'),idade=campo('blip-card-idade','Idade');idade.parentNode.title='Idade do par no momento da an\u00e1lise';const motivo=el('span','blip-card-motivo');motivo.hidden=true;cartao.append(head,grade,motivo);n.append(img,fallback,cartao);n.cartaoDados={nome,simbolo,estado,melhor:destaque,preco,variacao,score,liq,mcap,idade,motivo};img.addEventListener('load',()=>{if(n.dataset.img===img.src&&img.naturalWidth){img.hidden=false;img.style.opacity='1';fallback.hidden=true}});img.addEventListener('error',()=>{if(n.dataset.img===img.src){img.hidden=true;img.style.opacity='0';fallback.hidden=false}});n.addEventListener('animationend',()=>{delete n.dataset.novo;n.classList.remove('entra')},{once:true});if(p.x>50)cartao.classList.add('blip-cartao-esq');orbita.classList.add(estadoRadar(o));orbita.append(n);n.orbita=orbita;n.style.setProperty('--raio',raio.toFixed(2)+'%');n.style.setProperty('--fase-neg',(-fase).toFixed(2)+'deg');n.style.setProperty('--dur',(30+p.slot%9*1.7).toFixed(1)+'s');n.addEventListener('click',()=>{const mesma=radarSelecionado===o.mint,cartaoAberto=radarCartaoAbertoMint===o.mint,toque=matchMedia('(hover: none), (pointer: coarse)').matches;selecionarMoeda(o.mint);if(toque&&mesma&&cartaoAberto){radarCartaoAbertoMint=null;atualizarCartoesRadar()}});n.addEventListener('mouseenter',()=>{if(matchMedia('(hover: hover) and (pointer: fine)').matches){radarHoverMint=o.mint;atualizarCartoesRadar()}});n.addEventListener('mouseleave',()=>{if(radarHoverMint===o.mint)radarHoverMint=null;atualizarCartoesRadar()});n.addEventListener('focus',()=>{n.orbita.style.zIndex='8'});n.addEventListener('blur',()=>atualizarCartoesRadar());c.append(orbita);blips.set(o.mint,n)}
  const estado=estadoRadar(o),isMelhor=o.mint===melhorMint;atualizarImagemRadar(n,o);atualizarCartaoRadar(n,o,isMelhor);n.className='blip '+estado+(n.dataset.novo?' entra':'')+(radarSelecionado===o.mint?' selecionado':'');n.orbita.classList.remove('analise','aprovada','rejeitada');n.orbita.classList.add(estado);const estadoTexto=nomeEstadoRadar(o),nome=typeof o.nome==='string'&&o.nome.trim()?o.nome.trim():o.simbolo||'Moeda';n.setAttribute('aria-label',nome+' ($'+(o.simbolo||'Indispon\u00edvel')+') - '+estadoTexto);n.setAttribute('aria-pressed',String(radarSelecionado===o.mint));n.title=nome+' ($'+(o.simbolo||'Indispon\u00edvel')+') - '+estadoTexto}
 atualizarCartoesRadar();
 const atual=radarSelecionado&&l.find(o=>o.mint===radarSelecionado);if(atual)mostrarDetalhe(atual);else if(!radarSelecionado)mostrarDetalhe(null);const s=(D.sinais||[])[0],a=$('aviso'),ch=s?s.id+s.estado+(D.agora-s.ts<600):'-';
 if(chaves.a!==ch){const mudou=chaves.a&&s&&!String(chaves.a).startsWith(s.id);chaves.a=ch;const seq=++avisoSeq;const pintar=()=>{if(seq!==avisoSeq)return;
  const id=s?String(s.id):'-';avisoAtualId=id;a.className='cx aviso'+(s&&mudou?' novo':'');a.hidden=false;a.setAttribute('aria-hidden','false');
  if(!s)a.replaceChildren(el('h3',null,'Scanner ativo'),el('p',null,'Ainda sem nova moeda detetada.'));
  else{const simbolo='$'+(s.simbolo||'Moeda');a.replaceChildren(el('h3',null,'Nova Moeda Detectada'),el('p',null,simbolo+' adicionada \u00e0 lista de moedas analisadas.'));const bt=el('button','bt','Ver na lista');bt.type='button';bt.onclick=()=>{selecionarMoeda(s.mint,true);fecharAviso(id)};a.append(bt)}
  a.append(botaoFecharScanner('Fechar notifica\u00e7\u00e3o',()=>fecharAviso(id)));a.hidden=avisoFechadoId===id;a.setAttribute('aria-hidden',String(a.hidden));requestAnimationFrame(()=>a.classList.remove('saindo'))};if(a.childElementCount){a.classList.add('saindo');setTimeout(pintar,130)}else pintar()}requestAnimationFrame(fio)}
/* ---------- melhor oportunidade ---------- */
function velas(h){const s=$('velas');if(!s)return;if(!h||h.length<2){s.innerHTML='';return}
 if(h.length<10){const mn=Math.min(...h),am=(Math.max(...h)-mn)||1,k=h[h.length-1]>=h[0]?'#22e0c8':'#ff7a5c',q=h.map((v,i)=>[(8+i/(h.length-1)*224).toFixed(1),(84-(v-mn)/am*74).toFixed(1)]);
  s.innerHTML=`<polyline points="${q.map(a=>a.join(',')).join(' ')}" fill="none" stroke="${k}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/><circle cx="${q[q.length-1][0]}" cy="${q[q.length-1][1]}" r="3.5" fill="${k}"/>`;return}
 const g=Math.max(1,Math.ceil(h.length/30)),cs=[];for(let i=1;i<h.length;i+=g){const f=h.slice(i,i+g),o=h[i-1];cs.push([o,Math.max(o,...f),Math.min(o,...f),f[f.length-1]])}
 const mn=Math.min(...cs.map(c=>c[2])),am=(Math.max(...cs.map(c=>c[1]))-mn)||1,W=240,H=92,w=W/cs.length,y=v=>(H-6-(v-mn)/am*(H-12)).toFixed(1);
 s.innerHTML=cs.map((c,i)=>{const x=i*w+w/2,up=c[3]>=c[0],k=up?'#22e0c8':'#ff7a5c',t=Math.min(y(c[0]),y(c[3])),hh=Math.max(1.5,Math.abs(y(c[0])-y(c[3])));
  return`<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${y(c[1])}" y2="${y(c[2])}" stroke="${k}" stroke-width="1"/><rect x="${(x-w*.3).toFixed(1)}" y="${t}" width="${(w*.6).toFixed(1)}" height="${hh.toFixed(1)}" rx="1" fill="${k}"/>`}).join('')}
function melhor(){const c=$('melhor'),s=melhorSinalRadar();
 const ch=s?JSON.stringify([s.id,s.estado,s.var_h1,s.pontuacao,s.liq,s.vol_h1,D.agora-s.ts<600]):'-';if(chaves.m===ch)return;chaves.m=ch;
 if(!s){c.replaceChildren(el('div','melhor-vazio','A melhor oportunidade aparece aqui quando existir um sinal.'));return}
 const lead=el('div','melhor-lead'),tituloLead=el('h3');tituloLead.append(ico('louro'),'Melhor oportunidade');const moeda=el('div','melhor-moeda'),texto=el('div');texto.append(el('b',null,'$'+(s.simbolo||'Indispon\u00edvel')),el('small',null,'Pontua\u00e7\u00e3o '+(disponivel(s.pontuacao)?Math.round(Number(s.pontuacao))+'/100':'Indispon\u00edvel')));moeda.append(avatar(s,34),texto);lead.append(tituloLead,moeda);c.replaceChildren(lead);
 const metricas=[['Varia\u00e7\u00e3o',disponivel(s.var_h1)?pct(Number(s.var_h1)):'Indispon\u00edvel'],['Pontua\u00e7\u00e3o',disponivel(s.pontuacao)?Math.round(Number(s.pontuacao))+'/100':'Indispon\u00edvel'],['Liquidez',disponivel(s.liq)?curto(s.liq):'Indispon\u00edvel'],['Volume',disponivel(s.vol_h1)?curto(s.vol_h1):'Indispon\u00edvel']];
 for(const[k,v]of metricas){const m=el('div','melhor-metrica');m.append(el('span',null,k),el('b',null,v));c.append(m)}
 c.append(temLink(s)?ligar(el('a','bt melhor-link','DexScreener \u2192'),s):el('span','melhor-link sub','Indispon\u00edvel'))}
/* ---------- restantes vistas ---------- */
function grafico(h,sobe){const g=$('graf');if(!Array.isArray(h)||h.length<2){g.style.display='none';return}g.style.display='block';const W=600,H=120,P=8;const mn=Math.min(...h),am=(Math.max(...h)-mn)||1;
 const pts=h.map((v,i)=>[i/(h.length-1)*W,H-P-(v-mn)/am*(H-2*P)]),d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 const cor=sobe?'#2ee6a8':'#ff6b6b';$('linha').setAttribute('d',d);$('linha').setAttribute('stroke',cor);$('area').setAttribute('d',d+` L${W} ${H} L0 ${H} Z`);
 $('g1').setAttribute('stop-color',cor);$('g2').setAttribute('stop-color',cor)}
function posicoes(){const l=D.posicoes,c=$('pos');$('npos2').textContent=l.length+'/'+D.max_pos+' posições abertas';
 if(!l.length){c.replaceChildren(el('div','cx vazio','Nenhuma posição aberta.'));return}
 const ant=new Map([...c.children].filter(n=>n.dataset.m).map(n=>[n.dataset.m,n]));
 c.replaceChildren(...l.map(p=>{let n=ant.get(p.mint);if(!n){n=el('div','cx posi');n.dataset.m=p.mint;const a=el('div','a');a.append(el('span'),el('span'));
   const b=el('div','b');b.append(el('span'),el('span'));const r=el('div','barra');r.append(el('i'));n.append(a,b,r)}
  const[a,b,r]=n.children;a.children[0].textContent='$'+p.simbolo;a.children[1].textContent=pct(p.var);a.children[1].className=p.var>=0?'sobe':'desce';
  const atual=disponivel(p.tokens)&&Number(p.tokens)>0&&disponivel(p.preco_atual)&&Number(p.preco_atual)>0?usd(Number(p.tokens)*Number(p.preco_atual)):'Indispon\u00edvel';b.children[0].textContent='Investido '+valorOperacaoUsd(p.custo)+' - Atual bruto '+atual+' - '+Math.round(p.min)+' min'+(p.trailing?' - trailing':'');b.children[1].textContent='líquido '+pct(p.liquido);
  const w=Math.min(50,Math.abs(p.var)*2),i=r.firstChild;i.style.width=w+'%';i.style.left=(p.var>=0?50:50-w)+'%';i.style.background=p.var>=0?'var(--vd)':'var(--vm)';return n}))}
const CORES=['#38bdf8','#a78bfa','#2ee6a8','#f7a531'];
function agentes(){const c=$('agentes'),l=D.agentes;if(c.children.length!==l.length)c.replaceChildren(...l.map((a,i)=>{const d=el('div','cx ag');
  d.style.setProperty('--k',CORES[i%4]);d.style.setProperty('--d',(i*.45)+'s');const h=el('h4');h.append(el('span'),a.nome);d.append(h,el('p'));return d}));
 l.forEach((a,i)=>{c.children[i].lastChild.textContent=a.detalhe})}
function alertas(){const l=[];for(const s of D.sinais){l.push({ts:s.ts,h:s.hora,x:'Novo sinal $'+s.simbolo+' · pontuação '+s.pontuacao,k:'var(--ou)'});
  if(s.estado==='fechado')l.push({ts:s.ts_fecho||s.ts,h:s.hora_fecho||'',x:'Fechou $'+s.simbolo+' com '+pct(s.resultado),k:s.resultado>0?'var(--vd)':'var(--vm)'})}
 l.sort((a,b)=>b.ts-a.ts);if(vista==='alertas'&&l[0]&&l[0].ts>alertaVisto){alertaVisto=l[0].ts;try{localStorage.setItem('mb_alerta',alertaVisto)}catch(e){}}
 const n=l.filter(a=>a.ts>alertaVisto).length;document.querySelectorAll('[data-v="alertas"] em').forEach(e=>{e.hidden=!n;e.textContent=n>99?'99+':n});
 const ch=l.length+':'+(l[0]?l[0].ts:0);if(chaves.al===ch)return;chaves.al=ch;
 $('alertas').replaceChildren(...(l.length?l.slice(0,60).map(a=>{const li=el('li','al c');li.style.setProperty('--k',a.k);li.append(el('span',null,a.x),el('span',null,a.h));return li}):[el('li',null,'Ainda sem alertas.')]))}
function historicoOperacoes(){const c=$('acoes'),trades=Array.isArray(D.trades)?D.trades:[],ch=String(D.trades_disponiveis)+JSON.stringify(trades);if(chaves.ops===ch)return;chaves.ops=ch;const itens=[];
 if(D.trades_disponiveis===false)itens.push(el('li','', 'Historico de operacoes Indispon\u00edvel.'));
 else for(const t of [...trades].reverse()){
  const tipo=t.tipo==='COMPRA'?'Compra':t.tipo==='VENDA'?'Venda':'Entrada anulada',li=el('li','trade-row c'),info=el('div'),valor=valorOperacaoUsd(t.valor_usd),preco=precoToken(t.preco_efetivo),tokens=quantidadeTokens(t.tokens);
  info.append(el('strong',null,tipo+' $'+(t.simbolo||'Indispon\u00edvel')),el('small',null,t.data_hora||'Data/hora: Indispon\u00edvel'));
  info.append(el('small',null,(t.tipo==='COMPRA'?'Valor investido: ':'Valor recebido: ')+valor+' | Preco efetivo: '+preco+' | Tokens: '+tokens));
  if(t.tipo==='VENDA')info.append(el('small',null,'Resultado realizado: '+valorOperacaoUsd(t.pnl_usd)+(disponivel(t.pnl_pct)?' ('+Number(t.pnl_pct).toFixed(2)+'%)':'' )+' | Motivo: '+(t.motivo||'Indispon\u00edvel')));
  else if(t.tipo==='COMPRA')info.append(el('small',null,'Custos registados: '+valorOperacaoUsd(t.custos_usd)));
  else info.append(el('small',null,'Estado: anulada | Motivo: '+(t.motivo||'Indispon\u00edvel')));
  li.append(info,el('span','trade-result',valor));itens.push(li)}
 for(const acao of (D.acoes||[]).filter(a=>!/(COMPRA|VENDA)/.test(a)))itens.push(el('li','',acao));
 if(!itens.length)itens.push(el('li',null,D.trades_disponiveis===false?'Historico de operacoes Indispon\u00edvel.':'Ainda sem operacoes.'));c.replaceChildren(...itens)}
function render(){const d=D,st=d.stats||{},modo=d.modo==='demo'?'demo':d.modo==='simulacao'?'paper':null;[...d.sinais,...d.recentes].forEach(o=>{o.var_h1=disponivel(o.var_h1)?Number(o.var_h1):null;o.pontuacao=disponivel(o.pontuacao)?Math.round(Number(o.pontuacao)):null;o.ts=+o.ts||0});
 $('cmode').textContent=modo==='demo'?'Demo · mercado inventado':modo==='paper'?'Paper Trading · simulação':'Modo: Indisponível';$('cmode').className='chip '+(modo==='demo'?'demo':'modo');$('rot-total').textContent='Valor total · '+(modo==='demo'?'Demo':modo==='paper'?'Paper Trading':'Indisponível');$('relogio').textContent=typeof d.hora==='string'&&d.hora.trim()?d.hora:'Indisponível';
 $('estado-bot').textContent=valorOuIndisponivel(d.agora,()=> 'A verificar');
 $('ultima-atualizacao').textContent=disponivel(d.agora)&&Number(d.agora)>0?new Date(Number(d.agora)*1000).toLocaleTimeString('pt-PT'):'Indisponível';
 $('estado-bot').textContent=d.estado_bot||'A responder';
 if(disponivel(d.hoje))conta($('s1'),d.hoje,mil);else $('s1').textContent='Indisponível';if(disponivel(d.sinais_24h))conta($('s2'),d.sinais_24h,mil);else $('s2').textContent='Indisponível';if(Array.isArray(d.posicoes))conta($('s3'),d.posicoes.length,mil);else $('s3').textContent='Indisponível';
 $('s4').textContent=disponivel(st.fechados)&&st.fechados>0&&disponivel(st.acerto)?pt(st.acerto,1)+'%':'Indisponível';$('s4t').textContent=disponivel(st.fechados)&&st.fechados>0?'Taxa de acerto · '+st.fechados+' trades':'Taxa de acerto';
 linhas();radar();melhor();todas();alertas();renderManualProposals();
 conta($('total'),d.total,usd);conta($('saldo'),d.saldo,usd);conta($('real'),st.realizado,sinalado);$('real').className=disponivel(st.realizado)&&st.realizado<0?'desce':'sobe';
 $('trades').textContent=valorOuIndisponivel(st.fechados,mil);$('gp').textContent=disponivel(st.ganhos)&&disponivel(st.perdas)?mil(st.ganhos)+' / '+mil(st.perdas):'Indisponível';
 const dl=$('delta');dl.className='delta '+(disponivel(d.pnl)&&d.pnl<0?'desce':'sobe');dl.textContent=disponivel(d.pnl)&&disponivel(d.pnl_pct)?sinalado(d.pnl)+' ('+pct(d.pnl_pct,2)+')':'Indisponível';
 grafico(Array.isArray(d.historico)?d.historico.slice():[],disponivel(d.pnl)&&d.pnl>=0);if(Array.isArray(d.posicoes))posicoes();else $('pos').replaceChildren(el('div','cx vazio','Indisponível'));agentes();
 historicoOperacoes();
 const configAtual=JSON.stringify(d.config);if(chaves.cf!==configAtual){chaves.cf=configAtual;$('config').replaceChildren(...d.config.map(([k,v])=>{const li=el('li');li.append(el('span',null,k),el('b',null,v));return li}))}
 chaves.pronto=1}
function renderManualProposals(){const c=$('manual-proposals'),ps=Array.isArray(D.propostas_paper)?D.propostas_paper:[];c.replaceChildren();c.hidden=!ps.length;if(!ps.length)return;c.append(el('h3',null,'Confirmação manual necessária'));for(const p of ps){const card=el('div','manual-proposal'),v=(x)=>x===null||x===undefined||x===''?'Indisponível':String(x);card.append(el('b',null,'Proposta Paper '+v(p.proposal_id)),el('span',null,'Mint: '+v(p.mint)),el('span',null,'Par: '+v(p.pair_address)),el('span',null,'Preço de referência USD: '+v(p.reference_price_usd)),el('span',null,'Slippage base configurado (%): '+v(p.slippage_base_pct)),el('span',null,'Slippage base aplicado (%): '+v(p.slippage_base_applied_pct)),el('span',null,'Impacto de mercado aplicado (%): '+v(p.market_impact_pct)),el('span',null,'Slippage total aplicado (%): '+v(p.slippage_total_pct)),el('span',null,'Preço efetivo simulado USD: '+v(p.effective_price_usd)),el('span',null,'Taxa estimada ('+v(p.taxa_pct)+'%): '+v(p.estimated_fee_usd)+' USD'),el('strong',null,'Débito total USD: '+v(p.debit_total_usd)),el('span',null,'Quantidade líquida estimada: '+v(p.net_tokens)),el('span',null,'Liquidez observada usada USD: '+v(p.liquidity_observed_usd)+' · '+v(p.liquidity_status)),el('span',null,'Fallback de slippage: '+(p.impact_fallback?'95% aplicado pela regra existente':'Não')),el('span',null,'Score: '+(disponivel(p.score)?p.score+'/100':'Indisponível')),el('span',null,'Análise: '+v(p.analysis_summary)),el('small',null,'Criada UTC: '+v(p.created_at_utc)+' · Expira UTC: '+v(p.expires_at_utc)),el('small',null,'Aprovação exclusiva no terminal; código secreto não apresentado nesta página'));c.append(card)}}
function mudar(v){vista=VISTAS.some(x=>x[0]===v)?v:'scanner';
 document.querySelectorAll('.vista').forEach(n=>n.classList.toggle('on',n.id==='v-'+vista));
 document.querySelectorAll('[data-v]').forEach(b=>{b.classList.toggle('on',b.dataset.v===vista);if(b.dataset.v===vista&&b.closest('.fundo'))b.scrollIntoView({block:'nearest',inline:'center'})});
 try{history.replaceState(null,'','#'+vista)}catch(e){}scrollTo(0,0);if(D){alertas();requestAnimationFrame(fio)}}
function mudarAba(a){aba=['all','analise','aprovada','rejeitada'].includes(a)?a:'all';document.querySelectorAll('[data-a]').forEach(b=>{b.classList.toggle('on',b.dataset.a===aba);b.setAttribute('aria-pressed',String(b.dataset.a===aba))});chaves.l=null;if(D)linhas()}
for(const id of['nav','fundo'])$(id).replaceChildren(...VISTAS.map(([v,nome])=>{const b=el('button');b.dataset.v=v;b.append(ico(v),id==='fundo'&&v==='config'?'Config.':id==='fundo'&&v==='oportunidades'?'Oportun.':nome);
 if(v==='alertas'){const e=el('em');e.hidden=true;b.append(e)}b.onclick=()=>mudar(v);return b}));
document.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>mudarAba(b.dataset.a));
addEventListener('resize',()=>requestAnimationFrame(fio));
mudar(location.hash.slice(1));mudarAba('all');
$('varre').style.animationDelay=(-(performance.now()%T))+'ms';
function marcarDesatualizado(){if(!D)return;if(!disponivel(D.agora)||Number(D.agora)<=0){$('cvivo').className='chip fora';$('tvivo').textContent='Dados indisponíveis';$('estado-bot').textContent='Indisponível';return}const limite=Number(D.limite_atualizacao_seg)||30,antigo=(Date.now()/1000-Number(D.agora))>limite;
 if(antigo){$('cvivo').className='chip fora';$('tvivo').textContent='Dados desatualizados';$('estado-bot').textContent='Dados desatualizados'}else if(D){$('cvivo').className='chip vivo';$('tvivo').textContent='Ao vivo';$('estado-bot').textContent=D.estado_bot||'A responder'}}
async function ciclo(){try{const r=await fetch('/api/estado',{cache:'no-store'});if(!r.ok)throw 0;D=await r.json();render();marcarDesatualizado()}
 catch(e){$('cvivo').className='chip fora';$('tvivo').textContent='Sem resposta';$('estado-bot').textContent='Bot deixou de responder'}
 setTimeout(ciclo,2000)}
ciclo();
</script>
</body>
</html>
'''


CAMINHO_PAGINA_NEURAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "dashboard_neural.html")


def _pagina_neural():
    """Le o Dashboard Neural (HTML estatico, so leitura). Devolve None se nao existir ou for invalido."""
    try:
        with open(CAMINHO_PAGINA_NEURAL, "r", encoding="utf-8") as ficheiro:
            pagina = ficheiro.read(2_000_000)
    except (OSError, UnicodeError):
        return None
    return pagina if pagina.startswith("<!DOCTYPE html>") and "/api/estado" in pagina else None


IMAGENS_ASSETS = ("/assets/mascote_oficial.png", "/assets/mascote_chifres.png")  # a 2.a e a do tema "Chifres"


class _ServidorWeb(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # no Windows, reutilizar deixava dois bots abrir a mesma porta


class _PedidoWeb(BaseHTTPRequestHandler):
    timeout = 15  # larga ligacoes paradas

    def do_GET(self):
        caminho = self.path.split("?", 1)[0]
        if caminho in ("/", "/index.html"):
            # Dashboard Neural (so leitura); se o ficheiro faltar ou falhar, serve o dashboard classico.
            neural = _pagina_neural()
            if neural is not None:
                neural = neural.replace("__MEMEBOT_TOKEN__", getattr(self.server.bot, "dashboard_token", None) or "")
            corpo, tipo = (neural or PAGINA_HTML).encode("utf-8"), "text/html; charset=utf-8"
        elif caminho == "/classico":
            corpo, tipo = PAGINA_HTML.encode("utf-8"), "text/html; charset=utf-8"
        elif caminho == "/api/estado":
            corpo, tipo = self.server.bot.snapshot.encode("utf-8"), "application/json; charset=utf-8"
        elif caminho in IMAGENS_ASSETS:  # so imagens desta lista fixa; nenhum outro ficheiro da pasta e servido
            caminho_mascote = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", caminho.rsplit("/", 1)[1])
            try:
                with open(caminho_mascote, "rb") as ficheiro:
                    corpo = ficheiro.read()
            except OSError:
                self.send_error(404)
                return
            tipo = "image/png"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(corpo)

    def do_POST(self):
        """Unico endpoint de escrita: aprovar/recusar uma proposta Paper JA criada pelo bot."""
        if self.path.split("?", 1)[0] != "/api/proposta/decisao":
            self.send_error(404)
            return
        try:
            codigo, resposta = _decisao_dashboard(self)
        except Exception:
            log.exception("Falha ao processar decisao do dashboard; nada foi executado")
            codigo, resposta = 500, {"ok": False, "mensagem": "erro interno; nada foi executado"}
        corpo = json.dumps(resposta, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):  # nao enche a consola
        pass


DECISAO_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _decisao_dashboard(pedido):
    """Valida e aplica uma decisao do dashboard. Devolve (codigo_http, dict). Falha sempre fechada."""
    bot = pedido.server.bot
    recusa = lambda codigo, msg: (codigo, {"ok": False, "mensagem": msg})
    token = getattr(bot, "dashboard_token", None)
    if not token:
        return recusa(403, "decisoes no dashboard indisponiveis")
    porta = pedido.server.server_address[1]
    locais = (f"127.0.0.1:{porta}", f"localhost:{porta}")
    if pedido.headers.get("Host", "") not in locais:
        return recusa(403, "endereco nao permitido")
    if pedido.headers.get("Origin", "") not in tuple("http://" + h for h in locais):
        return recusa(403, "origem nao permitida")
    if not hmac.compare_digest(str(pedido.headers.get("X-MemeBot-Token", "")), token):
        return recusa(403, "chave da sessao invalida; recarrega o dashboard")
    if not str(pedido.headers.get("Content-Type", "")).startswith("application/json"):
        return recusa(415, "formato invalido")
    try:
        tamanho = int(pedido.headers.get("Content-Length", "0"))
    except ValueError:
        tamanho = -1
    if not 0 < tamanho <= 2048:
        return recusa(400, "pedido invalido")
    try:
        dados = json.loads(pedido.rfile.read(tamanho).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return recusa(400, "JSON invalido")
    if not isinstance(dados, dict):
        return recusa(400, "pedido invalido")
    proposal_id, decisao = dados.get("proposal_id"), dados.get("decisao")
    if not isinstance(proposal_id, str) or not DECISAO_ID_RE.fullmatch(proposal_id) or decisao not in ("aprovar", "recusar"):
        return recusa(400, "proposta ou decisao invalida")
    cfg = getattr(bot, "cfg", {}) or {}
    if (cfg.get("mode") != "paper" or cfg.get("live_trading_enabled") is not False
            or cfg.get("manual_confirmation_required") is not True):
        return recusa(409, "modo Paper com confirmacao manual indisponivel; nada foi feito")
    emergencia = getattr(bot, "_emergency_stop_is_active", None)
    if decisao == "aprovar" and (not callable(emergencia) or emergencia()):
        return recusa(409, "paragem de emergencia ativa; aprovacao recusada")
    gestor = getattr(bot, "manual_approvals", None)
    if not isinstance(gestor, ManualApprovalManager):
        return recusa(409, "gestor de propostas indisponivel")
    ok, mensagem = gestor.decidir_dashboard(proposal_id, decisao)
    texto = f"Proposta PAPER {proposal_id[:12]} {'aprovada' if decisao == 'aprovar' else 'recusada'} no dashboard"
    if ok:
        log.info("%s", texto)  # o ciclo principal regista a acao e a auditoria ao processar a decisao
    return (200 if ok else 409), {"ok": ok, "mensagem": mensagem}


def ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # nao envia nada; so descobre o IP da rede local
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def iniciar_painel_web(bot):
    # Liga o servidor web numa thread a parte. Devolve o endereco ou None.
    cfg = bot.cfg
    if not cfg.get("painel_web"):
        return None
    try:
        srv = _ServidorWeb(("127.0.0.1", cfg["porta_web"]), _PedidoWeb)
    except OSError as e:
        log.error("Nao consegui abrir a porta %s: %s", cfg["porta_web"], e)
        return f"INDISPONIVEL - a porta {cfg['porta_web']} esta ocupada (fecha o outro bot ou muda 'porta_web' no CONFIG)"
    srv.bot = bot
    bot.servidor_web = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    porta = srv.server_address[1]
    url = f"http://localhost:{porta}"
    return url


# ============================================================================
# MODO DEMO - mercado inventado, so para veres a plataforma a funcionar
# ============================================================================
class MercadoDemo:
    # Substitui a internet por tokens e precos aleatorios. NAO diz nada sobre a estrategia.
    NOMES = ["LUNA", "KOI", "PIXEL", "NEKO", "ORBIT", "MANGO", "ZEN", "TURBO", "FROG", "NOVA", "ECHO", "BOLT",
             "WAVE", "MOSS", "RUBY", "DASH", "FIG", "COMET", "PANDA", "ONYX"]

    def __init__(self):
        self.pares = {}
        self.fila = []
        self.n = 0
        random.shuffle(self.NOMES)

    def _novo(self):
        self.n += 1
        nome = self.NOMES[(self.n - 1) % len(self.NOMES)] + (str(1 + self.n // len(self.NOMES)) if self.n > len(self.NOMES) else "")
        mint = ("Demo%d" % self.n + "d" * 44)[:44]
        bom = random.random() < 0.6
        liq = random.uniform(30000, 120000) if bom else random.uniform(4000, 30000)
        c = random.randint(180, 420)
        self.pares[mint] = {
            "chainId": "solana", "pairAddress": "par" + mint[:10], "url": "",
            "baseToken": {"address": mint, "symbol": nome},
            "priceUsd": random.uniform(0.00005, 0.02), "liquidity": {"usd": liq},
            "marketCap": liq * random.uniform(3, 14), "pairCreatedAt": int((agora() - random.uniform(20, 300) * 60) * 1000),
            "volume": {"m5": random.uniform(600, 6000), "h1": liq * random.uniform(0.3, 2.2)},
            "txns": {"m5": {"buys": random.randint(8, 40), "sells": random.randint(5, 25)},
                     "h1": {"buys": c, "sells": int(c * random.uniform(0.45, 1.1))}},
            "priceChange": {"m5": random.uniform(-6, 18), "h1": random.uniform(-10, 70),
                            "h6": random.uniform(-20, 140), "h24": random.uniform(-30, 300)},
            "_tend": random.gauss(0.0, 0.012),
        }
        self.fila.append(mint)
        del self.fila[:-30]

    def get_json(self, url, grupo="pares", tentativas=None):
        if url == URL_PERFIS:
            if random.random() < 0.7:
                self._novo()
            return [{"chainId": "solana", "tokenAddress": m} for m in self.fila]
        if url == URL_BOOSTS:
            return []
        if "/tokens/v1/solana/" in url:
            saida = []
            for mint in url.rsplit("/", 1)[1].split(","):
                par = self.pares.get(mint)
                if par:
                    par["priceUsd"] *= math.exp(random.gauss(par["_tend"], 0.022))
                    if random.random() < 0.0006:  # de vez em quando, um rug
                        par["liquidity"]["usd"] *= 0.02
                        par["priceUsd"] *= 0.05
                    saida.append(par)
            return saida
        if "rugcheck" in url:
            return {"score_normalised": random.choice([4, 8, 15, 25, 40]), "risks": []}
        return None


def preparar_demo(cfg):
    pasta = tempfile.mkdtemp(prefix="paperbot_demo_")
    cfg.update(ficheiro_estado=os.path.join(pasta, "estado.json"), ficheiro_trades=os.path.join(pasta, "trades.csv"),
               ficheiro_log=os.path.join(pasta, "bot.log"), ficheiro_percursos=os.path.join(pasta, "percursos.jsonl"),
               percurso_minutos=3, intervalo_scanner_seg=4, intervalo_risco_seg=2,
               telegram_resumo_min=0, tempo_maximo_min=4)
    return pasta


# ============================================================================
# BOT - junta os 4 agentes, o painel e o ciclo principal
# ============================================================================
class Bot:
    CONTADORES_SCANNER = ("tokens_received", "unique_new", "reanalyzed", "no_pair",
                          "analysis_completed", "rejected", "pending_data",
                          "technical_errors", "approved")

    def __init__(self, cfg=None, process_lock=None):
        self.cfg = cfg or CONFIG
        if not self.cfg.get("limites_operacionais_ativos") or not self.cfg.get("_operational_config_path"):
            project_config = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "config_operacoes.json")
            aplicar_config_operacional(self.cfg, project_config)
        self._owns_process_lock = process_lock is None
        self.process_lock = process_lock or ProcessInstanceLock(self.cfg["ficheiro_estado"])
        if not self.process_lock.acquire():
            raise RuntimeError("Outra instancia ja esta a operar este estado Paper Trading")
        try:
            self._inicializar_com_lock()
        except Exception:
            self.close()
            raise

    def _inicializar_com_lock(self):
        """No shared state or database resource is opened before the instance lock."""
        self._closed = False
        reset_marker = reset_pending_path(self.cfg["ficheiro_estado"])
        if os.path.lexists(reset_marker):
            raise StateIntegrityError(
                f"reset interrompido ou por validar; dados preservados e arranque bloqueado: {reset_marker}"
            )
        self.estado = carregar_estado(self.cfg)
        self.paper_operations = PaperOperationStore(
            self.cfg["ficheiro_estado"], self.cfg["ficheiro_trades"], CABECALHO_CSV
        )
        try:
            self.paper_operations.recover(self.estado)
        except PaperOperationError:
            log.exception("Diario Paper ambiguo; novas compras bloqueadas ate recuperacao")
        self._emergency_lock = threading.RLock()
        self._emergency_active = False
        audit_path = os.path.join(os.path.dirname(os.path.abspath(self.cfg["ficheiro_estado"])),
                                  "paper_audit.jsonl")
        self.paper_audit = PaperAuditJournal(audit_path)
        if self.paper_audit.blocked:
            log.error("Diario de auditoria Paper inválido; novas propostas e compras bloqueadas: %s",
                      self.paper_audit.error)
        self._inicializar_paragem_emergencia()
        self._reconciliar_auditoria_operacoes()
        self.http = ClienteHTTP(self.cfg)
        self.telegram = Telegram()
        self.manual_approvals = ManualApprovalManager(
            ttl_seconds=90, terminal_command_handler=self._comando_emergencia_terminal)
        # Chave secreta por arranque: so a pagina servida por este bot a conhece (protege contra pedidos de outros sites).
        self.dashboard_token = secrets.token_urlsafe(32)
        self._latest_market_pairs = {}
        self.scanner = Scanner(self.cfg, self.http)
        self.scanner.entre_pedidos = self._vigiar_posicao_no_scanner
        self.analista = Analista(self.cfg, self.http)
        self.executor = Executor(self.cfg, self.estado, self)
        self.risco = Risco(self.cfg, self.estado, self.http, self.executor)
        # Preco rapido (Jupiter, so leitura). Inativo por defeito; main() ativa-o no modo Paper com mercado real.
        self.preco_rapido = None
        self._pares_risco = {}
        self._avisos_preco_rapido = {}
        self._avisos_tg_rapido = {}      # (mint, e_sem_rota) -> hora do ultimo aviso Telegram de preco rapido em falha
        self._ultimo_ciclo_rapido = 0.0  # para o scanner saber quando tem de ceder a vez ao preco rapido
        # Pontos finos do laboratorio: instancia Jupiter separada (main() cria-a no modo Paper com mercado real).
        self.preco_fino = None
        self._fino_ultimo = {}           # mint -> hora da ultima cotacao fina (para rodar pelos sinais)
        self._fino_pausa_ate = 0.0       # depois de uma falha de rede/Jupiter, nao insiste ate esta hora
        self.tg_decisoes = None          # botoes Aprovar/Recusar no Telegram (correr() liga-os)
        self._vendas_manuais = queue.Queue()  # mints com venda manual pedida (o ciclo principal e que vende)
        self._ultimo_motivo_log = {}
        self.analisados = 0
        self.ultimo_scan = "ainda nao correu"
        self.ultimo_resumo_tg = agora()
        self.ultima_analise = "a espera do primeiro token"
        self.recentes = []
        self.em_analise = []
        self.tokens_sessao = set()
        self.descobertas = []
        self._inicializar_contadores_scanner()
        self.modo = "simulacao"
        self.url_web = None
        self.servidor_web = None
        self.snapshot = "{}"
        self._fx_results = queue.Queue(maxsize=1)
        self._fx_fetch_active = False
        self._fx_next_retry = 0.0
        self._fx_status = "nao configurado"
        self._fx_rate_persisted = False
        sessao_inicial = self.estado.get("operational_session")
        if isinstance(sessao_inicial, dict):
            self._fx_rate_persisted = any(sessao_inicial.get(key) is not None for key in (
                "fx_usd_per_eur", "fx_reference_date", "fx_locked_at_utc", "fx_source"
            ))
        # Observacao extra (so leitura). Inativa por defeito; main() ativa-a no modo Paper com mercado real.
        self.obs_extra = ObservadorExtra(ativo=False, logger=log)
        estado_path = os.path.abspath(self.cfg.get("ficheiro_estado", "estado.json"))
        market_db = os.path.join(os.path.dirname(estado_path), "smart_filters_market.db")
        protection_db = os.path.join(os.path.dirname(estado_path), "smart_protection_observability.db")
        try:
            self.smart_protection_observability = SmartProtectionObservability(protection_db)
            self.risco.observability = self.smart_protection_observability
        except Exception:
            self.smart_protection_observability = None
            log.exception("Observabilidade Smart Protection indisponível; o bot continua normalmente")
        try:
            self.market_snapshots = MarketSnapshotRecorder(market_db)
        except Exception:
            self.market_snapshots = None
            log.exception("Snapshots Smart Filters indisponíveis; o bot continua sem histórico de mercado")
        self.atualizar_snapshot()

    def _inicializar_paragem_emergencia(self):
        with self._emergency_lock:
            stored = self.estado.get("emergency_stop")
            if not isinstance(stored, dict):
                stored = {"active": False, "stop_id": None, "changed_at_utc": None, "reason": None}
                self.estado["emergency_stop"] = stored
            self._emergency_active = stored.get("active") is True
            if self.paper_audit.blocked:
                return
            try:
                events = self.paper_audit.verify()["events"]
            except PaperAuditError:
                return
            emergency_events = [event for event in events
                                if event.get("event") in ("emergency_stop_activated", "emergency_stop_rearmed")]
            if emergency_events:
                latest = emergency_events[-1]
                if latest["event"] == "emergency_stop_activated":
                    fields = latest.get("fields", {})
                    self._emergency_active = True
                    if stored.get("active") is not True:
                        self.estado["emergency_stop"] = {
                            "active": True, "stop_id": fields.get("stop_id"),
                            "changed_at_utc": latest.get("occurred_at_utc"),
                            "reason": fields.get("reason"),
                        }
                        try:
                            guardar_estado(self.cfg, self.estado, strict=True)
                        except Exception:
                            log.exception("Paragem Paper recuperada do diário, mas estado não pôde ser sincronizado")
                elif stored.get("active") is not True:
                    self._emergency_active = False
            if stored.get("active") is True:
                self._emergency_active = True

    def _emergency_stop_is_active(self):
        return (self._emergency_active
                or self.estado.get("emergency_stop", {}).get("active") is True
                or self.paper_audit.blocked)

    def _comando_emergencia_terminal(self, action, argument):
        """Terminal-only, latched Paper stop; Risk keeps managing existing positions."""
        if not self.cfg.get("emergency_stop_enabled"):
            return False, "emergency_stop_enabled não está ativo; compras permanecem bloqueadas"
        with self.executor._purchase_lock:
            with self._emergency_lock:
                current = self.estado.get("emergency_stop", {})
                if action == "activate":
                    reason = sanitize_audit_text(argument)
                    if not reason:
                        return False, "indique um motivo não vazio: PARAR_PAPER <motivo>"
                    if self._emergency_stop_is_active() and current.get("stop_id"):
                        return True, f"já ativa ({current['stop_id']}); motivo preservado"
                    stop_id = uuid.uuid4().hex
                    record = {"active": True, "stop_id": stop_id,
                              "changed_at_utc": datetime.now(timezone.utc).isoformat(),
                              "reason": reason}
                    self._emergency_active = True
                    self.estado["emergency_stop"] = record
                    audit_ok = False
                    try:
                        self.paper_audit.append("emergency_stop_activated", {
                            "active": True, "stop_id": stop_id, "reason": reason,
                            "source": "terminal",
                        })
                        audit_ok = True
                    except PaperAuditError:
                        log.exception("Paragem ativada, mas diário B13 indisponível")
                    try:
                        guardar_estado(self.cfg, self.estado, strict=True)
                    except Exception as exc:
                        # Keep the in-process latch active even if durable storage fails.
                        return False, f"ativa em memória; falha ao persistir ({type(exc).__name__}); compras bloqueadas"
                    return True, (f"ativa ({stop_id}); posições existentes continuam sob Risco"
                                  if audit_ok else f"ativa ({stop_id}); diário B13 indisponível, compras bloqueadas")

                if action != "rearm":
                    return False, "ação desconhecida"
                parts = str(argument or "").strip().split(maxsplit=1)
                if len(parts) != 2 or not parts[1].strip():
                    return False, "formato: REARMAR_PAPER <ID_PARAGEM> <motivo>"
                stop_id, reason = parts[0], sanitize_audit_text(parts[1])
                if not self._emergency_stop_is_active() or stop_id != current.get("stop_id"):
                    return False, "ID da paragem inválido ou paragem não ativa"
                record = {"active": False, "stop_id": stop_id,
                          "changed_at_utc": datetime.now(timezone.utc).isoformat(),
                          "reason": reason}
                self.estado["emergency_stop"] = record
                try:
                    guardar_estado(self.cfg, self.estado, strict=True)
                except Exception as exc:
                    self.estado["emergency_stop"] = current
                    self._emergency_active = True
                    return False, f"rearmamento recusado; falha ao persistir ({type(exc).__name__})"
                try:
                    self.paper_audit.append("emergency_stop_rearmed", {
                        "active": False, "stop_id": stop_id, "reason": reason,
                        "source": "terminal",
                    })
                except PaperAuditError as exc:
                    self.estado["emergency_stop"] = current
                    self._emergency_active = True
                    try:
                        guardar_estado(self.cfg, self.estado, strict=True)
                    except Exception:
                        log.exception("Falha a restaurar latch de emergência após erro do diário")
                    return False, f"rearmamento bloqueado pelo diário de auditoria ({exc})"
                self._emergency_active = False
                return True, f"rearmada por comando terminal; motivo: {reason}"

    def _reconciliar_auditoria_operacoes(self):
        if self.paper_audit.blocked or self.paper_operations.blocked:
            return
        try:
            operation_audit = self.paper_operations.audit()
            audit_events = self.paper_audit.verify()["events"]
            operations = {operation.get("operation_id"): operation
                          for operation in operation_audit.get("operations", [])}
            committed = {}
            for event in audit_events:
                if event.get("event") != "execution_committed":
                    continue
                fields = event.get("fields", {})
                operation_id = fields.get("operation_id")
                if not operation_id:
                    continue
                operation = operations.get(operation_id)
                try:
                    event_amount = _decimal_canonico(fields.get("amount_usd"))
                    paper_amount = _decimal_canonico(
                        operation.get("trade_row", {}).get("debito_total_usd")
                    ) if operation else None
                except (TypeError, ValueError, InvalidOperation):
                    event_amount, paper_amount = None, None
                if (operation is None or operation.get("operation_type") != "BUY"
                        or operation.get("metadata", {}).get("proposal_id") != fields.get("proposal_id")
                        or operation.get("trade_row", {}).get("mint") != fields.get("mint")
                        or event_amount is None or paper_amount is None
                        or event_amount != paper_amount):
                    raise PaperOperationError(
                        f"evento execution_committed diverge do diario Paper: {operation_id}"
                    )
                committed[operation_id] = fields.get("proposal_id")
            for operation in operation_audit.get("operations", []):
                if (operation.get("operation_type") == "BUY"
                        and operation.get("metadata", {}).get("proposal_id")):
                    expected_proposal = operation["metadata"]["proposal_id"]
                    existing_proposal = committed.get(operation["operation_id"])
                    if existing_proposal is not None:
                        if existing_proposal != expected_proposal:
                            raise PaperOperationError(
                                f"proposal_id diverge para operacao {operation['operation_id']}"
                            )
                        continue
                    self.paper_audit.append("execution_committed", {
                        "proposal_id": expected_proposal,
                        "operation_id": operation["operation_id"],
                        "mint": operation.get("trade_row", {}).get("mint"),
                        "amount_usd": operation.get("trade_row", {}).get("debito_total_usd"),
                        "source": "paper_journal_recovery",
                    })
        except (PaperOperationError, PaperAuditError):
            log.exception("Reconciliação B13 falhou; novas compras bloqueadas")
            if isinstance(self.paper_audit, PaperAuditJournal):
                self.paper_audit.block("falha na reconciliação com o diário Paper")

    def close(self):
        """Close owned writers and release this instance's shared-state lock."""
        if getattr(self, "_closed", False):
            if getattr(self, "process_lock", None) is not None:
                self.process_lock.release()
            return
        self._closed = True
        try:
            if getattr(self, "manual_approvals", None) is not None:
                self.manual_approvals.close()
        except Exception:
            log.exception("Erro ao fechar confirmacoes manuais pendentes")
        try:
            if getattr(self, "market_snapshots", None) is not None:
                self.market_snapshots.close()
        except Exception:
            log.exception("Erro ao fechar snapshots de mercado")
        try:
            if getattr(self, "smart_protection_observability", None) is not None:
                self.smart_protection_observability.close()
        except Exception:
            log.exception("Erro ao fechar observabilidade Smart Protection")
        if getattr(self, "process_lock", None) is not None:
            self.process_lock.release()

    def _taxa_bce_info(self):
        """Return status and conversion data without doing network I/O."""
        if not self.cfg.get("limites_operacionais_ativos"):
            return {"status": "nao configurado", "rate": None, "reference_date": None, "cap_usd": None}
        if self.cfg.get("erro_config_operacional"):
            return {"status": "configuracao invalida", "rate": None, "reference_date": None, "cap_usd": None}
        sessao = self.estado.get("operational_session")
        if not isinstance(sessao, dict):
            status = "a obter" if self._fx_fetch_active else "indisponivel"
            return {"status": status, "rate": None, "reference_date": None, "cap_usd": None}
        has_fx_fields = any(sessao.get(key) is not None for key in (
            "fx_usd_per_eur", "fx_reference_date", "fx_locked_at_utc", "fx_source"
        ))
        if not has_fx_fields:
            status = "a obter" if self._fx_fetch_active else "indisponivel"
            return {"status": status, "rate": None, "reference_date": None, "cap_usd": None}
        if not self._fx_rate_persisted:
            return {"status": "nao persistida", "rate": None,
                    "reference_date": sessao.get("fx_reference_date"), "cap_usd": None}
        if not sessao.get("fx_usd_per_eur"):
            return {"status": "invalida", "rate": None,
                    "reference_date": sessao.get("fx_reference_date"), "cap_usd": None}
        try:
            rate = validate_locked_session_rate(sessao)
            cap = usd_cap_from_eur(self.cfg["max_eur_per_operation"], rate)
            return {"status": "valida", "rate": format(rate, "f"),
                    "reference_date": sessao.get("fx_reference_date"), "cap_usd": float(cap)}
        except (EcbRateError, KeyError, TypeError, ValueError) as erro:
            status = "expirada" if "demasiado antiga" in str(erro) else "invalida"
            return {"status": status, "rate": None,
                    "reference_date": sessao.get("fx_reference_date"), "cap_usd": None}

    def _guardar_cotacoes_recentes(self, pares):
        if not isinstance(pares, dict):
            return
        observado_em = float(agora())
        for mint, lista in pares.items():
            if isinstance(lista, list):
                self._latest_market_pairs[mint] = (observado_em, copy.deepcopy(lista))
            else:
                # A failure or missing response invalidates the prior quote for approval.
                self._latest_market_pairs[mint] = (observado_em, None)

    def _cotacao_atual_para_proposta(self, proposta):
        mint = proposta["mint"]
        cached = self._latest_market_pairs.get(mint)
        if not cached:
            return None, "cotacao atual indisponivel"
        observado_em, lista = cached
        max_age = max(30, 3 * min(self.cfg["intervalo_risco_seg"], self.cfg["intervalo_scanner_seg"]))
        if lista is None or not isinstance(lista, list):
            return None, "fonte de cotacao sem resposta"
        if float(agora()) - observado_em > max_age:
            return None, "cotacao demasiado antiga"
        par = next((p for p in lista if isinstance(p, dict)
                    and str(p.get("pairAddress") or "") == proposta["pair_address"]
                    and str(obter(p, "baseToken", "address") or "") == mint), None)
        if par is None:
            return None, "par aprovado ja nao esta disponivel"
        return par, ""

    def _worker_taxa_bce(self):
        try:
            self._fx_results.put((fetch_ecb_rate(), None))
        except EcbRateError as erro:
            self._fx_results.put((None, str(erro)))
        except Exception as erro:
            self._fx_results.put((None, f"erro inesperado: {type(erro).__name__}"))

    def _atualizar_taxa_bce(self):
        """Fetch asynchronously for initial lock or expired rate, persisting before use."""
        if not self.cfg.get("limites_operacionais_ativos"):
            return
        try:
            resultado, erro = self._fx_results.get_nowait()
        except queue.Empty:
            resultado = erro = None
        else:
            self._fx_fetch_active = False
            self._fx_next_retry = time.monotonic() + 300.0
            if resultado:
                try:
                    self.paper_operations.recover(self.estado)
                except PaperOperationError as erro_recuperacao:
                    resultado = None
                    erro = f"estado Paper ambiguo: {erro_recuperacao}"
            if resultado:
                sessao = self.estado.setdefault("operational_session", {})
                info_atual = self._taxa_bce_info()
                pode_instalar = info_atual["status"] in ("indisponivel", "nao persistida", "expirada")
                if pode_instalar:
                    try:
                        nova_taxa = validate_rate(resultado["rate_usd_per_eur"], resultado["reference_date"])
                        if resultado.get("source") != ECB_SOURCE:
                            raise EcbRateError("origem da taxa recebida nao corresponde ao BCE")
                        anterior = dict(sessao)
                        if anterior.get("fx_usd_per_eur") and info_atual["status"] == "expirada":
                            data_antiga = str(anterior.get("fx_reference_date", ""))
                            if resultado["reference_date"] <= data_antiga:
                                raise EcbRateError("a taxa renovada nao e mais recente que a taxa expirada")
                        candidato = copy.deepcopy(self.estado)
                        nova_sessao = candidato.setdefault("operational_session", {})
                        historico_taxas = list(nova_sessao.get("fx_history", []))
                        if nova_sessao.get("fx_usd_per_eur"):
                            historico_taxas.append({
                                "rate_usd_per_eur": str(nova_sessao["fx_usd_per_eur"]),
                                "reference_date": nova_sessao.get("fx_reference_date"),
                                "locked_at_utc": nova_sessao.get("fx_locked_at_utc"),
                                "source": nova_sessao.get("fx_source"),
                                "replaced_at_utc": datetime.now(timezone.utc).isoformat(),
                            })
                        nova_sessao.update({
                            "fx_usd_per_eur": format(nova_taxa, "f"),
                            "fx_reference_date": resultado["reference_date"],
                            "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(),
                            "fx_source": ECB_SOURCE,
                            "fx_generation": int(nova_sessao.get("fx_generation", 0)) + 1,
                            "fx_history": historico_taxas,
                        })
                        guardar_estado(self.cfg, candidato, strict=True)
                        with open(self.cfg["ficheiro_estado"], "r", encoding="utf-8") as source:
                            persistido = json.load(source)
                        sessao_persistida = persistido.get("operational_session", {})
                        taxa_persistida = validate_locked_session_rate(sessao_persistida)
                        if (sessao_persistida.get("id") != sessao.get("id")
                                or taxa_persistida != nova_taxa
                                or sessao_persistida.get("fx_reference_date") != resultado["reference_date"]):
                            raise PaperOperationError("a taxa BCE gravada nao passou a verificacao de leitura")
                        sync_state_in_place(self.estado, persistido)
                        self._fx_rate_persisted = True
                        self._fx_status = "valida"
                    except Exception as persist_error:
                        self._fx_rate_persisted = False
                        self._fx_status = f"persistencia falhou ({type(persist_error).__name__})"
                        log.error("Taxa BCE nao persistida/verificada; compras bloqueadas: %s", persist_error)
                else:
                    self._fx_status = info_atual["status"]
            elif erro:
                self._fx_status = f"indisponivel ({erro})"
                log.warning("Taxa BCE indisponivel; novas compras bloqueadas: %s", erro)

        info = self._taxa_bce_info()
        if info["status"] in ("valida", "invalida", "configuracao invalida"):
            self._fx_status = info["status"]
            return
        if (not self._fx_fetch_active and time.monotonic() >= self._fx_next_retry
                and self.cfg.get("limites_operacionais_ativos") and not self.cfg.get("erro_config_operacional")):
            sessao = self.estado.get("operational_session")
            if info["status"] in ("a obter", "indisponivel", "expirada", "nao persistida"):
                self._fx_fetch_active = True
                self._fx_status = "a renovar" if info["status"] == "expirada" else "a obter"
                threading.Thread(target=self._worker_taxa_bce, daemon=True, name="paper-fx-ecb").start()

    def _inicializar_contadores_scanner(self):
        hoje = datetime.fromtimestamp(agora()).date().isoformat()
        existentes = self.estado.get("scanner_counters")
        if not isinstance(existentes, dict):
            existentes = {"daily": {}}
        daily = existentes.get("daily")
        if not isinstance(daily, dict):
            daily = {}
        base = {name: 0 for name in self.CONTADORES_SCANNER}
        for dia, valor in list(daily.items()):
            if not isinstance(valor, dict):
                daily.pop(dia, None)
                continue
            for name in self.CONTADORES_SCANNER:
                valor[name] = max(0, int(num(valor.get(name))))
            if not isinstance(valor.get("unique_mints"), list):
                valor["unique_mints"] = []
        daily.setdefault(hoje, dict(base, unique_mints=[]))
        cutoff = datetime.fromtimestamp(agora()).date().toordinal() - 30
        for dia in list(daily):
            try:
                if datetime.fromisoformat(dia).date().toordinal() < cutoff:
                    del daily[dia]
            except (TypeError, ValueError):
                del daily[dia]
        session = dict(base, unique_mints=[])
        existentes.update({"daily": daily,
                           "session": session,
                           "session_id": f"{int(agora())}-{uuid.uuid4().hex[:8]}"})
        self.estado["scanner_counters"] = existentes

    def _counter_add(self, name, amount=1):
        if name not in self.CONTADORES_SCANNER:
            return
        counters = self.estado["scanner_counters"]
        hoje = datetime.fromtimestamp(agora()).date().isoformat()
        daily = counters["daily"].setdefault(hoje,
                                             {**{k: 0 for k in self.CONTADORES_SCANNER}, "unique_mints": []})
        counters["session"][name] += amount
        daily[name] += amount

    def _contar_novo_mint(self, mint):
        counters = self.estado["scanner_counters"]
        hoje = datetime.fromtimestamp(agora()).date().isoformat()
        daily = counters["daily"].setdefault(hoje,
                                             {**{k: 0 for k in self.CONTADORES_SCANNER}, "unique_mints": []})
        for bucket in (counters["session"], daily):
            bucket.setdefault("unique_mints", [])
            if mint not in bucket["unique_mints"]:
                bucket["unique_mints"].append(mint)
                bucket["unique_new"] += 1

    def _contar_resultado_analise(self, resultado, reanalisado=False):
        status = resultado.get("status")
        if reanalisado:
            self._counter_add("reanalyzed")
        if status in ("approved", "rejected", "pending_data"):
            self._counter_add("analysis_completed")
        if status == "approved":
            self._counter_add("approved")
        elif status == "rejected":
            self._counter_add("rejected")
        elif status == "pending_data":
            self._counter_add("pending_data")
        elif status == "technical_error":
            self._counter_add("technical_errors")

    def _contadores_scanner_publicos(self):
        hoje = datetime.fromtimestamp(agora()).date().isoformat()
        counters = self.estado.get("scanner_counters", {})
        def limpo(bucket):
            return {name: int(num(bucket.get(name))) for name in self.CONTADORES_SCANNER}
        return {"date": hoje, "session_id": counters.get("session_id"),
                "session": limpo(counters.get("session", {})),
                "daily": limpo(counters.get("daily", {}).get(hoje, {}))}

    def _registar_descoberta(self, mint, par=None):
        par = par if isinstance(par, dict) else {}
        nome = obter(par, "baseToken", "name")
        simbolo = obter(par, "baseToken", "symbol")
        item = {"id": mint, "mint": mint, "status": "discovered",
                "classification": "discovered", "estado_analise": "descoberta",
                "aprovado": None, "pontuacao": None, "score_calculated": False,
                "simbolo": simbolo if isinstance(simbolo, str) else "",
                "nome": nome if isinstance(nome, str) else "",
                "preco": Analista._numero_presente(par.get("priceUsd"))[0],
                "liq": Analista._numero_presente(obter(par, "liquidity", "usd"))[0],
                "hora": hora_txt(), "ts": agora(),
                "resultado_analise": "Nova descoberta; análise ainda não concluída."}
        self.descobertas = [x for x in self.descobertas if x.get("mint") != mint]
        self.descobertas.insert(0, item)
        del self.descobertas[40:]

    def _registar_sem_par(self, mint):
        self._counter_add("no_pair")
        self._registar_descoberta(mint)
        item = self.descobertas[0]
        item.update(status="no_pair", classification="no_pair", estado_analise="sem_par",
                    resultado_analise="Não foi encontrado um par utilizável.",
                    missing_fields=["pair"])
        self.registar_analise(mint, {}, {"status": "no_pair", "classification": "no_pair",
            "estado_analise": "sem_par", "aprovado": None, "pontuacao": None,
            "score_calculated": False, "motivo": item["resultado_analise"],
            "resultado_analise": item["resultado_analise"], "reavaliar": True,
            "missing_fields": ["pair"], "invalid_fields": [], "criteria": Analista._criterios_indisponiveis("Par inexistente."),
            "blocking_criterion": {"id": "pair", "name": "Par", "observed": None,
                                    "threshold": "par utilizável", "status": "pending_data"}})

    def registar_market_snapshots(self, pares, mints):
        """Pass already-fetched pair data to the asynchronous observational store."""
        # Demo prices and pairs are invented; never store them as market data.
        if self.modo == "demo" or self.market_snapshots is None:
            return 0
        try:
            return self.market_snapshots.record_pairs(pares, mints)
        except Exception:
            # Market history is optional and must not interfere with bot cycles.
            log.exception("Erro ao enfileirar snapshots de mercado")
            return 0

    # ---------- plataforma web ----------
    def registar_sinal(self, mint, par, analise, comprado, proposal_id=None):
        """Guarda um sinal (token aprovado pelo Analista) para a plataforma web."""
        e = self.estado
        e["sinais"].append({
            "id": f"{int(agora() * 1000)}-{mint[:8]}", "ts": agora(), "hora": hora_txt(),
            "simbolo": simbolo_limpo(par),
            "mint": mint if MINT_VALIDO.fullmatch(mint) else "",
            "pontuacao": int(analise["pontuacao"]),
            "preco": num(par.get("priceUsd")), "liq": num(obter(par, "liquidity", "usd")),
            "mcap": num(par.get("marketCap")) or num(par.get("fdv")),
            "vol_h1": num(obter(par, "volume", "h1")), "var_h1": num(obter(par, "priceChange", "h1")),
            "img": imagem_do_par(par), "spark": faisca_do_par(par),
            "proposal_id": proposal_id,
            "estado": "comprado" if comprado else "aguarda_confirmacao" if proposal_id else "sem_vaga",
            "resultado": None, "saida": "",
        })
        del e["sinais"][:-self.cfg["max_sinais"]]
        e["sinais_ts"] = [t for t in e["sinais_ts"] if agora() - num(t) < 86400][-5000:] + [int(agora())]

    def _recentes_com_observacao(self):
        obs = getattr(self, "obs_extra", None)
        if obs is None:
            return self.recentes
        dados = obs.todos()
        if not dados:
            return self.recentes
        return [dict(r, observacao_extra=self._observacao_para_o_painel(dados[r["mint"]]))
                if isinstance(r, dict) and r.get("mint") in dados else r
                for r in self.recentes]

    @staticmethod
    def _observacao_para_o_painel(obs):
        """Copia da observacao sem a lista de carteiras seguidas (privada) nem os donos das contas (volumoso)."""
        limpa = dict(obs)
        if isinstance(limpa.get("carteiras"), dict):
            limpa["carteiras"] = {k: v for k, v in limpa["carteiras"].items() if k != "quem"}
        if isinstance(limpa.get("holders"), dict):
            limpa["holders"] = {k: v for k, v in limpa["holders"].items() if k != "top"}
        return limpa

    def registar_analise(self, mint, par, analise):
        """Guarda os ultimos tokens analisados (aprovados ou nao) para o radar e as listas."""
        if not MINT_VALIDO.fullmatch(mint):
            return
        status = analise.get("status")
        if status not in ("approved", "rejected", "pending_data", "technical_error", "no_pair"):
            status = "approved" if analise.get("aprovado") is True else "rejected" if analise.get("aprovado") is False else "pending_data"
        nome = obter(par, "baseToken", "name")
        simbolo = obter(par, "baseToken", "symbol")
        if not isinstance(nome, str):
            nome = ""
        if not isinstance(simbolo, str) or not simbolo.strip():
            simbolo = simbolo_limpo(par)
        motivo = analise.get("motivo") if isinstance(analise.get("motivo"), str) else "Indisponível."
        legacy = {"approved": "aprovada", "rejected": "rejeitada", "pending_data": "dados_indisponiveis",
                  "technical_error": "erro_tecnico", "no_pair": "sem_par"}
        dados = {
            "simbolo": simbolo, "nome": nome.strip()[:100],
            "preco": Analista._numero_presente(par.get("priceUsd"))[0],
            "estado_analise": legacy[status], "status": status, "classification": status,
            "resultado_analise": motivo[:500], "pontuacao": analise.get("pontuacao"),
            "score_calculated": bool(analise.get("score_calculated", analise.get("pontuacao") is not None)),
            "aprovado": analise.get("aprovado"), "nota": motivo,
            "liq": Analista._numero_presente(obter(par, "liquidity", "usd"))[0],
            "var_h1": Analista._numero_presente(obter(par, "priceChange", "h1"))[0],
            "img": imagem_do_par(par), "spark": faisca_do_par(par),
            "blocking_criterion": analise.get("blocking_criterion"),
            "criteria": analise.get("criteria", []), "missing_fields": analise.get("missing_fields", []),
            "invalid_fields": analise.get("invalid_fields", []),
            "technical_error": analise.get("technical_error"), "partial_score": analise.get("partial_score"),
            "fonte": "geckoterminal" if mint in getattr(getattr(self, "scanner", None), "origem_gecko", ()) else "dexscreener",
        }
        for r in self.recentes:
            if r.get("mint") == mint:
                r.update(dados)
                return
        self.recentes.insert(0, dict(dados, id=mint, mint=mint, ts=agora(), hora=hora_txt()))
        del self.recentes[40:]
        return
        nota = analise["motivo"]
        nota = nota[11:70] if nota.startswith("REJEITADO: ") else ("aprovado" if analise["aprovado"] else "pontuação insuficiente")
        nome = obter(par, "baseToken", "name")
        if not isinstance(nome, str) or not nome.strip():
            nome = ""
        motivo = analise.get("motivo") if isinstance(analise.get("motivo"), str) else ""
        estado_analise = "aprovada" if analise.get("aprovado") else "rejeitada"
        dados = {
            "simbolo": simbolo_limpo(par),
            "nome": nome.strip()[:100], "preco": num(par.get("priceUsd")),
            "estado_analise": estado_analise, "resultado_analise": motivo[:260] or nota,
            "pontuacao": int(analise["pontuacao"]), "aprovado": bool(analise["aprovado"]), "nota": nota,
            "liq": num(obter(par, "liquidity", "usd")), "var_h1": num(obter(par, "priceChange", "h1")),
            "img": imagem_do_par(par), "spark": faisca_do_par(par),
        }
        for r in self.recentes:
            if r["mint"] == mint:  # reanalise: atualiza sem mudar a ordem nem a hora
                r.update(dados)
                return
        self.recentes.insert(0, dict(dados, id=mint, mint=mint, ts=agora(), hora=hora_txt()))
        del self.recentes[40:]

    def fechar_sinal(self, mint, pnl_pct, motivo):
        """Quando a posicao fecha, o sinal passa a mostrar o resultado."""
        return _fechar_sinal_no_estado(self.estado, mint, pnl_pct, motivo)

    def registar_historico(self):
        """Guarda o valor total ao longo do tempo. Quando enche, fica com metade dos pontos
        e passa a guardar mais espacado, para o grafico cobrir sempre a simulacao inteira."""
        e = self.estado
        h = e["historico"]
        passo = max(30, int(num(e.get("hist_passo"), 30)))
        if not h or agora() - num(h[-1][0]) >= passo:
            h.append([int(agora()), round(self.valor_total(), 4)])
            if len(h) > 400:
                del h[1::2]
                e["hist_passo"] = passo * 2

    def escaneadas_hoje(self):
        hoje = datetime.fromtimestamp(agora()).date().isoformat()
        return int(num(self.estado.get("scanner_counters", {}).get("daily", {}).get(hoje, {}).get("unique_new")))

    def atualizar_snapshot(self):
        """Prepara o JSON que a pagina web le (feito aqui para o servidor nunca tocar no estado)."""
        cfg, e = self.cfg, self.estado
        st = e["stats"]
        total = self.valor_total()
        inicial = e["saldo_inicial"] or 1.0
        trades = ler_trades_paper(cfg)
        try:
            valor_pre_proposta, bloqueio_pre_proposta = self.executor.pode_comprar()
            pre_proposta_status = ("Elegível para proposta; aprovação manual obrigatória"
                                   if valor_pre_proposta > 0 else bloqueio_pre_proposta)
        except Exception as readiness_error:
            valor_pre_proposta = 0.0
            pre_proposta_status = f"Indisponível ({type(readiness_error).__name__})"
        posicoes = []
        for pos in e["posicoes"].values():
            pe = pos["preco_entrada"] or 1.0
            receita, _, _ = simular_venda(cfg, pos, num(pos.get("preco_atual")), num(pos.get("liq_atual")))
            posicoes.append({
                "simbolo": pos["simbolo"], "mint": pos["mint"], "custo": pos["custo"],
                "preco_entrada": pos.get("preco_entrada"), "preco_efetivo": pos.get("preco_efetivo"),
                "preco_atual": pos.get("preco_atual"), "tokens": pos.get("tokens"),
                "aberta_em": pos.get("aberta_em"), "estado": "aberta",
                "var": (num(pos.get("preco_atual")) / pe - 1) * 100,
                "liquido": (receita / pos["custo"] - 1) * 100 if pos["custo"] else 0.0,
                "min": (agora() - pos["aberta_em"]) / 60, "trailing": bool(pos.get("trailing")),
                "hist": [num(v) for v in pos.get("hist", [])][-600:],
                "hist_t": [num(v) for v in pos.get("hist_t", [])][-600:]
                if len(pos.get("hist_t") or []) == len(pos.get("hist") or []) else [],
                "fonte_preco": pos.get("fonte_preco") or "dexscreener",
                # Grafico "Posicao ao vivo" (so leitura): par, pico, liquidez e regras de saida do Risco.
                "par": str(pos.get("par") or ""), "pico": num(pos.get("pico")), "liq_atual": num(pos.get("liq_atual")),
                "valor_atual_usd": receita, "pnl_usd": receita - num(pos.get("custo")),
                "intervalo_seg": (cfg.get("intervalo_preco_rapido_seg", 3) if pos.get("fonte_preco") == "jupiter"
                                  else cfg.get("intervalo_risco_com_posicao_seg", cfg["intervalo_risco_seg"])),
                "regras_saida": {k: cfg.get(k) for k in ("stop_loss_pct", "take_profit_pct", "trailing_ativar_pct",
                                                          "trailing_distancia_pct", "tempo_maximo_min")},
            })
        dados = {
            "modo": self.modo, "hora": hora_txt(), "agora": agora(),
            "limite_atualizacao_seg": max(30, 3 * min(cfg["intervalo_risco_seg"], cfg["intervalo_scanner_seg"])),
            "saldo": e["saldo"], "total": total,
            "analisados": self.analisados, "hoje": self.escaneadas_hoje(), "recentes": self._recentes_com_observacao(),
            "descobertas": self.descobertas,
            "scanner_counts": self._contadores_scanner_publicos(),
            "pendentes_scanner": [
                {"id": mint, "mint": mint, **self.estado.get("scanner_pending", {}).get(mint,
                    {"status": "pending_data", "reason": "Aguardando reavaliação; motivo anterior indisponível.",
                     "missing_fields": [], "blocking_criterion": None})}
                for mint in self.estado.get("pendentes", {})
            ],
            "em_analise": self.em_analise,
            "trades": trades if trades is not None else [], "trades_disponiveis": trades is not None,
            "config": [
                ["Modo", "Demo (mercado inventado)" if self.modo == "demo" else "Simulação (paper trading)"],
                ["Saldo inicial", f"${cfg['saldo_inicial_usd']:.2f}"],
                ["Pontuação mínima para sinal", f"{cfg['pontuacao_minima']}/100"],
                ["Tamanho de cada posição", f"{cfg['tamanho_posicao_pct']}% do capital"],
                ["Máximo de posições abertas", str(cfg["max_posicoes_abertas"])],
                ["Estado das pré-condições de compra", pre_proposta_status],
                ["Exposição máxima", f"{cfg['exposicao_maxima_pct']}%"],
                ["Stop loss", f"{cfg['stop_loss_pct']}%"],
                ["Take profit", f"+{cfg['take_profit_pct']}%" if cfg["take_profit_pct"] else "desligado"],
                ["Trailing stop", f"liga aos +{cfg['trailing_ativar_pct']}%, distância {cfg['trailing_distancia_pct']}%"
                 if cfg["trailing_ativar_pct"] else "desligado"],
                ["Tempo máximo por posição", f"{cfg['tempo_maximo_min']} min"],
                ["Liquidez mínima", f"${cfg['liquidez_minima_usd']:,}"],
                ["Idade do par", f"{cfg['idade_minima_min']} min a {cfg['idade_maxima_horas']} h"],
                ["Custos simulados", f"{cfg['slippage_pct']}% slippage + {cfg['taxa_pct']}% taxa, à entrada e à saída"],
                ["Percursos gravados (laboratório)", f"{e['n_percursos']} completos, {len(e['percursos'])} a decorrer"],
                ["Procura de tokens / verificação de preços", f"{cfg['intervalo_scanner_seg']} s / {cfg['intervalo_risco_seg']} s"],
            ],
            "sinais_24h": sum(1 for t in e["sinais_ts"] if agora() - num(t) < 86400),
            "pnl": total - inicial, "pnl_pct": (total / inicial - 1) * 100,
            "stats": {"fechados": st["fechados"], "ganhos": st["ganhos"], "perdas": st["perdas"],
                      "acerto": st["ganhos"] / st["fechados"] * 100 if st["fechados"] else 0.0,
                      "realizado": st["pnl_total"]},
            "agentes": [
                {"nome": "Scanner", "detalhe": self.ultimo_scan},
                {"nome": "Analista", "detalhe": self.ultima_analise},
                {"nome": "Executor", "detalhe": f"{len(e['posicoes'])}/{cfg['max_posicoes_abertas']} posições · "
                                                f"{cfg['tamanho_posicao_pct']}% por compra"},
                {"nome": "Risco", "detalhe": f"stop {cfg['stop_loss_pct']:.0f}% · " + (f"alvo +{cfg['take_profit_pct']:.0f}%" if cfg["take_profit_pct"] else "sem alvo fixo")},
            ],
            "historico": [inicial] + [num(v[1]) for v in e["historico"] if isinstance(v, list) and len(v) == 2] + [total],
            "posicoes": posicoes,
            "sinais": list(reversed(e["sinais"][-cfg["max_sinais"]:])),
            "acoes": list(reversed(e["acoes"][-12:])),
            "propostas_paper": self.manual_approvals.public_pending(),
            "rpc_observacao": getattr(getattr(self, "obs_extra", None), "origem_rpc", "publico")
                              if getattr(getattr(self, "obs_extra", None), "ativo", False) else "inativa",
            "paper_purchase_readiness": {
                "eligible_for_proposal": valor_pre_proposta > 0,
                "status": pre_proposta_status,
                "manual_confirmation_required": True,
                "max_open_positions": MAX_OPEN_POSITIONS,
                "max_eur_per_operation": (_decimal_canonico(cfg["max_eur_per_operation"])
                                          if cfg.get("max_eur_per_operation") is not None
                                          else "Indisponível"),
            },
            "emergency_stop": {
                "enabled": cfg.get("emergency_stop_enabled") is True,
                "active": self._emergency_stop_is_active(),
                "stop_id": self.estado.get("emergency_stop", {}).get("stop_id"),
                "changed_at_utc": self.estado.get("emergency_stop", {}).get("changed_at_utc"),
                "reason": self.estado.get("emergency_stop", {}).get("reason"),
                "terminal_control_only": True,
            },
            "minimo": cfg["pontuacao_minima"], "max_pos": cfg["max_posicoes_abertas"],
        }
        if cfg.get("limites_operacionais_ativos"):
            fx = self._taxa_bce_info()
            limite_eur = cfg.get("max_eur_per_operation")
            limite_txt = (f"máx. €{limite_eur:.2f}" if limite_eur is not None else "compras bloqueadas")
            if fx["status"] == "valida":
                limite_txt += f" · até ${fx['cap_usd']:.4f} · BCE {fx['reference_date']}"
            else:
                limite_txt += f" · compras bloqueadas ({fx['status']})"
            dados["config"].extend([
                ["Limite nominal por operação", limite_txt],
                ["Taxa BCE USD/EUR", (f"{fx['rate']} · {fx['reference_date']}" if fx["rate"]
                                      else fx["status"])],
            ])
            dados["limites_operacionais"] = {
                "estado_configuracao": "invalida" if cfg.get("erro_config_operacional") else "valida",
                "erro_configuracao": cfg.get("erro_config_operacional") or None,
                "max_eur_per_operation": (_decimal_canonico(cfg["max_eur_per_operation"])
                                          if cfg.get("max_eur_per_operation") is not None else None),
                "max_open_positions": cfg["max_posicoes_abertas"],
                "fx_status": fx["status"], "fx_usd_per_eur": fx["rate"],
                "fx_reference_date": fx["reference_date"], "limit_usd": fx["cap_usd"],
                "session_id": e.get("operational_session", {}).get("id"),
            }
        self.snapshot = json.dumps(dados, ensure_ascii=False)
        return total

    # ---------- utilitarios ----------
    def acao(self, texto):
        linha = f"{hora_txt()}  {texto}"
        log.info(texto)
        self.estado["acoes"].append(linha)
        del self.estado["acoes"][:-30]

    def _registar_auditoria_aprovacao(self, event, proposal, reason=None):
        """Persist a secret-free lifecycle event, then mirror it to log/Dashboard actions."""
        try:
            proposal_id = proposal.proposal_id if isinstance(proposal, PaperProposal) else proposal.get("proposal_id")
            fields = proposal.public_fields() if isinstance(proposal, PaperProposal) else proposal
            event_names = {
                "proposed": "proposal_created",
                "approved": "manual_confirmation_accepted",
                "validation_passed": "execution_validation_passed",
                "executed": "execution_committed",
                "execution_blocked": "execution_blocked",
                "invalidated": "execution_blocked",
                "expired": "proposal_expired",
                "rejected": "proposal_rejected",
            }
            journal_event = event_names.get(event)
            if journal_event is None or self.paper_audit.blocked:
                return False
            record = {
                "proposal_id": proposal_id,
                "mint": fields.get("mint"),
                "pair_address": fields.get("pair_address"),
                "amount_usd": fields.get("debit_total_usd", fields.get("amount_usd")),
                "price_usd": fields.get("reference_price_usd", fields.get("price_usd")),
                "reason": sanitize_audit_text(reason) if reason else None,
            }
            if event == "executed":
                record["operation_id"] = getattr(self, "_last_paper_operation_id", None)
                record["source"] = "Executor"
            elif event == "validation_passed":
                record["source"] = "Executor"
            self.paper_audit.append(journal_event, record)
            serialized = json.dumps({"event": journal_event, **record}, ensure_ascii=True,
                                    sort_keys=True, separators=(",", ":"))
            log.info("PAPER_APPROVAL_AUDIT %s", serialized)
            self.estado["acoes"].append(f"{hora_txt()}  PAPER_APPROVAL_AUDIT {serialized}")
            del self.estado["acoes"][:-30]
            return True
        except Exception:
            log.exception("Falha ao registar auditoria da proposta Paper")
            return False

    def guardar(self):
        self.estado["ultimo_ciclo"] = agora()
        try:
            self.paper_operations.recover(self.estado)
        except PaperOperationError:
            log.exception("Estado Paper com operacao pendente/ambigua; gravacao normal adiada")
            return False
        guardar_estado(self.cfg, self.estado)
        return True

    def retomar(self):
        """Ao arrancar depois de uma pausa longa (PC desligado), anula o que ficou a meio.
        Fechar essas posicoes com o preco de horas depois daria resultados que o bot nunca teria tido."""
        e = self.estado
        ultimo = num(e.get("ultimo_ciclo"))
        if not ultimo:
            return 0
        pausa_min = (agora() - ultimo) / 60.0
        if pausa_min <= self.cfg["pausa_maxima_min"]:
            return 0
        anuladas = 0
        for mint in list(e["posicoes"]):
            if self.executor.anular(mint, f"bot parado {pausa_min:.0f} min"):
                anuladas += 1
        n_perc = len(e["percursos"])
        e["percursos"] = {}
        e["pendentes"] = {}
        if anuladas or n_perc:
            self.acao(f"Bot esteve parado {pausa_min:.0f} min: {anuladas} posicoes anuladas (dinheiro devolvido), "
                      f"{n_perc} percursos descartados")
        return anuladas

    def valor_total(self):
        """Saldo + valor liquido estimado das posicoes se fossem vendidas agora."""
        total = self.estado["saldo"]
        for pos in self.estado["posicoes"].values():
            receita, _, _ = simular_venda(self.cfg, pos, num(pos.get("preco_atual")), num(pos.get("liq_atual")))
            total += receita
        return total

    def _marcar_visto(self, mint):
        e = self.estado
        e["pendentes"].pop(mint, None)
        e.setdefault("scanner_pending", {}).pop(mint, None)
        e["vistos"][mint] = int(agora())
        if len(e["vistos"]) > self.cfg["max_vistos"]:
            for antigo in list(e["vistos"].keys())[:len(e["vistos"]) - self.cfg["max_vistos"]]:
                del e["vistos"][antigo]

    def _adiar(self, mint, resultado=None):
        """Poe o token em lista de espera para reanalise (durante um tempo limitado)."""
        e = self.estado
        primeiro = e["pendentes"].get(mint)
        if primeiro is None:
            if len(e["pendentes"]) >= self.cfg["max_pendentes"]:
                self._marcar_visto(mint)
                return
            e["pendentes"][mint] = int(agora())
        elif agora() - num(primeiro) > self.cfg["janela_reanalise_min"] * 60:
            self._marcar_visto(mint)
            return
        if resultado is not None:
            status = resultado.get("status", "pending_data") if isinstance(resultado, dict) else "pending_data"
            e.setdefault("scanner_pending", {})[mint] = {
                "status": status, "reason": resultado.get("motivo", "Aguardando reavaliação."),
                "missing_fields": resultado.get("missing_fields", []),
                "blocking_criterion": resultado.get("blocking_criterion"),
                "updated_at": agora(),
            }

    # ---------- ciclos ----------
    def ciclo_scanner(self):
        """Scanner -> Analista -> Executor."""
        e = self.estado
        observar = [r.get("mint") for r in self.recentes
                    if isinstance(r, dict) and r.get("mint")
                    and r.get("mint") not in e["posicoes"]
                    and r.get("mint") not in e["percursos"]]
        candidatos, sem_par = self.scanner.procurar(e["vistos"], list(e["pendentes"].keys()),
                                                    ignorar=e["posicoes"], observar=observar)
        self._guardar_cotacoes_recentes(getattr(self.scanner, "ultimos_pares", {}))
        self._counter_add("tokens_received", int(num(getattr(self.scanner, "last_received_count", 0))))
        for mint in getattr(self.scanner, "last_new_mints", []):
            self._contar_novo_mint(mint)
            self._registar_descoberta(mint, melhor_par(self.scanner.ultimos_pares.get(mint) or []))
        self.registar_market_snapshots(self.scanner.ultimos_pares, self.scanner.ultimos_mints)
        if candidatos is None:
            for mint in dict.fromkeys(getattr(self.scanner, "last_new_mints", [])
                                      + getattr(self.scanner, "last_reanalysis_mints", [])):
                erro = {"status": "technical_error", "motivo": "Falha técnica ao consultar os pares; análise não realizada.",
                        "pontuacao": None, "aprovado": None, "score_calculated": False,
                        "reavaliar": True, "technical_error": {"type": "pair_api_unavailable"},
                        "criteria": Analista._criterios_indisponiveis("Consulta de pares falhou."),
                        "missing_fields": [], "invalid_fields": []}
                self._contar_resultado_analise(erro, mint in getattr(self.scanner, "last_reanalysis_mints", []))
                self.registar_analise(mint, {}, erro)
                if mint in e["pendentes"]:
                    self._adiar(mint, erro)
            self.ultimo_scan = f"{hora_txt()} - API sem resposta"
            self.atualizar_snapshot()
            return 0
        for mint in sem_par:
            self._registar_sem_par(mint)
            if mint in getattr(self.scanner, "last_reanalysis_mints", []):
                self._counter_add("reanalyzed")
            self._adiar(mint, {"status": "no_pair", "motivo": "Não foi encontrado um par utilizável.",
                               "missing_fields": ["pair"], "blocking_criterion": {"id": "pair"}})
        for mint in dict.fromkeys(getattr(self.scanner, "last_pair_errors", [])):
            erro = {"status": "technical_error", "motivo": "Falha técnica ao consultar os pares; análise não realizada.",
                    "pontuacao": None, "aprovado": None, "score_calculated": False,
                    "reavaliar": True, "technical_error": {"type": "pair_api_unavailable"},
                    "criteria": Analista._criterios_indisponiveis("Consulta de pares falhou."),
                    "missing_fields": [], "invalid_fields": []}
            self._contar_resultado_analise(erro, mint in getattr(self.scanner, "last_reanalysis_mints", []))
            self.registar_analise(mint, {}, erro)
            if mint in e["pendentes"]:
                self._adiar(mint, erro)
        aprovados = []
        for mint, par in candidatos:
            self._vigiar_posicao_no_scanner()  # com posicao aberta, o stop/trailing nao espera pelo scanner
            nome = obter(par, "baseToken", "name")
            self.em_analise = [{
                "id": mint, "mint": mint, "simbolo": simbolo_limpo(par),
                "nome": nome.strip()[:100] if isinstance(nome, str) else "",
                "preco": Analista._numero_presente(par.get("priceUsd"))[0], "estado_analise": "em_analise",
                "status": "analysis_in_progress", "classification": "analysis_in_progress",
                "pontuacao": None, "aprovado": None,
                "score_calculated": False,
                "img": imagem_do_par(par),
                "resultado_analise": "Análise em curso.",
            }] if MINT_VALIDO.fullmatch(mint) else []
            self.atualizar_snapshot()
            try:
                analise = self.analista.analisar(mint, par)
            finally:
                self.em_analise = []
            if mint not in self.tokens_sessao:
                self.tokens_sessao.add(mint)
                hora = str(int(agora() // 3600))
                ph = e["por_hora"]
                ph[hora] = int(num(ph.get(hora))) + 1
                for antiga in [h for h in ph if num(h) < agora() // 3600 - 48]:
                    del ph[antiga]
            self.analisados = len(self.tokens_sessao)
            self._contar_resultado_analise(analise, mint in getattr(self.scanner, "last_reanalysis_mints", []))
            simbolo = simbolo_limpo(par)
            motivo_base = re.sub(r"[-+]?\d[\d.,]*", "#", str(analise.get("motivo")))
            if self._ultimo_motivo_log.get(mint) != motivo_base:  # so regista quando o motivo muda
                log.info("ANALISE %s (%s): %s", simbolo, mint, analise["motivo"])
                self._ultimo_motivo_log[mint] = motivo_base
                while len(self._ultimo_motivo_log) > 5000:
                    self._ultimo_motivo_log.pop(next(iter(self._ultimo_motivo_log)))
            else:
                log.debug("ANALISE %s (%s): %s", simbolo, mint, analise["motivo"])
            self.registar_analise(mint, par, analise)
            self.atualizar_snapshot()
            self.ultima_analise = (f"{simbolo}: {analise['pontuacao']}/100" if analise.get("score_calculated")
                                   else f"{simbolo}: {analise['motivo'][:60]}")
            if analise.get("status") == "approved" or analise.get("aprovado") is True:
                aprovados.append((analise["pontuacao"], mint, par, analise))
                self._marcar_visto(mint)
            elif analise.get("status") in ("pending_data", "technical_error", "no_pair") or analise.get("reavaliar"):
                self._adiar(mint, analise)
            else:
                self._marcar_visto(mint)
                if (analise.get("score_calculated") and analise.get("pontuacao") is not None
                        and num(analise.get("pontuacao")) >= num(self.cfg.get("percursos_controlo_pontuacao_min"), 50)):
                    try:  # grupo de controlo do laboratorio: recusado, mas seguido como se fosse sinal
                        self.iniciar_percurso(mint, par, analise, grupo="controlo")
                    except Exception:
                        log.exception("Percurso de controlo ignorado para %s", mint)
        # As compras aprovadas ficam pendentes de confirmação explícita no terminal.
        propostas = 0
        compras_confirmadas = 0
        for _, mint, par, analise in sorted(aprovados, key=lambda x: -x[0]):
            valor, motivo = self.executor.pode_comprar()
            self._pedir_observacao_extra(mint, par, valor)
            if mint in getattr(self.scanner, "origem_gecko", ()):
                # Fonte GeckoTerminal em modo observacao: regista e grava o percurso, mas nunca propoe compra.
                self.acao(f"{simbolo_limpo(par)}: aprovado ({analise['pontuacao']}) [GeckoTerminal - observacao] - sem proposta")
                self.registar_sinal(mint, par, analise, False)
                self.iniciar_percurso(mint, par, analise)
                continue
            if valor <= 0:
                self.acao(f"{simbolo_limpo(par)}: aprovado ({analise['pontuacao']}) mas sem proposta - {motivo}")
                self._registar_auditoria_aprovacao("execution_blocked", {
                    "mint": mint, "pair_address": str(par.get("pairAddress") or ""),
                    "amount_usd": None, "price_usd": par.get("priceUsd"),
                }, motivo)
                self.registar_sinal(mint, par, analise, False)
                self.iniciar_percurso(mint, par, analise)
                continue
            bloqueio_rota = self._bloqueio_rota_compra(mint, valor)
            if bloqueio_rota:
                self.acao(f"{simbolo_limpo(par)}: aprovado mas sem proposta - {bloqueio_rota}")
                self._registar_auditoria_aprovacao("execution_blocked", {
                    "mint": mint, "pair_address": str(par.get("pairAddress") or ""),
                    "amount_usd": valor, "price_usd": par.get("priceUsd"),
                }, bloqueio_rota)
                self.registar_sinal(mint, par, analise, False)
                self.iniciar_percurso(mint, par, analise)
                continue
            try:
                execution_plan = self.executor.preparar_execucao(mint, par, valor)
            except (TypeError, ValueError, InvalidOperation) as exc:
                self.acao(f"{simbolo_limpo(par)}: sem proposta - plano financeiro indisponível")
                self._registar_auditoria_aprovacao("execution_blocked", {
                    "mint": mint, "pair_address": str(par.get("pairAddress") or ""),
                    "amount_usd": valor, "price_usd": par.get("priceUsd"),
                }, f"plano financeiro inválido ({type(exc).__name__})")
                self.registar_sinal(mint, par, analise, False)
                self.iniciar_percurso(mint, par, analise)
                continue
            plan_block = self.executor.bloqueio_plano_financeiro(execution_plan)
            if plan_block:
                self.acao(f"{simbolo_limpo(par)}: aprovado mas proposta bloqueada - {plan_block}")
                self._registar_auditoria_aprovacao("execution_blocked", {
                    "mint": mint, "pair_address": str(par.get("pairAddress") or ""),
                    "amount_usd": valor, "price_usd": par.get("priceUsd"),
                }, plan_block)
                self.registar_sinal(mint, par, analise, False)
                self.iniciar_percurso(mint, par, analise)
                continue
            if isinstance(self.manual_approvals, ManualApprovalManager):
                proposta = self.manual_approvals.propose(
                    mint, par, analise, valor, execution_plan)
            else:
                proposta = self.manual_approvals.propose(mint, par, analise, valor)
            propostas += 1
            log.info("PROPOSTA PAPER %s %s valor=$%.4f score=%s expira em 90s",
                     proposta["proposal_id"], proposta["symbol"], proposta["amount_usd"], proposta["score"])
            try:
                tgd = getattr(self, "tg_decisoes", None)
                pid = proposta["proposal_id"]
                if tgd is not None and tgd.ativo and isinstance(pid, str):
                    self.telegram.enviar(texto_proposta(self.cfg, proposta["symbol"], mint, proposta["score"],
                                                        proposta["amount_usd"], par, com_botoes=True),
                                         botoes=tgd.botoes_proposta(pid),
                                         ao_enviar=lambda mid, texto, pid=pid: tgd.registar_mensagem(pid, mid, texto))
                else:
                    self.telegram.enviar(texto_proposta(self.cfg, proposta["symbol"], mint, proposta["score"],
                                                        proposta["amount_usd"], par))
            except Exception:
                log.warning("Aviso de proposta por Telegram falhou")
            if not self._registar_auditoria_aprovacao("proposed", proposta):
                cancel = getattr(self.manual_approvals, "cancel", None)
                if callable(cancel):
                    cancel(proposta["proposal_id"], "diário de auditoria indisponível")
                self.acao("Proposta cancelada: diário de auditoria indisponível")
                continue
            self.registar_sinal(mint, par, analise, False, proposta["proposal_id"])
            self.iniciar_percurso(mint, par, analise)
            _, compras = self._processar_respostas_aprovacao()
            compras_confirmadas += compras
        self.atualizar_snapshot()
        self.ultimo_scan = (f"{hora_txt()} - {len(candidatos)} analisados, {len(aprovados)} aprovados, "
                            f"{propostas} propostas manuais, 0 compras automaticas, {len(e['pendentes'])} em espera"
                            + ("" if self.scanner.respondeu else " (lista de tokens novos sem resposta)"))
        _, compras = self._processar_respostas_aprovacao()
        compras_confirmadas += compras
        return compras_confirmadas

    def _processar_respostas_aprovacao(self):
        """Executa respostas no thread principal, mantendo o leitor de terminal fora do Risk."""
        alterou = False
        compras = 0
        def descartar(token):
            descarte = getattr(self.manual_approvals, "discard", None)
            if callable(descarte):
                descarte(token)
        def resolver_sinal(proposal_id, state, motivo=None):
            for item in reversed(self.estado.get("sinais", [])):
                if item.get("proposal_id") == proposal_id:
                    item["estado"] = state
                    break
            tgd = getattr(self, "tg_decisoes", None)
            if tgd is not None:
                try:
                    tgd.fechar(proposal_id, "✅ Compra Paper feita." if state == "comprado" else
                               f"❌ Sem compra: {motivo}." if motivo else
                               "⚠️ Aprovada, mas sem compra: o preço ou as condições mudaram (vê o dashboard).")
                except Exception:
                    log.warning("Atualizar a mensagem da proposta no Telegram falhou")
        for proposta, token, aceite, motivo in self.manual_approvals.drain_results():
            alterou = True
            if not aceite:
                resolver_sinal(proposta["proposal_id"], "sem_vaga", motivo)
                if token:
                    descartar(token)
                self._registar_auditoria_aprovacao(
                    "expired" if motivo == "proposta expirada" else "rejected", proposta, motivo)
                self.acao(f"Proposta PAPER {proposta['proposal_id'][:12]} recusada - {motivo}")
                continue
            if not self._registar_auditoria_aprovacao(
                    "approved", proposta, "confirmação manual aceite pelo gestor"
                    + (" (dashboard)" if motivo == "aprovada no dashboard" else
                       " (Telegram)" if motivo == "aprovada no Telegram" else "")):
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self.acao("Compra bloqueada: não foi possível persistir a confirmação manual")
                continue
            if time.monotonic() >= proposta["expires_monotonic"]:
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao("expired", proposta, "expirou antes da execucao")
                self.acao(f"Proposta PAPER {proposta['proposal_id'][:12]} expirada; compra bloqueada")
                continue
            valor, bloqueio = self.executor.pode_comprar()
            par = proposta["pair"]
            valor_proposto = (proposta.amount_usd if isinstance(proposta, PaperProposal)
                              else proposta["amount_usd"])
            if valor <= 0 or _decimal_canonico(valor) != _decimal_canonico(valor_proposto):
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao("invalidated", proposta,
                                                    bloqueio or "valor da ordem mudou")
                self.acao(f"Proposta PAPER {proposta['proposal_id'][:12]} invalidada - "
                          f"{bloqueio or 'valor da ordem mudou'}")
                continue
            cotacao, motivo_cotacao = self._cotacao_atual_para_proposta(proposta)
            if cotacao is None:
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao("invalidated", proposta, motivo_cotacao)
                self.acao(f"Proposta PAPER {proposta['proposal_id'][:12]} invalidada - {motivo_cotacao}")
                continue
            try:
                plano_atual = self.executor.preparar_execucao(proposta["mint"], cotacao, valor)
            except (TypeError, ValueError, InvalidOperation):
                plano_atual = None
            execution_json = getattr(proposta, "execution_json", None)
            if execution_json is None:
                # Test doubles predating immutable proposals derive a baseline plan.
                try:
                    execution_json = self.executor.preparar_execucao(
                        proposta["mint"], par, proposta["amount_usd"]).canonical_json
                except (TypeError, ValueError, InvalidOperation):
                    execution_json = None
            if plano_atual is None or plano_atual.canonical_json != execution_json:
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao(
                    "invalidated", proposta, "preco, liquidez ou condicoes de execucao mudaram")
                self.acao(f"Proposta PAPER {proposta['proposal_id'][:12]} invalidada - "
                          "preco ou condicoes de execucao mudaram; exige nova analise e aprovacao")
                continue
            bloqueio_plano = self.executor.bloqueio_plano_financeiro(plano_atual)
            if bloqueio_plano:
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao("execution_blocked", proposta, bloqueio_plano)
                self.acao(f"Proposta bloqueada - {bloqueio_plano}")
                continue
            if not self._registar_auditoria_aprovacao(
                    "validation_passed", proposta, "limites, liquidez, impacto e plano revalidados"):
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self.acao("Compra bloqueada: diário de auditoria indisponível")
                continue
            self._last_paper_operation_id = None
            try:
                posicao = self.executor.comprar(proposta["mint"], cotacao,
                                                proposta["analysis"], token, plano_atual)
            except (PaperOperationAuthorizationError, PaperOperationError) as exc:
                descartar(token)
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao(
                    "execution_blocked", proposta,
                    f"persistencia Paper recusada ({type(exc).__name__})",
                )
                self.acao(f"Proposta PAPER {proposta['proposal_id'][:12]} bloqueada na persistencia Paper")
                continue
            if posicao:
                compras += 1
                resolver_sinal(proposta["proposal_id"], "comprado")
                self._registar_auditoria_aprovacao("executed", proposta, "operacao Paper persistida")
            else:
                resolver_sinal(proposta["proposal_id"], "sem_vaga")
                self._registar_auditoria_aprovacao("execution_blocked", proposta,
                                                    "Executor recusou a operacao")
        if alterou:
            self.atualizar_snapshot()
        return alterou, compras

    def ciclo_risco(self):
        e = self.estado
        mints = list(e["posicoes"]) + [m for m in e["percursos"] if m not in e["posicoes"]]
        if not mints:
            return 0
        pares = buscar_pares(self.http, mints)
        self.registar_market_snapshots(pares, mints)
        if pares is None:
            self._guardar_cotacoes_recentes({mint: None for mint in mints})
            log.warning("Sem resposta da API de precos; tento outra vez no proximo ciclo.")
            try:
                self.risco.registrar_indisponibilidade("API sem resposta")
            except Exception:
                pass
            return 0
        self._guardar_cotacoes_recentes(pares)
        self._pares_risco = {m: pares.get(m) for m in e["posicoes"] if pares.get(m) is not None}
        vendas = self.risco.verificar(pares)
        try:
            self.atualizar_percursos(pares)
        except Exception:
            log.exception("Erro a gravar percursos")
        return vendas

    def ciclo_rapido(self):
        """Preco de VENDA real (cotacao Jupiter) da posicao aberta, de poucos em poucos segundos.

        So leitura: nao assina nem envia nada. Os stops/trailing passam a ver o preco quase em tempo real;
        a liquidez (regras de rug) continua a vir da DexScreener, lida de 5 em 5 segundos com posicao aberta.
        """
        posicoes = self.estado["posicoes"]
        rapidos = self.risco.precos_rapidos
        for mint in [m for m in rapidos if m not in posicoes]:
            rapidos.pop(mint, None)
            self._avisos_preco_rapido.pop(mint, None)
            if self.preco_rapido is not None:
                self.preco_rapido.esquecer(mint)
        for chave in [k for k in self._avisos_tg_rapido if k[0] not in posicoes]:
            self._avisos_tg_rapido.pop(chave, None)
        if self.preco_rapido is None or not posicoes:
            return 0
        self._ultimo_ciclo_rapido = agora()
        novo = False
        for mint, pos in list(posicoes.items()):
            ds = num(pos.get("preco_ds")) or num(pos.get("preco_atual")) or num(pos.get("preco_entrada"))
            r = self.preco_rapido.cotar(mint, num(pos.get("tokens")), ds if ds > 0 else None)
            if r.get("estado") != "ok":
                erro = r.get("erro")
                if self._avisos_preco_rapido.get(mint) != erro:
                    self._avisos_preco_rapido[mint] = erro
                    log.warning("Preco rapido Jupiter indisponivel para %s: %s (continua a DexScreener)",
                                pos.get("simbolo"), erro)
                    self._avisar_preco_rapido(mint, pos, erro)
                continue
            preco = num(r.get("preco"))
            anterior = rapidos.get(mint)
            limite = num(self.cfg.get("preco_rapido_desvio_max_pct"), 35) / 100.0
            if not anterior and ds > 0 and abs(preco / ds - 1) > limite:
                if self._avisos_preco_rapido.get(mint) != "desvio":
                    self._avisos_preco_rapido[mint] = "desvio"
                    log.warning("Preco rapido Jupiter de %s ignorado: %.1f%% de diferenca face a DexScreener",
                                pos.get("simbolo"), (preco / ds - 1) * 100)
                continue
            if self._avisos_preco_rapido.pop(mint, None) is not None or not anterior:
                log.info("Preco rapido Jupiter ativo para %s (%s)", pos.get("simbolo"), self.preco_rapido.origem)
            rapidos[mint] = {"preco": preco, "ts": agora(), "impacto_pct": r.get("impacto_pct"),
                             "rota": r.get("rota")}
            self._ponto_fino(mint, preco)  # laboratorio: aproveita a cotacao, sem pedidos a mais
            novo = True
        if not novo or not self._pares_risco:
            return 0
        return self.risco.verificar(self._pares_risco, rapido=True)

    def _avisar_preco_rapido(self, mint, pos, erro):
        """Telegram quando a posicao aberta fica sem preco rapido (no maximo 1 aviso por minuto, por token
        e por tipo; a falta de rota de venda tem aviso proprio, que nunca fica escondido atras de outro)."""
        try:
            chave = (mint, erro == "sem_rota")
            if agora() - num(self._avisos_tg_rapido.get(chave)) < 60:
                return
            self._avisos_tg_rapido[chave] = agora()
            simbolo = pos.get("simbolo") or "?"
            if erro == "sem_rota":
                texto = (f"🚨 [PAPER] ALERTA — ${simbolo}\nO Jupiter deixou de ter rota de VENDA (possível rug).\n"
                         "O bot continua a vigiar pela DexScreener; vê a posição no dashboard.")
            else:
                texto = (f"⚠️ [PAPER] AVISO — ${simbolo}\nPreço rápido Jupiter indisponível ({erro}).\n"
                         "Stop e trailing estão a usar o preço da DexScreener, que é mais lento.")
            self.telegram.enviar(texto)
        except Exception:
            log.warning("Aviso de preco rapido por Telegram falhou")

    def _atualizar_urgencia_http(self):
        """Com posicao aberta, os pedidos HTTP ficam curtos (1 tentativa) para nao atrasarem os stops."""
        try:
            self.http.urgente = bool(self.estado["posicoes"])
        except AttributeError:
            pass

    def _vigiar_posicao_no_scanner(self):
        """Chamado entre analises do scanner: se houver posicao aberta e ja for hora, le o preco rapido."""
        if self.estado["posicoes"] and not self._vendas_manuais.empty():
            try:
                self._processar_vendas_manuais()  # uma venda pedida no Telegram nao espera pelo scanner
            except Exception:
                log.exception("Erro na venda manual (durante o scanner)")
        if not self.estado["posicoes"] or self.preco_rapido is None or not self.cfg.get("preco_rapido"):
            return 0
        if agora() - self._ultimo_ciclo_rapido < num(self.cfg.get("intervalo_preco_rapido_seg"), 3):
            return 0
        try:
            return self.ciclo_rapido()
        except Exception:
            log.exception("Erro no ciclo de preco rapido (durante o scanner)")
            return 0

    def _ponto_fino(self, mint, preco_venda):
        """Laboratorio (SO GRAVACAO): junta um ponto fino (preco de venda Jupiter) ao percurso do token.
        Nunca mexe em 'pts' nem em nenhuma decisao; o laboratorio continua a usar 'pts'."""
        cfg = self.cfg
        pc = self.estado["percursos"].get(mint)
        if not cfg.get("percursos_finos") or not isinstance(pc, dict):
            return False
        preco0, preco_venda = num(pc.get("preco0")), num(preco_venda)
        seg = int(agora() - num(pc.get("ts")))
        if preco0 <= 0 or preco_venda <= 0 or not 0 <= seg <= num(cfg.get("percurso_fino_minutos"), 30) * 60:
            return False
        finos = pc.get("ptsj")
        if not isinstance(finos, list):
            finos = pc["ptsj"] = []
        if finos and seg - num(finos[-1][0]) < num(cfg.get("percurso_fino_min_por_sinal_seg"), 10):
            return False  # pontos demasiado juntos nao acrescentam nada e enchem o estado.json
        finos.append([seg, round(preco_venda / preco0, 5)])
        del finos[:-1200]
        return True

    def ciclo_percursos_finos(self):
        """Laboratorio (SO GRAVACAO): 1 cotacao de venda Jupiter por chamada, a rodar pelos sinais recentes.

        O token da posicao aberta fica de fora: ja tem pontos do preco rapido. Com posicao aberta o ciclo
        principal chama isto mais devagar, para nao tirar pedidos Jupiter ao preco rapido (stop/trailing).
        Nao assina nem envia nada. Devolve 1 se gravou um ponto."""
        cfg, e = self.cfg, self.estado
        if self.preco_fino is None or not cfg.get("percursos_finos"):
            return 0
        if agora() < self._fino_pausa_ate:
            return 0
        limite = num(cfg.get("percurso_fino_minutos"), 30) * 60
        minimo = num(cfg.get("percurso_fino_min_por_sinal_seg"), 10)

        def recente(pc):  # ja tem um ponto fino ha menos de `minimo` segundos
            finos = pc.get("ptsj")
            return (isinstance(finos, list) and finos
                    and agora() - num(pc.get("ts")) - num(finos[-1][0]) < minimo)

        ativos = [m for m, pc in e["percursos"].items()
                  if isinstance(pc, dict) and pc.get("grupo", "sinal") != "controlo" and m not in e["posicoes"]
                  and num(pc.get("preco0")) > 0 and 0 <= agora() - num(pc.get("ts")) <= limite
                  and not recente(pc)]
        for antigo in [m for m in self._fino_ultimo if m not in e["percursos"]]:
            self._fino_ultimo.pop(antigo, None)
        if not ativos:
            return 0
        mint = min(ativos, key=lambda m: self._fino_ultimo.get(m, 0.0))  # o que esta ha mais tempo sem ponto
        self._fino_ultimo[mint] = agora()
        pc = e["percursos"][mint]
        ultimo = num(pc["pts"][-1][1]) if pc.get("pts") else 1.0
        ref = pc["preco0"] * (ultimo if ultimo > 0 else 1.0)  # ultimo preco DexScreener (valida as casas decimais)
        tokens = max(0.01, num(cfg.get("percurso_fino_valor_usd"), 5.5)) / pc["preco0"]
        r = self.preco_fino.cotar(mint, tokens, ref)
        if not isinstance(r, dict) or r.get("estado") != "ok":
            erro = r.get("erro") if isinstance(r, dict) else None
            if erro not in ("sem_rota", "sem_saida", "decimais_desconhecidos", "decimais_incoerentes",
                            "preco_incoerente", "mint_invalido", "quantidade_invalida"):
                # Falha de rede ou do Jupiter (nao deste token): cada tentativa pode demorar 3 s, por isso espera.
                self._fino_pausa_ate = agora() + 30
            return 0
        return 1 if self._ponto_fino(mint, r.get("preco")) else 0

    def _posicao_aberta(self, mint):
        return mint in self.estado.get("posicoes", {})

    def _pedido_venda_telegram(self, mint):
        """Regista o pedido; a venda e feita pelo ciclo principal (nunca a partir da thread do Telegram)."""
        cfg = self.cfg
        if cfg.get("mode") != "paper" or cfg.get("live_trading_enabled") is not False:
            return False, "modo Paper indisponivel; nada foi feito"
        if not self._posicao_aberta(mint):
            return False, "a posicao ja esta fechada"
        self._vendas_manuais.put(mint)
        return True, "pedido registado"

    def _processar_vendas_manuais(self):
        """Vende em Paper as posicoes pedidas no Telegram, ao preco mais recente conhecido."""
        vendas = 0
        while True:
            try:
                mint = self._vendas_manuais.get_nowait()
            except queue.Empty:
                return vendas
            pos = self.estado["posicoes"].get(mint)
            if pos is None:
                continue
            preco = (self.risco._preco_rapido_valido(mint) or num(pos.get("preco_atual"))
                     or num(pos.get("preco_entrada")))
            liq = num(pos.get("liq_atual")) or num(pos.get("liq_entrada"))
            if preco <= 0:
                log.warning("Venda manual de %s adiada: sem preco valido", pos.get("simbolo"))
                self._vendas_manuais.put(mint)
                return vendas
            try:
                if self.executor.vender(mint, preco, liq, "MANUAL (Telegram)") is not None:
                    vendas += 1
            except (PaperOperationError, PaperOperationAuthorizationError):
                log.exception("Venda manual de %s recusada na persistencia Paper", pos.get("simbolo"))

    def _proposta_pendente(self, proposal_id):
        gestor = getattr(self, "manual_approvals", None)
        return isinstance(gestor, ManualApprovalManager) and gestor.esta_pendente(proposal_id)

    def _decisao_telegram(self, proposal_id, decisao):
        """Mesmas verificacoes do dashboard. Devolve (ok, mensagem). Falha sempre fechada."""
        cfg = self.cfg
        if (cfg.get("mode") != "paper" or cfg.get("live_trading_enabled") is not False
                or cfg.get("manual_confirmation_required") is not True):
            return False, "modo Paper com confirmacao manual indisponivel; nada foi feito"
        if decisao == "aprovar" and self._emergency_stop_is_active():
            return False, "paragem de emergencia ativa; aprovacao recusada"
        gestor = getattr(self, "manual_approvals", None)
        if not isinstance(gestor, ManualApprovalManager):
            return False, "gestor de propostas indisponivel"
        return gestor.decidir_dashboard(proposal_id, decisao, origem="telegram")

    def _bloqueio_rota_compra(self, mint, valor_usd):
        """Confirma no Jupiter (so leitura) que ha rota de compra e que o impacto real e aceitavel.

        Devolve o motivo do bloqueio ou None. Se o Jupiter nao responder, NAO bloqueia (fica no registo)."""
        if self.preco_rapido is None or not self.cfg.get("verificar_rota_compra_jupiter"):
            return None
        r = self.preco_rapido.cotar_compra(mint, valor_usd)
        if r.get("estado") != "ok":
            if r.get("erro") == "sem_rota":
                return "Jupiter sem rota de compra (token nao negociavel)"
            log.warning("Verificacao de rota Jupiter indisponivel para %s: %s (nao bloqueia)", mint, r.get("erro"))
            return None
        impacto = r.get("impacto_pct")
        maximo = num(self.cfg.get("impacto_compra_jupiter_max_pct"), 1.5)
        if impacto is not None and impacto > maximo:
            return f"impacto real Jupiter {impacto:.2f}% acima do maximo {maximo:g}%"
        return None

    def _pedir_observacao_extra(self, mint, par, valor_usd):
        """So leitura: pede holders/mint/Jupiter em segundo plano. Nunca bloqueia nem decide."""
        obs = getattr(self, "obs_extra", None)
        if obs is None or not obs.ativo:
            return
        try:
            valor = float(valor_usd) if valor_usd and float(valor_usd) > 0 else 5.0 * 1.08  # ~5 EUR nominal
            preco = num(par.get("priceUsd"))
            liq = num(obter(par, "liquidity", "usd"))
            sim = round(slippage_total(self.cfg, valor, liq) * 100, 4) if liq > 0 else None
            obs.pedir(mint, round(valor, 4), preco if preco > 0 else None, sim)
        except Exception:
            log.exception("Observacao extra ignorada para %s", mint)

    # ---------- laboratorio: gravar o percurso real do preco de cada sinal ----------
    @staticmethod
    def _caracteristicas_entrada(par):
        """Dados do par no momento do sinal, para o laboratorio descobrir que entradas funcionam."""
        def n(*caminho):
            v = Analista._numero_presente(obter(par, *caminho))[0]
            return round(float(v), 4) if v is not None else None
        criado = n("pairCreatedAt")
        return {"m5": n("priceChange", "m5"), "h1": n("priceChange", "h1"), "h6": n("priceChange", "h6"),
                "compras_m5": n("txns", "m5", "buys"), "vendas_m5": n("txns", "m5", "sells"),
                "compras_h1": n("txns", "h1", "buys"), "vendas_h1": n("txns", "h1", "sells"),
                "vol_m5": n("volume", "m5"), "vol_h1": n("volume", "h1"), "fdv": n("fdv"),
                "idade_min": round((agora() - criado / 1000.0) / 60.0, 1) if criado else None}

    def iniciar_percurso(self, mint, par, analise, grupo="sinal"):
        cfg, pc = self.cfg, self.estado["percursos"]
        preco = num(par.get("priceUsd"))
        if not cfg["gravar_percursos"] or mint in pc or preco <= 0 or not MINT_VALIDO.fullmatch(mint):
            return
        if analise.get("pontuacao") is None:
            return
        controlo = sum(1 for v in pc.values() if v.get("grupo") == "controlo")
        if grupo == "controlo":
            if controlo >= int(num(cfg.get("percursos_controlo_max"), 0)):
                return
        elif len(pc) - controlo >= cfg["percursos_max_ativos"]:
            return
        pc[mint] = {"simbolo": simbolo_limpo(par), "pontuacao": int(analise["pontuacao"]), "ts": int(agora()),
                    "preco0": preco, "liq0": num(obter(par, "liquidity", "usd")),
                    "par": str(par.get("pairAddress", "")), "pts": [[0, 1.0]], "sem_dados": 0,
                    "fonte": "geckoterminal" if mint in getattr(self.scanner, "origem_gecko", ()) else "dexscreener",
                    "grupo": "controlo" if grupo == "controlo" else "sinal",
                    "feat": self._caracteristicas_entrada(par),
                    "comp": {k: round(num(v), 2) for k, v in (analise.get("detalhes") or {}).items()
                             if isinstance(k, str)}}

    def _fechar_percurso(self, mint, fim, liq_fim):
        pc = self.estado["percursos"].pop(mint)
        linha = {"mint": mint, "simbolo": pc["simbolo"], "pontuacao": pc["pontuacao"], "ts": pc["ts"],
                 "preco0": pc["preco0"], "liq0": pc["liq0"], "fim": fim, "liq_fim": liq_fim, "pts": pc["pts"]}
        obs = getattr(self, "obs_extra", None)
        extra = obs.obter(mint) if obs is not None else None
        if extra:
            linha["obs"] = extra
        if pc.get("fonte"):
            linha["fonte"] = pc["fonte"]
        if pc.get("grupo"):
            linha["grupo"] = pc["grupo"]
        if isinstance(pc.get("feat"), dict):
            linha["feat"] = pc["feat"]
        if isinstance(pc.get("comp"), dict) and pc["comp"]:
            linha["comp"] = pc["comp"]  # pontos de cada criterio da pontuacao (laboratorio)
        if pc.get("par"):
            linha["par"] = pc["par"]  # endereco publico da pool: o laboratorio usa-o para descontar a pool nos holders
        if isinstance(pc.get("ptsj"), list) and pc["ptsj"]:
            linha["ptsj"] = pc["ptsj"]  # pontos finos (preco de venda Jupiter); o laboratorio ainda usa so 'pts'
        try:
            with open(self.cfg["ficheiro_percursos"], "a", encoding="utf-8") as f:
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")
            self.estado["n_percursos"] = int(num(self.estado.get("n_percursos"))) + 1
        except OSError as erro:
            log.error("Nao consegui gravar o percurso: %s", erro)

    def atualizar_percursos(self, pares):
        cfg = self.cfg
        for mint in list(self.estado["percursos"]):
            pc = self.estado["percursos"][mint]
            lista = pares.get(mint)
            if lista is None:  # sem resposta da API para este token
                continue
            seg = int(agora() - pc["ts"])
            par = next((p for p in lista if str(p.get("pairAddress", "")) == pc["par"]
                        and num(p.get("priceUsd")) > 0), None) or melhor_par(lista)
            if par is None:
                pc["sem_dados"] = int(num(pc.get("sem_dados"))) + 1
                if pc["sem_dados"] >= cfg["falhas_sem_dados_max"]:
                    pc["pts"].append([seg, 0.0])
                    self._fechar_percurso(mint, "desapareceu", 0.0)
                continue
            pc["sem_dados"] = 0
            rel = round(num(par.get("priceUsd")) / pc["preco0"], 5)
            liq = num(obter(par, "liquidity", "usd"))
            ultimo = pc["pts"][-1]
            rug = liq < cfg["rug_liquidez_minima_usd"] or (
                pc["liq0"] > 0 and liq < pc["liq0"] * (1 - cfg["rug_queda_liquidez_pct"] / 100.0))
            acabou = seg >= cfg["percurso_minutos"] * 60
            if rug or acabou or seg - ultimo[0] >= 60 or (ultimo[1] > 0 and abs(rel / ultimo[1] - 1) >= 0.004):
                pc["pts"].append([seg, rel])
            if rug:
                self._fechar_percurso(mint, "rug", liq)
            elif acabou:
                self._fechar_percurso(mint, "tempo", liq)

    # ---------- painel ----------
    def painel(self, total=None):
        cfg, e = self.cfg, self.estado
        total = self.valor_total() if total is None else total
        inicial = e["saldo_inicial"] or 1.0
        st = e["stats"]
        taxa_acerto = st["ganhos"] / st["fechados"] * 100 if st["fechados"] else 0.0
        L = []
        L.append("=" * 78)
        L.append("  BOT PAPER TRADING - MEMECOINS SOLANA   (SIMULACAO - sem dinheiro real)")
        L.append("=" * 78)
        L.append(f"  Hora: {data_txt()}    Telegram: {'ligado' if self.telegram.ativo else 'desligado'}")
        L.append(f"  Saldo livre: ${e['saldo']:.2f}   Valor total: ${total:.2f}   "
                 f"P&L: ${total - inicial:+.2f} ({(total / inicial - 1) * 100:+.2f}%)")
        if cfg.get("limites_operacionais_ativos"):
            fx = self._taxa_bce_info()
            limite = cfg.get("max_eur_per_operation")
            if fx["status"] == "valida":
                L.append(f"  Limite Paper: max. EUR {limite:.2f} / USD {fx['cap_usd']:.4f} (BCE {fx['reference_date']}, "
                         f"USD/EUR {fx['rate']}) | posicoes maximas: {cfg['max_posicoes_abertas']}")
            else:
                L.append(f"  Limite Paper: max. EUR {limite} | taxa BCE {fx['status']} - novas compras bloqueadas | "
                         f"posicoes maximas: {cfg['max_posicoes_abertas']}")
        L.append(f"  Trades fechados: {st['fechados']}   Ganhos: {st['ganhos']}   Perdas: {st['perdas']}   "
                 f"Taxa de acerto: {taxa_acerto:.0f}%   P&L realizado: ${st['pnl_total']:+.2f}")
        L.append(f"  Scanner: {self.ultimo_scan}")
        if self.url_web:
            L.append(f"  Plataforma de sinais: {self.url_web}")
        L.append("-" * 78)
        L.append(f"  POSICOES ABERTAS ({len(e['posicoes'])}/{cfg['max_posicoes_abertas']})")
        if not e["posicoes"]:
            L.append("    (nenhuma)")
        else:
            L.append(f"    {'TOKEN':<12}{'INVEST.':>9}{'PRECO %':>9}{'LIQUIDO %':>11}{'PICO %':>9}{'MIN':>6}  ESTADO")
            for pos in e["posicoes"].values():
                pe = pos["preco_entrada"] or 1.0
                var = (num(pos.get("preco_atual")) / pe - 1) * 100
                pico = (num(pos.get("pico")) / pe - 1) * 100
                receita, _, _ = simular_venda(cfg, pos, num(pos.get("preco_atual")), num(pos.get("liq_atual")))
                liquido = (receita / pos["custo"] - 1) * 100 if pos["custo"] else 0.0
                minutos = (agora() - pos["aberta_em"]) / 60
                estado_txt = "trailing ativo" if pos.get("trailing") else ""
                if pos.get("sem_dados"):
                    estado_txt = f"sem dados x{pos['sem_dados']}"
                L.append(f"    {pos['simbolo']:<12}{'$%.2f' % pos['custo']:>9}{var:>+8.1f}%{liquido:>+10.1f}%"
                         f"{pico:>+8.1f}%{minutos:>6.0f}  {estado_txt}")
            L.append("    (PRECO % = variacao do preco de mercado; LIQUIDO % = ja com slippage e taxas)")
        L.append("-" * 78)
        L.append("  ULTIMAS ACOES")
        ultimas = e["acoes"][-cfg["acoes_no_painel"]:]
        if not ultimas:
            L.append("    (ainda nada - a procurar tokens...)")
        for a in ultimas:
            L.append("    " + a[:72])
        L.append("-" * 78)
        L.append("  Ctrl+C para parar (guarda tudo e mostra o resumo). Detalhes em bot.log")
        if cfg["limpar_ecra"]:
            os.system("cls" if os.name == "nt" else "clear")
        print("\n".join(L), flush=True)

    def resumo(self):
        e = self.estado
        st = e["stats"]
        total = self.valor_total()
        inicial = e["saldo_inicial"] or 1.0
        horas = (agora() - num(e.get("inicio"), agora())) / 3600
        taxa_acerto = st["ganhos"] / st["fechados"] * 100 if st["fechados"] else 0.0
        L = ["", "=" * 60, "  RESUMO DA SIMULACAO (paper trading)", "=" * 60,
             f"  Inicio da simulacao: ha {horas:.1f} horas",
             f"  Saldo inicial:       ${inicial:.2f}",
             f"  Valor total atual:   ${total:.2f}",
             f"  P&L total:           ${total - inicial:+.2f} ({(total / inicial - 1) * 100:+.2f}%)",
             f"  Trades fechados:     {st['fechados']}",
             f"  Ganhos / Perdas:     {st['ganhos']} / {st['perdas']}",
             f"  Taxa de acerto:      {taxa_acerto:.1f}%",
             f"  P&L realizado:       ${st['pnl_total']:+.2f}",
             f"  Custos simulados:    ${st['custos_total']:.2f} (slippage + taxas)",
             f"  Melhor / pior trade: {st['melhor']:+.1f}% / {st['pior']:+.1f}%",
             f"  Posicoes abertas:    {len(e['posicoes'])}"]
        if self.cfg.get("limites_operacionais_ativos"):
            fx = self._taxa_bce_info()
            limite = self.cfg.get("max_eur_per_operation")
            if fx["rate"]:
                L.append(f"  Limite por operacao: max. EUR {limite:.2f} / USD ${fx['cap_usd']:.4f} | "
                         f"posicoes maximas: {self.cfg['max_posicoes_abertas']}")
                L.append(f"  Taxa BCE USD/EUR:    {fx['rate']} ({fx['reference_date']})")
            else:
                L.append(f"  Limite por operacao: max. EUR {limite} | posicoes maximas: "
                         f"{self.cfg['max_posicoes_abertas']}")
                L.append(f"  Taxa BCE USD/EUR:    {fx['status']} - novas compras bloqueadas")
        if st["por_motivo"]:
            L.append("  Por motivo de saida:")
            for motivo, v in sorted(st["por_motivo"].items()):
                L.append(f"    {motivo:<14} {v['n']:>3} trades   ${v['pnl']:+.2f}")
        L.append("=" * 60)
        L.append("  Lembra-te: resultados em simulacao NAO garantem resultados reais.")
        return "\n".join(L)

    def resumo_telegram(self):
        """Resumo curto para o telemovel (o resumo() completo continua a ser o do terminal)."""
        e = self.estado
        st = e["stats"]
        total = self.valor_total()
        inicial = e["saldo_inicial"] or 1.0
        acerto = f" ({st['ganhos'] / st['fechados'] * 100:.0f}%)" if st["fechados"] else ""
        abertas = ", ".join("$" + str(p.get("simbolo")) for p in e["posicoes"].values()) or "nenhuma"
        dia = obter(e, "scanner_counters", "daily", datetime.fromtimestamp(agora()).date().isoformat(), defeito={})
        dia = dia if isinstance(dia, dict) else {}
        return "\n".join([
            "📊 [PAPER] RESUMO",
            f"Carteira: ${total:.2f} ({(total / inicial - 1) * 100:+.1f}% desde o início)",
            f"Fechadas: {st['fechados']} · {st['ganhos']} ganhas · {st['perdas']} perdidas{acerto}",
            f"Posição aberta: {abertas}",
            f"Hoje: {int(num(dia.get('analysis_completed')))} tokens analisados · "
            f"{int(num(dia.get('approved')))} aprovados"])

    def _texto_estado_bot(self, icone, estado):
        abertas = ", ".join("$" + str(p.get("simbolo")) for p in self.estado["posicoes"].values())
        return (f"{icone} [PAPER] Bot {estado} · carteira ${self.valor_total():.2f} · "
                + (f"posição aberta: {abertas}" if abertas else "sem posição aberta"))

    # ---------- ciclo principal ----------
    def _fechar_observabilidade(self):
        observabilidade = getattr(self, "smart_protection_observability", None)
        if observabilidade is None:
            return
        try:
            fila_fechada = observabilidade.close()
            metrica_fila = observabilidade.metrics()
            if (not fila_fechada or metrica_fila["pending_events"] or metrica_fila["failed_events"]
                    or metrica_fila["dropped"] or metrica_fila["write_errors"]):
                log.error("Encerramento Smart Protection incompleto; eventos gravados=%d, pendentes=%d, falhados=%d, descartados=%d, erros=%d, worker_ativo=%s",
                          metrica_fila["written"], metrica_fila["pending_events"],
                          metrica_fila["failed_events"], metrica_fila["dropped"],
                          metrica_fila["write_errors"], metrica_fila["worker_alive"])
            else:
                log.info("Fila Smart Protection encerrada sem pendências; eventos gravados=%d",
                         metrica_fila["written"])
        except Exception:
            log.exception("Erro ao descarregar a fila Smart Protection")

    def correr(self):
        cfg = self.cfg
        if not cfg.get("limites_operacionais_ativos"):
            aplicar_config_operacional(cfg)
        if not self.process_lock.acquire():
            log.error("Outra instancia ja esta a operar este estado Paper Trading; arranque cancelado")
            print("Outra instancia ja esta a operar este estado Paper Trading; arranque cancelado.")
            return
        try:
            prox_scan = prox_risco = prox_rapido = prox_fino = 0.0
            self.manual_approvals.start()
            if cfg.get("telegram_botoes") and self.modo != "demo" and getattr(self.telegram, "ativo", False):
                manual = bool(cfg.get("telegram_venda_manual"))
                self.tg_decisoes = TelegramDecisoes(self.telegram.token, self.telegram.chat, self._decisao_telegram,
                                                    self._proposta_pendente, avisar=self.telegram.enviar,
                                                    vender=self._pedido_venda_telegram if manual else None,
                                                    posicao_aberta=self._posicao_aberta if manual else None)
                if self.tg_decisoes.ativo:
                    self.tg_decisoes.iniciar()
                log.info("Botoes Aprovar/Recusar no Telegram: %s", "ligados" if self.tg_decisoes.ativo
                         else "desligados (" + str(self.tg_decisoes.desligado_motivo) + ")")
            print("A arrancar... (primeira procura de tokens pode demorar alguns segundos)")
            self.url_web = iniciar_painel_web(self)
            self.retomar()
            self._atualizar_taxa_bce()
            try:
                signal.signal(signal.SIGTERM, _pedido_de_paragem)
            except (ValueError, OSError, AttributeError):
                pass
            try:
                self.telegram.enviar(self._texto_estado_bot("▶️", "iniciado"))
            except Exception:
                log.warning("Aviso de arranque por Telegram falhou")
            while True:
                t = agora()
                aprovacao_alterou, _ = self._processar_respostas_aprovacao()
                mexeu = aprovacao_alterou
                try:
                    if self._processar_vendas_manuais():
                        mexeu = True
                except Exception:
                    log.exception("Erro na venda manual")
                if self.tg_decisoes is not None:
                    self.tg_decisoes.fechar_posicoes(set(self.estado["posicoes"]))
                self._atualizar_taxa_bce()
                self._atualizar_urgencia_http()
                if t >= prox_risco:
                    try:
                        self.ciclo_risco()
                    except Exception:
                        log.exception("Erro no ciclo de risco")
                    prox_risco = agora() + (cfg.get("intervalo_risco_com_posicao_seg", cfg["intervalo_risco_seg"])
                                            if self.estado["posicoes"] else cfg["intervalo_risco_seg"])
                    mexeu = True
                if (cfg.get("preco_rapido") and self.preco_rapido is not None and self.estado["posicoes"]
                        and agora() >= max(prox_rapido, self._ultimo_ciclo_rapido
                                           + num(cfg.get("intervalo_preco_rapido_seg"), 3))):
                    try:
                        self.ciclo_rapido()
                    except Exception:
                        log.exception("Erro no ciclo de preco rapido")
                    prox_rapido = agora() + cfg.get("intervalo_preco_rapido_seg", 3)
                    mexeu = True
                if cfg.get("percursos_finos") and self.preco_fino is not None and agora() >= prox_fino:
                    try:
                        self.ciclo_percursos_finos()  # so gravacao; fica guardado no proximo ciclo normal
                    except Exception:
                        log.exception("Erro no ciclo de percursos finos")
                    passo = (cfg.get("percurso_fino_intervalo_com_posicao_seg", 5) if self.estado["posicoes"]
                             else cfg.get("percurso_fino_intervalo_seg", 2))
                    prox_fino = agora() + max(2, num(passo, 5))
                self._atualizar_urgencia_http()
                if t >= prox_scan:
                    try:
                        self.ciclo_scanner()
                    except Exception:
                        log.exception("Erro no ciclo do scanner")
                    prox_scan = agora() + cfg["intervalo_scanner_seg"]
                    mexeu = True
                if mexeu:
                    try:
                        self.registar_historico()
                        total_publicado = self.atualizar_snapshot()
                    except Exception:
                        log.exception("Erro a preparar a plataforma web")
                        total_publicado = None
                    if total_publicado is None:
                        try:
                            total_publicado = json.loads(self.snapshot).get("total")
                        except (TypeError, ValueError, AttributeError):
                            total_publicado = None
                    self.guardar()
                    try:
                        self.painel(total_publicado)
                    except Exception:
                        log.exception("Erro a desenhar o painel")
                    intervalo = cfg["telegram_resumo_min"] * 60
                    if intervalo and agora() - self.ultimo_resumo_tg >= intervalo:
                        self.ultimo_resumo_tg = agora()
                        try:
                            self.telegram.enviar(self.resumo_telegram())
                        except Exception:
                            log.warning("Resumo por Telegram falhou")
                dormir(0.5)
        except KeyboardInterrupt:
            print("\nA parar... a guardar o estado.")
        finally:
            if self.tg_decisoes is not None:
                self.tg_decisoes.parar()
            try:
                if self.servidor_web:
                    self.servidor_web.shutdown()
                    self.servidor_web.server_close()
            except Exception:
                log.exception("Erro ao encerrar o painel web")
            try:
                self._fechar_observabilidade()
                self.guardar()
                texto = self.resumo()
                print(texto)
                self.telegram.em_segundo_plano = False  # a sair: envia ja, senao a mensagem perdia-se com o processo
                self.telegram.enviar(self._texto_estado_bot("⏹️", "parado"))
            except Exception:
                log.exception("Erro durante o encerramento do bot Paper")
            finally:
                self.close()


# ============================================================================
# Arranque
# ============================================================================
# ============================================================================
# LABORATORIO - que regras teriam dado mais, com os percursos reais gravados?
# ============================================================================
def repetir_percurso(pc, regra, cfg, valor_usd=12.0):
    """Aplica uma regra (stop, alvo, trailing_ativar, trailing_dist, minutos) a um percurso gravado.
    Devolve (resultado liquido em %, motivo). Usa os mesmos custos simulados do bot."""
    stop, alvo, ativar, dist, minutos = regra
    pico, preco, motivo, trailing = 1.0, 1.0, "fim", False
    pts = pc["pts"]
    for i, (seg, preco) in enumerate(pts[1:], 1):
        pico = max(pico, preco)
        v = (preco - 1) * 100
        if preco <= 0:
            return -100.0, "desapareceu"
        if i == len(pts) - 1 and pc.get("fim") == "rug":
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
    liquido = (1 - taxa) / (1 + s_ent) * preco * (1 - s_sai) * (1 - taxa)
    return (liquido - 1) * 100.0, motivo


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
        vendas = [t for t in (ler_trades_paper(cfg) or []) if t.get("tipo") == "VENDA" and t.get("pnl_usd") is not None]
    except Exception:
        vendas = []
    if not vendas:
        return L + ["    Ainda sem operacoes fechadas (ou trades.csv indisponivel)."]
    dias = {}
    for t in vendas:
        dias.setdefault(str(t.get("data_hora") or "")[:10], []).append(t)
    for dia in sorted(dias):
        g = dias[dia]
        pnl = sum(num(t["pnl_usd"]) for t in g)
        ganhas = sum(1 for t in g if num(t["pnl_usd"]) > 0)
        media = sum(num(t.get("pnl_pct")) for t in g) / len(g)
        L.append(f"    {dia}   {len(g):>3} fechadas   {ganhas:>2} ganhas   resultado ${pnl:+7.2f}   media {media:+6.2f}% por operacao")
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
    todos = ler_percursos(cfg)
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
        L += ["", "  PROVA DOS NOVE (a melhor regra da 1.a metade dos sinais, testada na 2.a metade)",
              f"    {_regra_txt(melhor)}",
              f"    1.a metade: {m_a:+6.2f}% por trade     2.a metade: {m_b:+6.2f}% por trade (acerto {ac_b:.1f}%)",
              "    Se a 2.a metade for muito pior, a regra so 'acertou' por acaso - nao confies nela."]

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
    L += _secao_por_dia(cfg)
    L += ["", "  COMO LER", "    - 'media' ja inclui os custos simulados (slippage e taxas, a entrada e a saida).",
          "    - Fora da seccao PONTOS FINOS, tudo usa os pontos normais (DexScreener, ~30 em 30 s).",
          "    - Conta cada sinal como um trade, sem o limite de posicoes abertas.",
          "    - Se NENHUMA combinacao tiver media positiva, o problema esta nas entradas (o Analista),",
          "      nao nas saidas: nenhuma regra de venda salva compras sem vantagem.",
          "    - Resultado em simulacao nao garante resultado com dinheiro real.", "=" * 74]
    return "\n".join(L)


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


def _pedido_de_paragem(*_):
    raise KeyboardInterrupt


def configurar_log(cfg):
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    try:
        h = logging.FileHandler(cfg["ficheiro_log"], encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
    except OSError:
        log.addHandler(logging.NullHandler())


def aplicar_config_operacional(cfg, config_path=None):
    """Load operational Paper limits; invalid config always blocks new orders."""
    config_path = config_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_operacoes.json")
    cfg["limites_operacionais_ativos"] = True
    cfg["_operational_config_path"] = os.path.abspath(config_path)
    try:
        limites = load_operational_config(config_path)
    except OperationalConfigError as erro:
        cfg["erro_config_operacional"] = str(erro)
        cfg["max_posicoes_abertas"] = 1
        cfg["max_eur_per_operation"] = None
        log.error("Configuracao operacional invalida; novas compras bloqueadas: %s", erro)
    else:
        cfg["erro_config_operacional"] = None
        cfg["mode"] = limites["mode"]
        cfg["max_posicoes_abertas"] = limites["max_open_positions"]
        cfg["max_eur_per_operation"] = limites["max_eur_per_operation"]
        cfg["live_trading_enabled"] = limites["live_trading_enabled"]
        cfg["private_keys_in_application"] = limites["private_keys_in_application"]
        cfg["manual_confirmation_required"] = limites["manual_confirmation_required"]
        cfg["emergency_stop_enabled"] = limites["emergency_stop_enabled"] is True
    return cfg


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _journal_tree_hashes(path):
    result = {}
    def raise_walk_error(error):
        raise PaperOperationError(f"nao foi possivel percorrer o diario Paper: {error}") from error

    for root, _dirs, files in os.walk(path, followlinks=False, onerror=raise_walk_error):
        for name in sorted(files):
            full = os.path.join(root, name)
            relative = os.path.relpath(full, path).replace(os.sep, "/")
            result[relative] = _sha256_file(full)
    return result


def reiniciar(cfg, confirmado=False):
    """Reset only after a read-only journal audit and a verified archive."""
    if not confirmado:
        raise PaperOperationError("reset exige confirmacao explicita: --reset --confirmar-reset")

    # Resolve once before locking, then repeat the read-only preflight while holding
    # the exact canonical state lock. No data/archive mutation precedes this check.
    paths = canonical_reset_paths(cfg["ficheiro_estado"], cfg["ficheiro_trades"])
    state_path = paths["state"]
    with state_access_lock(state_path):
        paths = canonical_reset_paths(state_path, paths["trades"])
        state_path = paths["state"]
        trades_path = paths["trades"]
        marker = paths["marker"]
        journal_dir = paths["journal"]
        if os.path.lexists(marker):
            raise PaperOperationError(f"ja existe um reset pendente; dados preservados: {marker}")

        auditor = PaperOperationStore.for_read_only_audit(state_path, trades_path, CABECALHO_CSV)
        resultado = auditor.audit()
        if resultado["pending_ids"]:
            raise PaperOperationError(
                f"reset recusado: existem intents pendentes: {resultado['pending_ids']}"
            )

        archive_root = paths["archive_root"]
        archive_name = "reset_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        archive = os.path.join(archive_root, archive_name)
        os.makedirs(archive, exist_ok=False)

        archived = {}
        for label, source_path in (("estado.json", state_path), ("trades.csv", trades_path)):
            if os.path.exists(source_path):
                destination = os.path.join(archive, label)
                shutil.copy2(source_path, destination)
                source_hash = _sha256_file(source_path)
                if _sha256_file(destination) != source_hash:
                    raise PaperOperationError(f"copia de arquivo nao passou verificacao: {label}")
                archived[label] = source_hash
            else:
                archived[label] = None

        journal_hashes = _journal_tree_hashes(journal_dir) if os.path.isdir(journal_dir) else None
        atomic_write_json(os.path.join(archive, "manifest.json"), {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "state_path": state_path,
            "trades_path": trades_path,
            "archived_sha256": archived,
            "journal_files_sha256": journal_hashes,
            "note": "Arquivo anterior ao reset; nao remover automaticamente.",
        })
        atomic_write_json(marker, {
            "status": "reset_in_progress",
            "archive": archive,
            "archived_sha256": archived,
            "journal_files_sha256": journal_hashes,
        })

        try:
            if journal_hashes is not None:
                journal_archive = os.path.join(archive, "paper_operations")
                os.replace(journal_dir, journal_archive)
                if _journal_tree_hashes(journal_archive) != journal_hashes:
                    raise PaperOperationError("diario arquivado diverge do manifesto")

            atomic_write_json(state_path, estado_novo(cfg))
            header = (",".join(CABECALHO_CSV) + "\n").encode("utf-8")
            atomic_write_bytes(trades_path, header)

            with open(state_path, "r", encoding="utf-8") as source:
                estado_reset = json.load(source)
            if (estado_reset.get("saldo") != float(cfg["saldo_inicial_usd"])
                    or estado_reset.get("posicoes") != {}
                    or estado_reset.get("paper_state_revision") != 0
                    or estado_reset.get("paper_ops_applied") != []):
                raise PaperOperationError("estado novo nao passou a verificacao do reset")
            fields, rows = auditor._read_csv()
            if fields != CABECALHO_CSV or rows:
                raise PaperOperationError("CSV novo nao passou a verificacao do reset")
            if os.path.exists(journal_dir):
                raise PaperOperationError("diario antigo continua no caminho ativo apos reset")
            os.remove(marker)
        except Exception as exc:
            raise PaperOperationError(
                f"reset incompleto; arranque bloqueado e arquivo preservado em {archive}: {exc}"
            ) from exc

        print(f"Arquivo anterior preservado em {archive}")
        print(f"Simulacao reiniciada com ${cfg['saldo_inicial_usd']:.2f}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bot de paper trading de memecoins Solana (simulacao).")
    ap.add_argument("--reset", action="store_true", help="recomeca a simulacao com o saldo inicial")
    ap.add_argument("--confirmar-reset", action="store_true",
                    help="confirma explicitamente o reset; usar em conjunto com --reset")
    ap.add_argument("--resumo", action="store_true", help="mostra o resumo atual e sai")
    ap.add_argument("--analisar", action="store_true",
                    help="laboratorio: testa muitas regras de saida nos percursos reais ja gravados")
    ap.add_argument("--demo", action="store_true",
                    help="mercado INVENTADO, so para ver a plataforma web a funcionar (nao testa a estrategia)")
    ap.add_argument("--sem-limpar", action="store_true", help="nao limpa o ecra a cada atualizacao")
    args = ap.parse_args(argv)
    if args.confirmar_reset and not args.reset:
        ap.error("--confirmar-reset so pode ser usado com --reset")

    # os ficheiros ficam sempre na pasta do bot, seja qual for a pasta de onde o corres
    pasta = os.path.dirname(os.path.abspath(__file__))
    cfg = dict(CONFIG)
    for chave in ("ficheiro_estado", "ficheiro_trades", "ficheiro_log", "ficheiro_percursos"):
        if not os.path.isabs(cfg[chave]):
            cfg[chave] = os.path.join(pasta, cfg[chave])
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if args.sem_limpar:
        cfg["limpar_ecra"] = False
    if args.analisar:  # so leitura dos percursos gravados: pode correr com o bot ligado
        print(analisar(cfg))
        return 0
    process_lock = ProcessInstanceLock(cfg["ficheiro_estado"])
    if not process_lock.acquire():
        print("Outra instancia ja esta a operar este estado Paper Trading; arranque cancelado.")
        return 2
    bot = None
    try:
        config_path = os.path.join(pasta, "config_operacoes.json")
        aplicar_config_operacional(cfg, config_path)
        configurar_log(cfg)
        if args.demo:
            preparar_demo(cfg)
        if args.reset:
            reiniciar(cfg, confirmado=args.confirmar_reset)
        bot = Bot(cfg, process_lock=process_lock)
        bot.obs_extra.ativo = bool(cfg.get("observacao_extra")) and not args.demo
        bot.obs_extra.rpc_url, bot.obs_extra.origem_rpc = configurar_rpc(pasta)
        log.info("Observacao extra: RPC %s", bot.obs_extra.origem_rpc)  # nunca regista a chave
        if not args.demo:
            bot.obs_extra.pasta_carteiras = pasta
            log.info("Carteiras seguidas (wallet_watchlist.json): %d", len(ler_carteiras_seguidas(pasta)))
        if not args.demo:
            chaves = ler_chaves_locais(pasta)
            if cfg.get("preco_rapido"):
                bot.preco_rapido = PrecoRapido(chaves["jupiter_api_key"])
                if cfg.get("percursos_finos"):
                    # Instancia separada: uma pausa por limite de pedidos aqui nunca trava o preco da posicao aberta.
                    bot.preco_fino = PrecoRapido(chaves["jupiter_api_key"], timeout=3.0, espera_429_seg=120.0)
            if not bot.telegram.ativo and chaves["telegram_token"] and chaves["telegram_chat_id"]:
                bot.telegram = Telegram(chaves["telegram_token"], chaves["telegram_chat_id"])
            bot.telegram.em_segundo_plano = True
            log.info("Preco rapido: %s; Telegram: %s",  # nunca regista chaves
                     bot.preco_rapido.origem if bot.preco_rapido else "desligado",
                     "ligado" if bot.telegram.ativo else "desligado")
        bot.scanner.gecko_ativo = bool(cfg.get("fonte_geckoterminal")) and not args.demo
        if args.demo:
            bot.modo = "demo"
            bot.risco.data_source = "demo_simulated"
            bot.telegram.ativo = False
            bot.http = bot.scanner.http = bot.analista.http = bot.risco.http = MercadoDemo()
        if args.resumo:
            print(bot.resumo())
            return 0
        bot.correr()
        return 0
    except (StateIntegrityError, PaperOperationError, RuntimeError) as erro:
        print(f"Arranque Paper bloqueado por integridade/lock: {erro}")
        logging.getLogger("paper").error("Arranque Paper bloqueado: %s", erro)
        return 2
    finally:
        if bot is not None:
            bot.close()
        process_lock.release()


if __name__ == "__main__":
    sys.exit(main())
