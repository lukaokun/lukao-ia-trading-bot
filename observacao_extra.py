# -*- coding: utf-8 -*-
"""Observacao extra (SO LEITURA) para tokens aprovados pelo Analista.

Recolhe tres sinais adicionais, apenas para registo e analise no laboratorio:
  1. Permissoes do mint (mint authority / freeze authority) via getAccountInfo
     (reutiliza smart_filters_security.TokenMintAuditor).
  2. Concentracao de holders: getTokenLargestAccounts + getTokenSupply no RPC
     publico da Solana (os 20 maiores holders; pode incluir a propria pool).
  3. Cotacao real do Jupiter (lite-api, sem chave) para o valor da ordem Paper:
     impacto de preco e slippage real face ao preco de referencia.

Este modulo NAO assina, NAO envia transacoes e NAO influencia decisoes de compra.
Todas as funcoes devolvem dicionarios e nunca lancam excecoes para o chamador.
"""
from __future__ import annotations

import json
import os
import base64
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import rede
from datetime import datetime, timezone

from smart_filters_security import PUBLIC_RPC_URL, parse_mint_account

JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
VERSAO = 2
FICHEIRO_CHAVES = "chaves_locais.json"  # ficheiro LOCAL (ignorado pelo Git); nunca e registado nem mostrado


def configurar_rpc(pasta=None, ambiente=None):
    """Devolve (url_rpc, origem). A chave nunca e devolvida isoladamente nem registada.

    Ordem: variavel HELIUS_API_KEY, variavel SOLANA_RPC_URL, ficheiro chaves_locais.json
    ({"helius_api_key": "..."} ou {"solana_rpc_url": "https://..."}), senao RPC publico.
    """
    amb = os.environ if ambiente is None else ambiente
    chave = str(amb.get("HELIUS_API_KEY") or "").strip()
    url = str(amb.get("SOLANA_RPC_URL") or "").strip()
    if not chave and not url and pasta:
        try:
            with open(os.path.join(pasta, FICHEIRO_CHAVES), "r", encoding="utf-8") as f:
                dados = json.load(f)
            if isinstance(dados, dict):
                chave = str(dados.get("helius_api_key") or "").strip()
                url = str(dados.get("solana_rpc_url") or "").strip()
        except (OSError, ValueError, UnicodeError):
            pass
    if chave and re.fullmatch(r"[A-Za-z0-9-]{16,80}", chave):
        return f"https://mainnet.helius-rpc.com/?api-key={chave}", "helius"
    if url.startswith("https://") and len(url) < 400:
        return url, "personalizado"
    return PUBLIC_RPC_URL, "publico"


