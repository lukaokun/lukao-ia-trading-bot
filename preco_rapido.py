# -*- coding: utf-8 -*-
"""Preco rapido (SO LEITURA) para a posicao Paper aberta, via cotacao de venda do Jupiter.

Porque existe: o preco da DexScreener chega com atraso e so era lido de 10 em 10 segundos,
por isso os stops e o trailing disparavam muito depois do nivel configurado.

Como funciona: pede ao Jupiter uma cotacao de VENDA de toda a quantidade da posicao para USDC.
O preco resultante (USDC recebidos / tokens vendidos) e o preco a que a posicao podia
realmente ser vendida naquele momento, ja com o impacto de preco incluido.

Este modulo NAO assina, NAO envia transacoes e NAO guarda chaves privadas.
A chave de API do Jupiter (opcional, gratuita) vem do ficheiro local chaves_locais.json
e nunca e registada no log nem mostrada no dashboard.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import rede

URL_SEM_CHAVE = "https://lite-api.jup.ag/swap/v1/quote"
URL_COM_CHAVE = "https://api.jup.ag/swap/v1/quote"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
CHAVE_RE = re.compile(r"^[A-Za-z0-9_-]{16,100}$")
FICHEIRO_CHAVES = "chaves_locais.json"
SEM_ROTA = ("COULD_NOT_FIND_ANY_ROUTE", "NO_ROUTES_FOUND", "TOKEN_NOT_TRADABLE", "ROUTE_NOT_FOUND")


def ler_chaves_locais(pasta, ambiente=None):
    """Le chaves_locais.json (ficheiro local, ignorado pelo Git). Nunca lanca excecoes."""
    amb = os.environ if ambiente is None else ambiente
    dados = {}
    if pasta:
        try:
            with open(os.path.join(pasta, FICHEIRO_CHAVES), "r", encoding="utf-8-sig") as f:
                lido = json.load(f)
            if isinstance(lido, dict):
                dados = lido
        except (OSError, ValueError, UnicodeError):
            dados = {}
    jup = str(amb.get("JUPITER_API_KEY") or dados.get("jupiter_api_key") or "").strip()
    tg_token = str(amb.get("TELEGRAM_TOKEN") or dados.get("telegram_token") or "").strip()
    tg_chat = str(amb.get("TELEGRAM_CHAT_ID") or dados.get("telegram_chat_id") or "").strip()
    return {
        "jupiter_api_key": jup if CHAVE_RE.fullmatch(jup) else "",
        "telegram_token": tg_token if re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,80}", tg_token) else "",
        "telegram_chat_id": tg_chat if re.fullmatch(r"-?\d{3,20}", tg_chat) else "",
    }


def _num(valor):
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pedir(url, cabecalhos, timeout, opener):
    """GET JSON. Devolve (dict|None, erro|None, corpo_erro|None). Nunca lanca excecoes."""
    pedido = urllib.request.Request(url, headers=cabecalhos, method="GET")
    try:
        with opener(pedido, timeout=timeout) as resposta:
            return json.loads(resposta.read(1_000_000).decode("utf-8")), None, None
    except urllib.error.HTTPError as exc:
        corpo = None
        try:
            corpo = json.loads(exc.read(20_000).decode("utf-8"))
        except Exception:
            corpo = None
        return None, "limite_pedidos" if exc.code == 429 else f"http_{exc.code}", corpo
    except (TimeoutError, socket.timeout):
        return None, "timeout", None
    except urllib.error.URLError as exc:
        motivo = getattr(exc, "reason", None)
        if isinstance(motivo, (TimeoutError, socket.timeout)):
            return None, "timeout", None
        return None, "sem_ligacao", None
    except (ValueError, UnicodeDecodeError):
        return None, "resposta_invalida", None
    except Exception as exc:  # nunca propagar
        return None, f"erro_{type(exc).__name__}", None


class PrecoRapido:
    """Cotacao de venda Jupiter para as posicoes abertas (normalmente so uma)."""

    def __init__(self, api_key="", timeout=4.0, opener=None, relogio=None, espera_429_seg=30.0):
        self.api_key = api_key if CHAVE_RE.fullmatch(str(api_key or "")) else ""
        self.timeout = float(timeout)
        self._opener = opener  # None = biblioteca requests (rede.py); um opener so nos testes
        self._relogio = relogio or time.monotonic
        self.espera_429_seg = float(espera_429_seg)
        self._lock = threading.Lock()
        self.decimais = {}          # mint -> casas decimais (inferidas ou conhecidas)
        self.sem_rota = {}          # mint -> falhas seguidas por falta de rota
        self.bloqueado_ate = 0.0
        self.ultimo_erro = None
        self.pedidos = 0
        self.sucessos = 0

    def _pedir(self, url, cab):
        if self._opener is None:
            return rede.pedir_json(url, cabecalhos=cab, timeout=self.timeout)
        return _pedir(url, cab, self.timeout, self._opener)

    @property
    def origem(self):
        return "jupiter_chave" if self.api_key else "jupiter_publico"

    def _url_e_cabecalhos(self):
        cab = {"Accept": "application/json", "User-Agent": "paper-bot-preco-rapido/1.0"}
        if self.api_key:
            cab["x-api-key"] = self.api_key
            return URL_COM_CHAVE, cab
        return URL_SEM_CHAVE, cab

    def _cotar_bruto(self, mint, quantidade_bruta):
        url, cab = self._url_e_cabecalhos()
        params = urllib.parse.urlencode({"inputMint": mint, "outputMint": USDC_MINT,
                                         "amount": int(quantidade_bruta), "slippageBps": 300})
        self.pedidos += 1
        resp, erro, corpo = self._pedir(f"{url}?{params}", cab)
        if erro == "limite_pedidos":
            self.bloqueado_ate = self._relogio() + self.espera_429_seg
            return None, erro
        detalhe = None
        for fonte in (corpo, resp):
            if isinstance(fonte, dict) and (fonte.get("errorCode") or fonte.get("error")):
                detalhe = str(fonte.get("errorCode") or fonte.get("error"))[:60]
        if detalhe:
            return None, ("sem_rota" if any(s in detalhe.upper() for s in SEM_ROTA) else detalhe)
        if erro or not isinstance(resp, dict):
            return None, erro or "resposta_invalida"
        saida = _num(resp.get("outAmount"))
        if not saida or saida <= 0:
            return None, "sem_saida"
        return resp, None

    def cotar(self, mint, tokens, preco_ref=None):
        """Preco executavel de venda (USD por token) da quantidade `tokens`.

        Devolve {"estado": "ok", "preco", "impacto_pct", "rota", "decimais"} ou
        {"estado": "erro", "erro", "sem_rota_seguidas"}. Nunca lanca excecoes.
        """
        try:
            return self._cotar(mint, tokens, preco_ref)
        except Exception as exc:  # defensivo
            self.ultimo_erro = f"erro_{type(exc).__name__}"
            return {"estado": "erro", "erro": self.ultimo_erro, "sem_rota_seguidas": 0}

    def _cotar(self, mint, tokens, preco_ref):
        falha = lambda e: self._falha(mint, e)
        if not isinstance(mint, str) or not MINT_RE.fullmatch(mint):
            return falha("mint_invalido")
        qtd = _num(tokens)
        if not qtd or qtd <= 0:
            return falha("quantidade_invalida")
        if self._relogio() < self.bloqueado_ate:
            return falha("em_pausa_limite_pedidos")
        ref = _num(preco_ref)
        conhecido = mint in self.decimais
        dec = self.decimais.get(mint, 6)  # tokens pump.fun usam sempre 6 casas decimais
        if not conhecido and not mint.endswith("pump") and not (ref and ref > 0):
            return falha("decimais_desconhecidos")
        resp, erro = self._cotar_bruto(mint, qtd * 10 ** dec)
        if erro:
            return falha(erro)
        preco = (float(resp["outAmount"]) / 10 ** USDC_DECIMALS) / qtd
        if not conhecido and ref and ref > 0:
            # Infere as casas decimais: preco_calculado = preco_real * 10^(6 - decimais_reais)
            dec_real = dec - int(round(math.log10(preco / ref)))
            if not 0 <= dec_real <= 12:
                return falha("decimais_incoerentes")
            if dec_real != dec:
                dec = dec_real
                resp, erro = self._cotar_bruto(mint, qtd * 10 ** dec)
                if erro:
                    return falha(erro)
                preco = (float(resp["outAmount"]) / 10 ** USDC_DECIMALS) / qtd
            if abs(math.log10(preco / ref)) > 0.5:  # mais de ~3x de diferenca: nao confiar
                return falha("preco_incoerente")
        with self._lock:
            self.decimais[mint] = dec
            self.sem_rota[mint] = 0
        impacto = _num(resp.get("priceImpactPct"))
        try:
            rota = " > ".join(str(p["swapInfo"].get("label", "?"))[:30] for p in resp.get("routePlan", []))[:120]
        except (TypeError, KeyError, AttributeError):
            rota = None
        self.sucessos += 1
        self.ultimo_erro = None
        return {"estado": "ok", "preco": preco, "decimais": dec,
                "impacto_pct": round(impacto * 100, 4) if impacto is not None else None, "rota": rota or None}

    def cotar_compra(self, mint, valor_usd):
        """Cotacao de COMPRA (USDC -> token) para confirmar que ha rota e medir o impacto real.

        Devolve {"estado": "ok", "impacto_pct", "rota"} ou {"estado": "erro", "erro"}. Nunca lanca excecoes.
        """
        try:
            v = _num(valor_usd)
            if not isinstance(mint, str) or not MINT_RE.fullmatch(mint):
                return {"estado": "erro", "erro": "mint_invalido"}
            if not v or v <= 0:
                return {"estado": "erro", "erro": "valor_invalido"}
            if self._relogio() < self.bloqueado_ate:
                return {"estado": "erro", "erro": "em_pausa_limite_pedidos"}
            url, cab = self._url_e_cabecalhos()
            params = urllib.parse.urlencode({"inputMint": USDC_MINT, "outputMint": mint,
                                             "amount": int(round(v * 10 ** USDC_DECIMALS)), "slippageBps": 300})
            self.pedidos += 1
            resp, erro, corpo = self._pedir(f"{url}?{params}", cab)
            if erro == "limite_pedidos":
                self.bloqueado_ate = self._relogio() + self.espera_429_seg
                return {"estado": "erro", "erro": erro}
            for fonte in (corpo, resp):
                if isinstance(fonte, dict) and (fonte.get("errorCode") or fonte.get("error")):
                    detalhe = str(fonte.get("errorCode") or fonte.get("error"))[:60]
                    return {"estado": "erro",
                            "erro": "sem_rota" if any(s in detalhe.upper() for s in SEM_ROTA) else detalhe}
            if erro or not isinstance(resp, dict) or not _num(resp.get("outAmount")):
                return {"estado": "erro", "erro": erro or "resposta_invalida"}
            impacto = _num(resp.get("priceImpactPct"))
            try:
                rota = " > ".join(str(p["swapInfo"].get("label", "?"))[:30] for p in resp.get("routePlan", []))[:120]
            except (TypeError, KeyError, AttributeError):
                rota = None
            self.sucessos += 1
            return {"estado": "ok", "impacto_pct": round(impacto * 100, 4) if impacto is not None else None,
                    "rota": rota or None}
        except Exception as exc:  # defensivo
            return {"estado": "erro", "erro": f"erro_{type(exc).__name__}"}

    def _falha(self, mint, erro):
        seguidas = 0
        with self._lock:
            if erro == "sem_rota" and isinstance(mint, str):
                self.sem_rota[mint] = self.sem_rota.get(mint, 0) + 1
            seguidas = self.sem_rota.get(mint, 0) if isinstance(mint, str) else 0
        self.ultimo_erro = erro
        return {"estado": "erro", "erro": erro, "sem_rota_seguidas": seguidas}

    def esquecer(self, mint):
        with self._lock:
            self.decimais.pop(mint, None)
            self.sem_rota.pop(mint, None)
