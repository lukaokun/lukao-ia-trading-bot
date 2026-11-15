# -*- coding: utf-8 -*-
"""Pedidos HTTP JSON (so leitura) para a observacao extra e o preco rapido.

Usa a biblioteca `requests`, a mesma que o bot ja usa com sucesso para a DexScreener:
traz os certificados atualizados (certifi) e reutiliza ligacoes. O `urllib` do Windows
falhava com CERTIFICATE_VERIFY_FAILED e WinError 10013 nesta maquina.

Nunca lanca excecoes e nunca devolve URLs nem chaves nas mensagens de erro.
"""
from __future__ import annotations

import re
import threading

try:
    import requests
except ImportError:  # o bot ja depende de requests; isto so protege importacoes em testes minimos
    requests = None

_local = threading.local()
_CHAVE_RE = re.compile(r"(api[-_]?key=)[^\s&'\")]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")


def limpar_erro(texto, limite=70):
    """Remove URLs e chaves de uma mensagem de erro."""
    texto = _CHAVE_RE.sub(r"\1***", str(texto or ""))
    texto = _URL_RE.sub("<url>", texto)
    texto = re.sub(r"with url: \S+", "with url: <url>", texto)
    return texto[:limite]


def _sessao():
    s = getattr(_local, "sessao", None)
    if s is None and requests is not None:
        s = _local.sessao = requests.Session()
    return s


def pedir_json(url, body=None, cabecalhos=None, timeout=6.0):
    """GET (body None) ou POST JSON. Devolve (dict|None, erro|None, corpo_erro|None)."""
    s = _sessao()
    if s is None:
        return None, "sem_requests", None
    cab = {"Accept": "application/json", "User-Agent": "paper-bot-observacao/1.0"}
    cab.update(cabecalhos or {})
    try:
        if body is None:
            r = s.get(url, headers=cab, timeout=timeout)
        else:
            r = s.post(url, json=body, headers=cab, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, "timeout", None
    except requests.exceptions.RequestException as exc:
        return None, f"sem_ligacao: {limpar_erro(exc)}", None
    except Exception as exc:  # nunca propagar
        return None, f"erro_{type(exc).__name__}", None
    try:
        dados = r.json()
    except ValueError:
        dados = None
    if r.status_code == 429:
        return None, "limite_pedidos", dados
    if r.status_code >= 400:
        return None, f"http_{r.status_code}", dados
    if dados is None:
        return None, "resposta_invalida", None
    return dados, None, None