def _agora_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_json(url, body=None, timeout=6.0, opener=None):
    """GET (body None) ou POST JSON. Devolve (dict|None, erro|None)."""
    if opener is None:  # uso real: requests (certificados certifi); o urllib falhava no Windows
        dados, erro, _corpo = rede.pedir_json(url, body=body, timeout=timeout)
        return dados, erro
    headers = {"Accept": "application/json", "User-Agent": "paper-bot-observacao/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    pedido = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with opener(pedido, timeout=timeout) as resposta:
            return json.loads(resposta.read(2_000_000).decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, "limite_pedidos" if exc.code == 429 else f"http_{exc.code}"
    except (TimeoutError, socket.timeout):
        return None, "timeout"
    except urllib.error.URLError as exc:
        motivo = getattr(exc, "reason", None)
        if isinstance(motivo, (TimeoutError, socket.timeout)):
            return None, "timeout"
        detalhe = re.sub(r"https?://\S+", "<url>", str(motivo or type(exc).__name__))[:70]
        return None, f"sem_ligacao: {detalhe}"
    except (ValueError, UnicodeDecodeError):
        return None, "resposta_invalida"
    except Exception as exc:  # nunca propagar
        return None, f"erro_{type(exc).__name__}"


def _num(valor):
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def concentracao_holders(mint, timeout=6.0, opener=None, rpc_url=PUBLIC_RPC_URL):
    """Percentagem do supply nos maiores holders (top 1 e top 10)."""
    out = {"estado": "indisponivel", "top1_pct": None, "top10_pct": None, "n_contas": None, "decimais": None, "erro": None}
    rpc = lambda metodo: _http_json(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": metodo,
                                                    "params": [mint, {"commitment": "confirmed"}]},
                                    timeout=timeout, opener=opener)
    supply_resp, erro = rpc("getTokenSupply")
    if erro or not isinstance(supply_resp, dict) or "error" in supply_resp:
        out["erro"] = erro or "rpc_erro_supply"
        return out
    contas_resp, erro = rpc("getTokenLargestAccounts")
    if erro or not isinstance(contas_resp, dict) or "error" in contas_resp:
        out["erro"] = erro or "rpc_erro_holders"
        return out
    try:
        supply = int(supply_resp["result"]["value"]["amount"])
        dec = supply_resp["result"]["value"].get("decimals")
        out["decimais"] = dec if isinstance(dec, int) and not isinstance(dec, bool) else None
        contas = [int(c["amount"]) for c in contas_resp["result"]["value"]]
    except (KeyError, TypeError, ValueError):
        out["erro"] = "resposta_invalida"
        return out
    if supply <= 0 or not contas:
        out["erro"] = "supply_ou_contas_vazios"
        return out
    contas.sort(reverse=True)
    out.update(estado="ok", n_contas=len(contas),
               top1_pct=round(contas[0] / supply * 100, 2),
               top10_pct=round(sum(contas[:10]) / supply * 100, 2))
    out["top"] = _donos_das_maiores_contas(contas_resp, supply, rpc_url, timeout, opener)
    return out


def _donos_das_maiores_contas(contas_resp, supply, rpc_url, timeout, opener, maximo=12):
    """Dono (carteira ou pool) de cada uma das maiores contas: [[dono, pct], ...], ou None se nao der.

    So leitura e opcional: se falhar, top1_pct e top10_pct continuam validos. Serve para o laboratorio poder
    descontar a pool de liquidez, que e muitas vezes o maior "holder". Sao enderecos publicos."""
    try:
        linhas = [(c["address"], int(c["amount"])) for c in contas_resp["result"]["value"]
                  if isinstance(c, dict) and isinstance(c.get("address"), str) and MINT_RE.fullmatch(c["address"])]
    except (KeyError, TypeError, ValueError):
        return None
    linhas = sorted(linhas, key=lambda x: -x[1])[:maximo]
    if not linhas:
        return None
    resp, erro = _http_json(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                                      "params": [[endereco for endereco, _ in linhas],
                                                 {"encoding": "jsonParsed", "commitment": "confirmed"}]},
                            timeout=timeout, opener=opener)
    if erro or not isinstance(resp, dict) or "error" in resp:
        return None
    try:
        valores = resp["result"]["value"]
    except (KeyError, TypeError):
        return None
    if not isinstance(valores, list) or len(valores) != len(linhas):
        return None
    top = []
    for (_, quantidade), conta in zip(linhas, valores):
        try:
            dono = conta["data"]["parsed"]["info"]["owner"]
        except (KeyError, TypeError):
            dono = None
        top.append([dono if isinstance(dono, str) and MINT_RE.fullmatch(dono) else None,
                    round(quantidade / supply * 100, 2)])
    return top


def cotacao_jupiter(mint, valor_usd, preco_ref_usd, decimais, timeout=6.0, opener=None):
    """Cotacao real USDC -> token para o valor da ordem Paper (nao executa nada)."""
    out = {"estado": "indisponivel", "impacto_pct": None, "slippage_real_pct": None,
           "rota": None, "valor_usd": valor_usd, "erro": None}
    v = _num(valor_usd)
    if not v or v <= 0:
        out["erro"] = "valor_invalido"
        return out
    params = urllib.parse.urlencode({"inputMint": USDC_MINT, "outputMint": mint,
                                     "amount": int(round(v * 10 ** USDC_DECIMALS)), "slippageBps": 300})
    resp, erro = _http_json(f"{JUPITER_QUOTE_URL}?{params}", timeout=timeout, opener=opener)
    if erro or not isinstance(resp, dict):
        out["erro"] = erro or "resposta_invalida"
        return out
    if resp.get("error") or resp.get("errorCode"):
        out["erro"] = str(resp.get("errorCode") or "sem_rota")[:60]
        return out
    impacto = _num(resp.get("priceImpactPct"))
    if impacto is not None:
        out["impacto_pct"] = round(impacto * 100, 4)  # Jupiter devolve fracao (0-1)
    try:
        rotas = [str(p["swapInfo"].get("label", "?"))[:30] for p in resp.get("routePlan", [])]
        out["rota"] = " > ".join(rotas)[:120] or None
    except (TypeError, KeyError, AttributeError):
        pass
    saida = _num(resp.get("outAmount"))
    ref = _num(preco_ref_usd)
    if saida and saida > 0 and ref and ref > 0 and isinstance(decimais, int) and 0 <= decimais <= 18:
        preco_efetivo = v / (saida / 10 ** decimais)
        out["slippage_real_pct"] = round((preco_efetivo / ref - 1) * 100, 3)
    out["estado"] = "ok" if impacto is not None else "parcial"
    return out


