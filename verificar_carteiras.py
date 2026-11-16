# -*- coding: utf-8 -*-
"""Verifica carteiras candidatas e (opcionalmente) grava o wallet_watchlist.json. SO LEITURA na rede.

Para cada candidata pede ao RPC as ultimas transacoes (1 pedido por carteira) e ve se a carteira existe
e esta ativa. Nao avalia lucros: so confirma que o endereco e real e que negoceia agora. Escolhe as mais
ativas (no maximo --max) para o bot as consultar em cada sinal, em modo de observacao.

Uso (na pasta do bot):
  python verificar_carteiras.py                  mostra a tabela, nao grava nada
  python verificar_carteiras.py --gravar         grava wallet_watchlist.json (guarda copia do anterior em .bak)

A chave do RPC nunca e mostrada. O ficheiro gravado e local e nunca vai para o Git.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

import observacao_extra as OE
from solana_swap_decoder import is_solana_address

FICHEIRO_CANDIDATAS = "carteiras_candidatas.json"
LIMITE_ASSINATURAS = 200


def ler_candidatas(caminho):
    """Devolve [(nome, endereco, fonte)] validas e sem repetidas. Aceita {"candidatas": [...]} ou uma lista."""
    with open(caminho, "r", encoding="utf-8-sig") as f:
        dados = json.load(f)
    lista = dados.get("candidatas") if isinstance(dados, dict) else dados
    resultado, vistos = [], set()
    for item in lista if isinstance(lista, list) else []:
        if isinstance(item, str):
            item = {"endereco": item}
        if not isinstance(item, dict):
            continue
        endereco = str(item.get("endereco") or "").strip()
        if endereco in vistos or not OE.MINT_RE.fullmatch(endereco) or not is_solana_address(endereco):
            continue
        vistos.add(endereco)
        resultado.append((str(item.get("nome") or "?")[:24], endereco, str(item.get("fonte") or "?")[:24]))
    return resultado


def atividade(endereco, rpc_url, agora=None, timeout=10.0, opener=None):
    """{"estado": "ok"|"vazia"|"erro", "ultima_h": horas desde a ultima transacao, "tx_24h": n, "erro": str|None}."""
    agora = time.time() if agora is None else agora
    resp, erro = OE._http_json(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                                         "params": [endereco, {"limit": LIMITE_ASSINATURAS}]},
                               timeout=timeout, opener=opener)
    if erro or not isinstance(resp, dict) or "error" in resp:
        return {"estado": "erro", "ultima_h": None, "tx_24h": None, "erro": erro or "rpc_erro"}
    linhas = resp.get("result")
    if not isinstance(linhas, list):
        return {"estado": "erro", "ultima_h": None, "tx_24h": None, "erro": "resposta_invalida"}
    tempos = [x.get("blockTime") for x in linhas if isinstance(x, dict) and isinstance(x.get("blockTime"), (int, float))
              and not isinstance(x.get("blockTime"), bool)]
    if not tempos:
        return {"estado": "vazia", "ultima_h": None, "tx_24h": 0, "erro": None}
    return {"estado": "ok", "ultima_h": max(0.0, (agora - max(tempos)) / 3600.0),
            "tx_24h": sum(1 for t in tempos if agora - t <= 86400), "erro": None}


def escolher(resultados, dias=7.0, maximo=OE.MAX_CARTEIRAS):
    """Fica com as carteiras ativas nos ultimos `dias`, ordenadas pelas mais ativas nas ultimas 24 h."""
    ativas = [r for r in resultados if r["estado"] == "ok" and r["ultima_h"] is not None and r["ultima_h"] <= dias * 24]
    ativas.sort(key=lambda r: (-r["tx_24h"], r["ultima_h"], r["endereco"]))
    return ativas[:max(0, min(int(maximo), OE.MAX_CARTEIRAS))]


def gravar_lista(pasta, escolhidas):
    """Grava wallet_watchlist.json (permissoes 600, mesmo dono da pasta). Guarda o anterior em .bak."""
    destino = os.path.join(pasta, OE.FICHEIRO_CARTEIRAS)
    if os.path.exists(destino):
        shutil.copy2(destino, destino + ".bak")
    conteudo = {"wallets": [r["endereco"] for r in escolhidas],
                "nomes": {r["endereco"]: r["nome"] for r in escolhidas},
                "gerado_em_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "nota": "Lista para OBSERVACAO. Carteiras publicas de sites de ranking; lucros nao verificados."}
    temporario = destino + ".novo"
    descritor = os.open(temporario, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descritor, "w", encoding="utf-8") as f:
        json.dump(conteudo, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        dono = os.stat(pasta)
        os.chown(temporario, dono.st_uid, dono.st_gid)
    except OSError:
        pass
    os.replace(temporario, destino)
    return destino


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verifica carteiras candidatas (so leitura) e grava a lista a seguir")
    ap.add_argument("--pasta", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--candidatas", default=None)
    ap.add_argument("--dias", type=float, default=7.0, help="so carteiras com atividade nos ultimos X dias")
    ap.add_argument("--max", type=int, default=OE.MAX_CARTEIRAS)
    ap.add_argument("--gravar", action="store_true", help="grava wallet_watchlist.json (sem isto, so mostra)")
    a = ap.parse_args(argv)
    caminho = a.candidatas or os.path.join(a.pasta, FICHEIRO_CANDIDATAS)
    try:
        candidatas = ler_candidatas(caminho)
    except (OSError, ValueError) as erro:
        print(f"ERRO: nao consegui ler as candidatas ({type(erro).__name__}); nada foi alterado.")
        return 1
    rpc_url, origem = OE.configurar_rpc(a.pasta)
    print(f"Candidatas validas: {len(candidatas)}   RPC: {origem}")
    if origem == "publico":
        print("AVISO: sem chave Helius o RPC publico limita os pedidos; o resultado pode vir incompleto.")
    resultados, erros_seguidos = [], 0
    for nome, endereco, fonte in candidatas:
        r = atividade(endereco, rpc_url)
        r.update(nome=nome, endereco=endereco, fonte=fonte)
        resultados.append(r)
        erros_seguidos = erros_seguidos + 1 if r["estado"] == "erro" else 0
        if erros_seguidos >= 5:
            print("ERRO: 5 falhas seguidas do RPC; parei. Nada foi alterado.")
            return 1
        time.sleep(0.15)
    escolhidas = escolher(resultados, a.dias, a.max)
    dentro = {r["endereco"] for r in escolhidas}
    print(f"{'nome':<24} {'endereco':<13} {'fonte':<18} {'ultima':>9} {'tx 24h':>7}  decisao")
    for r in sorted(resultados, key=lambda r: (r["endereco"] not in dentro, -(r["tx_24h"] or 0))):
        ultima = "-" if r["ultima_h"] is None else (f"{r['ultima_h']:.1f} h" if r["ultima_h"] < 48 else f"{r['ultima_h'] / 24:.0f} d")
        tx = "-" if r["tx_24h"] is None else (f"{r['tx_24h']}+" if r["tx_24h"] >= LIMITE_ASSINATURAS else str(r["tx_24h"]))
        decisao = ("SEGUIR" if r["endereco"] in dentro else "erro RPC: " + str(r["erro"]) if r["estado"] == "erro"
                   else "fora: sem transacoes (endereco errado?)" if r["estado"] == "vazia"
                   else f"fora: parada ha mais de {a.dias:g} dias" if (r["ultima_h"] or 0) > a.dias * 24
                   else "fora: lista cheia")
        print(f"{r['nome']:<24} {r['endereco'][:5]}..{r['endereco'][-4:]:<6} {r['fonte']:<18} {ultima:>9} {tx:>7}  {decisao}")
    print(f"\nA seguir: {len(escolhidas)} de {len(candidatas)} (maximo {min(a.max, OE.MAX_CARTEIRAS)}).")
    if not a.gravar:
        print("Nada foi gravado (falta --gravar).")
        return 0
    if not escolhidas:
        print("Nenhuma carteira ativa: nada foi gravado.")
        return 1
    print("Gravado:", gravar_lista(a.pasta, escolhidas))
    print("O bot le a lista a cada sinal: nao e preciso reiniciar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