def auditar_mint(mint, timeout=6.0, opener=None, rpc_url=PUBLIC_RPC_URL):
    """getAccountInfo no RPC configurado + parser existente (smart_filters_security)."""
    resp, erro = _http_json(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                      "params": [mint, {"encoding": "base64", "commitment": "confirmed"}]},
                            timeout=timeout, opener=opener)
    if erro or not isinstance(resp, dict) or "error" in resp:
        return {"quality_status": "indisponivel", "erro": erro or "rpc_erro"}
    try:
        valor = resp["result"]["value"]
        slot = resp["result"].get("context", {}).get("slot")
        if valor is None:
            return {"quality_status": "not_found"}
        bruto = base64.b64decode(valor["data"][0], validate=True)
        return parse_mint_account(valor.get("owner"), bruto, slot=slot)
    except (KeyError, TypeError, ValueError, IndexError):
        return {"quality_status": "resposta_invalida"}


def observar(mint, valor_usd, preco_ref_usd, slippage_simulado_pct=None, timeout=6.0, opener=None, auditor=None,
             rpc_url=PUBLIC_RPC_URL, origem_rpc="publico"):
    """Recolhe as tres observacoes. Nunca lanca excecoes."""
    resultado = {"versao": VERSAO, "observado_utc": _agora_utc(), "modo": "observacao",
                 "slippage_simulado_pct": slippage_simulado_pct, "rpc": origem_rpc}
    if not isinstance(mint, str) or not MINT_RE.fullmatch(mint):
        resultado["erro"] = "mint_invalido"
        return resultado
    decimais = None
    try:
        auditoria = auditor.audit_mint(mint) if auditor else auditar_mint(mint, timeout, opener, rpc_url)
        decimais = auditoria.get("decimals") if isinstance(auditoria.get("decimals"), int) else None
        resultado["mint"] = {
            "estado": auditoria.get("quality_status"),
            "mint_authority": auditoria.get("mint_authority_state"),
            "freeze_authority": auditoria.get("freeze_authority_state"),
            "token_program": auditoria.get("token_program"),
        }
    except Exception as exc:
        resultado["mint"] = {"estado": f"erro_{type(exc).__name__}"}
    resultado["holders"] = concentracao_holders(mint, timeout=timeout, opener=opener, rpc_url=rpc_url)
    if decimais is None:
        decimais = resultado["holders"].get("decimais")
    resultado["jupiter"] = cotacao_jupiter(mint, valor_usd, preco_ref_usd, decimais, timeout=timeout, opener=opener)
    return resultado


class ObservadorExtra:
    """Corre observar() numa thread em segundo plano, uma vez por mint."""

    def __init__(self, ativo=True, max_itens=300, observar_fn=None, logger=None, rpc_url=PUBLIC_RPC_URL, origem_rpc="publico"):
        self.ativo = bool(ativo)
        self.rpc_url = rpc_url
        self.origem_rpc = origem_rpc
        self.max_itens = max_itens
        self._observar = observar_fn or observar
        self._log = logger
        self._lock = threading.Lock()
        self._dados = {}
        self._em_curso = set()

    def pedir(self, mint, valor_usd, preco_ref_usd, slippage_simulado_pct=None):
        if not self.ativo or not isinstance(mint, str):
            return False
        with self._lock:
            if mint in self._dados or mint in self._em_curso:
                return False
            self._em_curso.add(mint)
        t = threading.Thread(target=self._correr, args=(mint, valor_usd, preco_ref_usd, slippage_simulado_pct),
                             name=f"obs-{mint[:6]}", daemon=True)
        t.start()
        return True

    def _correr(self, mint, valor_usd, preco_ref_usd, slippage_simulado_pct):
        try:
            if self._observar is observar:
                r = observar(mint, valor_usd, preco_ref_usd, slippage_simulado_pct,
                             rpc_url=self.rpc_url, origem_rpc=self.origem_rpc)
            else:
                r = self._observar(mint, valor_usd, preco_ref_usd, slippage_simulado_pct)
        except Exception as exc:  # defensivo: observar() ja nao lanca
            r = {"versao": VERSAO, "modo": "observacao", "erro": f"erro_{type(exc).__name__}"}
        with self._lock:
            self._em_curso.discard(mint)
            self._dados[mint] = r
            while len(self._dados) > self.max_itens:
                self._dados.pop(next(iter(self._dados)))
        if self._log is not None:
            try:
                self._log.info("OBSERVACAO_EXTRA %s %s", mint, json.dumps(r, ensure_ascii=False, sort_keys=True))
            except Exception:
                pass

    def obter(self, mint):
        with self._lock:
            r = self._dados.get(mint)
            return dict(r) if isinstance(r, dict) else None

    def todos(self):
        with self._lock:
            return {m: dict(v) for m, v in self._dados.items()}
