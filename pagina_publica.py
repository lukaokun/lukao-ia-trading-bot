# -*- coding: utf-8 -*-
"""Pagina publica SO DE LEITURA do MemeBot (Paper Trading).

Corre como um processo separado do bot e nunca escreve nada:
  /            -> Dashboard Neural em modo de visualizacao (sem aprovar/recusar, sem propostas)
  /api/estado  -> copia filtrada do estado que o bot publica em 127.0.0.1:8000 (sem propostas)
  /resultados  -> pagina simples de resultados (le estado.json e trades.csv)
Nao aceita POST, nao tem o token do dashboard e nao mostra chaves.

Uso:  python pagina_publica.py --porta 8080 --pasta /home/bot/memebot
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE_SEG = 5
BOT_URL = "http://127.0.0.1:8000"
_cache_bot = {"t": 0.0, "estado": None, "pagina": None, "pagina_t": 0.0}
CAMPOS_REMOVIDOS = ("propostas_paper", "paper_purchase_readiness")
# "carteiras" e "quem": a lista de carteiras seguidas e privada (o bot ja a retira; aqui e a segunda barreira)
CHAVES_INTERNAS = {"proposal_id", "session_id", "operation_id", "stop_id", "carteiras", "quem"}


def _limpar(v):
    """Remove identificadores internos (propostas, sessoes, operacoes) de qualquer nivel."""
    if isinstance(v, dict):
        return {k: _limpar(x) for k, x in v.items() if k not in CHAVES_INTERNAS}
    if isinstance(v, list):
        return [_limpar(x) for x in v]
    return v

INJECAO_HEAD = """<script>window.MEMEBOT_SO_LEITURA=true</script>
<style>#c-avisos,a.chip[href="/classico"],.al-acoes,.al .al-acoes,[data-acao]{display:none!important}
.so-leitura{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:90;padding:8px 14px;border-radius:999px;
background:rgba(4,16,17,.88);border:1px solid rgba(247,165,49,.45);color:#ffd9a3;font:600 11px ui-monospace,Consolas,monospace;
letter-spacing:.6px;backdrop-filter:blur(8px);white-space:nowrap;max-width:calc(100% - 24px);overflow:hidden;text-overflow:ellipsis}</style>
"""
INJECAO_BODY = ('<div class="so-leitura" role="note">MODO DE VISUALIZACAO · SIMULACAO (PAPER), SEM DINHEIRO REAL'
                ' · <a href="/resultados" style="color:#22e0c8">resultados</a></div>')
_lock = threading.Lock()
_cache = {"t": 0.0, "dados": None}


def _num(v, defeito=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return defeito
    return x if math.isfinite(x) else defeito


def _motivo_curto(m):
    m = str(m or "").upper()
    for chave, nome in (("STOP", "Stop-loss"), ("TRAILING", "Trailing"), ("RUG", "Rug"),
                        ("TEMPO", "Tempo"), ("ALVO", "Alvo"), ("TAKE", "Alvo"), ("PERDA", "Sem dados"),
                        ("MANUAL", "Manual")):
        if chave in m:
            return nome
    return "Venda"


def _ler_trades(pasta):
    vendas, compras = [], {}
    try:
        with open(os.path.join(pasta, "trades.csv"), encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                tipo = (r.get("tipo") or "").upper()
                if tipo == "COMPRA":
                    compras[r.get("mint")] = r
                elif tipo == "VENDA":
                    if str(r.get("motivo") or "").upper().startswith("PARCIAL"):
                        continue  # venda parcial (recuperar entrada): a posicao continua aberta
                    c = compras.get(r.get("mint")) or {}
                    vendas.append({
                        "simbolo": str(r.get("simbolo") or "?")[:20],
                        "mint": str(r.get("mint") or "")[:64],
                        "entrada": str(c.get("data_hora") or "")[:16],
                        "saida": str(r.get("data_hora") or "")[:16],
                        "pnl_pct": _num(r.get("pnl_pct")),
                        "pnl_usd": _num(r.get("pnl_usd")),
                        "motivo": _motivo_curto(r.get("motivo")),
                        "duracao_min": _num(r.get("duracao_min")),
                        "pontuacao": _num(r.get("pontuacao")),
                    })
    except OSError:
        pass
    return vendas


def _ler_dados(pasta):
    with open(os.path.join(pasta, "estado.json"), encoding="utf-8") as f:
        e = json.load(f)
    inicial = _num(e.get("saldo_inicial"), 100.0)
    posicoes = []
    valor_pos = 0.0
    for mint, p in (e.get("posicoes") or {}).items():
        entrada, atual = _num(p.get("preco_entrada"), 0), _num(p.get("preco_atual"), 0)
        tokens = _num(p.get("tokens"), 0)
        valor_pos += atual * tokens
        posicoes.append({
            "simbolo": str(p.get("simbolo") or "?")[:20], "mint": str(mint)[:64],
            "var_pct": (atual / entrada - 1) * 100 if entrada and atual else None,
            "desde": datetime.fromtimestamp(_num(p.get("aberta_em"), time.time())).strftime("%H:%M"),
        })
    saldo = _num(e.get("saldo"), inicial)
    total = saldo + valor_pos
    vendas = _ler_trades(pasta)
    ganhos = sum(1 for v in vendas if (v["pnl_usd"] or 0) > 0)
    hist = [[int(_num(a, 0)), round(_num(b, 0), 4)] for a, b in (e.get("historico") or [])[-400:]
            if _num(a) and _num(b) is not None]
    hoje = datetime.now().strftime("%Y-%m-%d")
    dia = ((e.get("scanner_counters") or {}).get("daily") or {}).get(hoje) or {}
    return {
        "atualizado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ultimo_ciclo": datetime.fromtimestamp(_num(e.get("ultimo_ciclo"), 0)).strftime("%Y-%m-%d %H:%M:%S")
        if _num(e.get("ultimo_ciclo")) else None,
        "ativo": (time.time() - _num(e.get("ultimo_ciclo"), 0)) < 180,
        "inicio": datetime.fromtimestamp(_num(e.get("inicio"), time.time())).strftime("%Y-%m-%d"),
        "saldo_inicial": inicial, "total": total, "pnl_usd": total - inicial,
        "pnl_pct": (total / inicial - 1) * 100 if inicial else 0,
        "fechados": len(vendas), "ganhos": ganhos, "perdas": len(vendas) - ganhos,
        "acerto": ganhos / len(vendas) * 100 if vendas else None,
        "melhor": max((v["pnl_pct"] for v in vendas if v["pnl_pct"] is not None), default=None),
        "pior": min((v["pnl_pct"] for v in vendas if v["pnl_pct"] is not None), default=None),
        "posicoes": posicoes, "vendas": vendas[-30:][::-1], "historico": hist,
        "hoje": {"recebidos": int(_num(dia.get("tokens_received"), 0)),
                 "analisados": int(_num(dia.get("analysis_completed"), 0)),
                 "aprovados": int(_num(dia.get("approved"), 0)),
                 "rejeitados": int(_num(dia.get("rejected"), 0))},
    }


def dados(pasta):
    with _lock:
        if _cache["dados"] is None or time.time() - _cache["t"] > CACHE_SEG:
            try:
                _cache["dados"] = _ler_dados(pasta)
            except (OSError, ValueError):
                pass  # ficheiro a meio de ser gravado: mantem a ultima leitura
            _cache["t"] = time.time()
        return _cache["dados"]


def _get_bot(caminho, timeout=4):
    with urllib.request.urlopen(BOT_URL + caminho, timeout=timeout) as r:
        return r.read(5_000_000)


def estado_publico():
    """Estado do bot sem as propostas (ninguem de fora ve nem decide propostas)."""
    with _lock:
        if _cache_bot["estado"] is None or time.time() - _cache_bot["t"] > 2:
            try:
                d = json.loads(_get_bot("/api/estado").decode("utf-8"))
                for campo in CAMPOS_REMOVIDOS:
                    d.pop(campo, None)
                d = _limpar(d)
                if isinstance(d.get("acoes"), list):
                    d["acoes"] = [a for a in d["acoes"] if "APPROVAL_AUDIT" not in str(a) and "propos" not in str(a).lower()]
                d["propostas_paper"] = []
                d["so_leitura"] = True
                _cache_bot["estado"] = json.dumps(d, ensure_ascii=False)
            except Exception:
                _cache_bot["estado"] = None
            _cache_bot["t"] = time.time()
        return _cache_bot["estado"]


def pagina_neural(pasta):
    """Dashboard Neural em modo de visualizacao: sem token, sem botoes de decisao."""
    with _lock:
        if _cache_bot["pagina"] is None or time.time() - _cache_bot["pagina_t"] > 30:
            try:
                with open(os.path.join(pasta, "assets", "dashboard_neural.html"), encoding="utf-8") as f:
                    html = f.read(2_000_000)
                if html.startswith("<!DOCTYPE html>") and "</head>" in html and "<body>" in html:
                    html = html.replace("__MEMEBOT_TOKEN__", "")
                    html = html.replace("</head>", INJECAO_HEAD + "</head>", 1)
                    html = html.replace("<body>", "<body>" + INJECAO_BODY, 1)
                    html = html.replace("<title>", "<title>(Visualizacao) ", 1)
                    _cache_bot["pagina"] = html
            except OSError:
                pass
            _cache_bot["pagina_t"] = time.time()
        return _cache_bot["pagina"]


PAGINA = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#041011">
<title>SpiderBot · Resultados (simulação)</title>
<style>
:root{--bg:#041011;--bg2:#071a1c;--lin:rgba(94,234,212,.14);--txt:#e8f1f0;--txt2:#b9cfcc;--mut:#7f9896;--tq:#22e0c8;--vd:#2ee6a8;--vm:#ff6b6b;--ou:#f7a531;
--mono:ui-monospace,"JetBrains Mono",Consolas,monospace}
*{box-sizing:border-box;margin:0}
body{background:radial-gradient(1200px 600px at 70% -10%,rgba(34,224,200,.10),transparent 60%),var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Poppins,sans-serif;min-height:100vh}
.w{max-width:1080px;margin:0 auto;padding:28px 18px 60px}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:22px}
h1{font-size:22px;letter-spacing:.2px}h1 b{color:var(--tq)}
.sub{color:var(--mut);font:12px var(--mono);letter-spacing:.6px;text-transform:uppercase}
.est{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--lin);border-radius:999px;font:12px var(--mono);color:var(--txt2)}
.est i{width:8px;height:8px;border-radius:50%;background:var(--mut)}.est.on i{background:var(--vd);box-shadow:0 0 10px var(--vd)}
.aviso{margin-bottom:20px;padding:12px 14px;border:1px solid rgba(247,165,49,.35);background:rgba(247,165,49,.07);border-radius:12px;color:#ffd9a3;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}
.card{background:linear-gradient(180deg,rgba(255,255,255,.025),transparent),var(--bg2);border:1px solid var(--lin);border-radius:14px;padding:16px}
.k{color:var(--mut);font:11px var(--mono);letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
.v{font:700 24px var(--mono);letter-spacing:-.5px}.v small{font-size:13px;font-weight:500;color:var(--txt2)}
.pos{color:var(--vd)}.neg{color:var(--vm)}
.big{grid-column:span 2}.big .v{font-size:34px}
.graf{margin-bottom:12px}.graf svg{width:100%;height:220px;display:block}
.tw{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13.5px}
th{color:var(--mut);font:11px var(--mono);letter-spacing:1px;text-transform:uppercase;text-align:left;padding:8px 10px;border-bottom:1px solid var(--lin);white-space:nowrap}
td{padding:10px;border-bottom:1px solid rgba(94,234,212,.06);white-space:nowrap}td.n{font-family:var(--mono);text-align:right}th.n{text-align:right}
a{color:var(--tq);text-decoration:none}a:hover{text-decoration:underline}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font:11px var(--mono);border:1px solid var(--lin);color:var(--txt2)}
.duas{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.lin{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed rgba(94,234,212,.08);font-size:14px}.lin:last-child{border:0}.lin b{font-family:var(--mono)}
.vazio{color:var(--mut);font-size:14px;padding:8px 0}
footer{margin-top:22px;color:var(--mut);font-size:12px;text-align:center}
@media(max-width:760px){.grid{grid-template-columns:1fr 1fr}.big{grid-column:span 2}.duas{grid-template-columns:1fr}.big .v{font-size:28px}.v{font-size:20px}}
header{flex-direction:column;justify-content:center;text-align:center;gap:10px}
h1.nome{font:900 40px/1 Poppins,system-ui,sans-serif;letter-spacing:1px;color:#fff;text-shadow:-2px 0 rgba(255,60,120,.85),2px 0 rgba(34,224,200,.9),0 0 18px rgba(34,224,200,.35);animation:glitchNome 7s steps(1) infinite}
header .sub{margin-top:8px;letter-spacing:3px}
@keyframes glitchNome{0%,92%,100%{transform:none;text-shadow:-2px 0 rgba(255,60,120,.85),2px 0 rgba(34,224,200,.9),0 0 18px rgba(34,224,200,.35)}93%{transform:translateX(1px) skewX(-7deg);text-shadow:-5px 0 rgba(255,60,120,.9),4px 0 rgba(34,224,200,.9),0 0 18px rgba(34,224,200,.35)}95%{transform:translateX(-1px);text-shadow:3px 0 rgba(255,60,120,.9),-4px 0 rgba(34,224,200,.9),0 0 18px rgba(34,224,200,.35)}97%{transform:none;text-shadow:-1px 0 rgba(255,60,120,.85),1px 0 rgba(34,224,200,.9),0 0 24px rgba(34,224,200,.35)}}
@media(max-width:760px){h1.nome{font-size:32px}header .sub{letter-spacing:1.2px;font-size:11px}}
@media(prefers-reduced-motion:reduce){h1.nome{animation:none}}
</style>
</head>
<body>
<div class="w">
<header>
 <div><h1 class="nome">SpiderBot</h1><div class="sub">Resultados · Solana · meme coins · Paper Trading</div></div>
 <span class="est" id="est"><i></i><span id="est-t">a ligar…</span></span>
</header>
<div class="aviso"><b>Simulação (Paper Trading).</b> Nenhuma operação usa dinheiro real. Os resultados são simulados com preços reais do mercado e custos estimados, e não garantem resultados futuros. Isto não é aconselhamento financeiro.</div>
<div class="grid">
 <div class="card big"><div class="k">Valor total da carteira</div><div class="v" id="total">—</div><div id="pnl" class="sub" style="margin-top:6px">—</div></div>
 <div class="card"><div class="k">Operações fechadas</div><div class="v" id="fech">—</div></div>
 <div class="card"><div class="k">Taxa de acerto</div><div class="v" id="acerto">—</div></div>
 <div class="card"><div class="k">Melhor operação</div><div class="v pos" id="melhor">—</div></div>
 <div class="card"><div class="k">Pior operação</div><div class="v neg" id="pior">—</div></div>
 <div class="card"><div class="k">Analisados hoje</div><div class="v" id="anal">—</div></div>
 <div class="card"><div class="k">Aprovados hoje</div><div class="v" id="aprov">—</div></div>
</div>
<div class="card graf"><div class="k">Evolução do valor da carteira (USD) <span style="text-transform:none;letter-spacing:0">· tracejado = valor inicial</span></div><svg id="g" viewBox="0 0 1000 220" preserveAspectRatio="none" role="img" aria-label="Gráfico da evolução do valor da carteira"></svg></div>
<div class="duas">
 <div class="card"><div class="k">Posição aberta</div><div id="posa"><div class="vazio">—</div></div></div>
 <div class="card"><div class="k">Resumo</div><div id="res"></div></div>
</div>
<div class="card"><div class="k">Últimas operações</div><div class="tw"><table><thead><tr><th>Token</th><th>Saída</th><th class="n">Resultado</th><th class="n">USD</th><th>Motivo</th><th class="n">Duração</th></tr></thead><tbody id="tb"></tbody></table></div></div>
<footer>Atualiza sozinho a cada 10 segundos · <span id="atu"></span></footer>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd=v=>v==null?'—':(v<0?'-':'')+'$'+Math.abs(v).toFixed(2);
const pct=v=>v==null?'—':(v>0?'+':'')+v.toFixed(1)+'%';
const cls=v=>v==null?'':(v>=0?'pos':'neg');
const dex=m=>/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(m||'')?'https://dexscreener.com/solana/'+m:null;
function suavizar(xs,ys,maxN){const n=xs.length;if(n<=maxN)return[xs,ys];const ox=[xs[0]],oy=[ys[0]],passo=(n-2)/(maxN-2);
 for(let k=0;k<maxN-2;k++){const i0=1+Math.floor(k*passo),i1=Math.max(i0+1,1+Math.floor((k+1)*passo));let sx=0,sy=0,c=0;for(let i=i0;i<i1&&i<n-1;i++){sx+=xs[i];sy+=ys[i];c++}if(c){ox.push(sx/c);oy.push(sy/c)}}
 ox.push(xs[n-1]);oy.push(ys[n-1]);return[ox,oy]}
function cometa(xs,ys){const n=xs.length,f=v=>v.toFixed(1);let d='M'+f(xs[0])+' '+f(ys[0]);if(n<3){for(let i=1;i<n;i++)d+=' L'+f(xs[i])+' '+f(ys[i]);return d}
 for(let i=0;i<n-1;i++){const a=Math.max(0,i-1),b=Math.min(n-1,i+2);d+=' C'+f(xs[i]+(xs[i+1]-xs[a])/6)+' '+f(ys[i]+(ys[i+1]-ys[a])/6)+' '+f(xs[i+1]-(xs[b]-xs[i])/6)+' '+f(ys[i+1]-(ys[b]-ys[i])/6)+' '+f(xs[i+1])+' '+f(ys[i+1])}return d}
let ULT_G=null;
function grafico(h,ini){const s=$('g');ULT_G=[h,ini];if(!h||h.length<2){s.innerHTML='';if(!s.nextElementSibling||!s.nextElementSibling.classList.contains('vazio'))s.insertAdjacentHTML('afterend','<div class="vazio">Ainda sem dados suficientes</div>');return}
 // linha do cometa (igual ao dashboard): suavizada, verde acima do inicial, vermelha abaixo
 const xs=h.map(p=>p[0]),ys=h.map(p=>p[1]).concat([ini]),x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys),py=(y1-y0)*.12||1;
 const sx=1000/(s.clientWidth||1000),sy=220/(s.clientHeight||220),X=t=>6+(t-x0)/((x1-x0)||1)*(982-12*sx),Y=v=>205-(v-(y0-py))/((y1+py)-(y0-py))*190,[XS,YS]=suavizar(h.map(p=>X(p[0])),h.map(p=>Y(p[1])),Math.max(24,Math.min(90,Math.round((s.clientWidth||1000)/12))));
 const d=cometa(XS,YS),yI=Y(ini).toFixed(1),xu=XS[XS.length-1],yu=YS[YS.length-1],up=h[h.length-1][1]>=ini,c=up?'46,230,168':'255,107,107';
 const yMin=Math.min(...YS).toFixed(1),yMax=Math.max(...YS).toFixed(1),fe=' L'+xu.toFixed(1)+' '+yI+' L'+XS[0].toFixed(1)+' '+yI+' Z';
 s.innerHTML='<defs><linearGradient id="gv" x1="0" y1="'+yMin+'" x2="0" y2="'+yI+'" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#2ee6a8" stop-opacity=".30"/><stop offset="1" stop-color="#2ee6a8" stop-opacity=".02"/></linearGradient>'+
 '<linearGradient id="gr" x1="0" y1="'+yMax+'" x2="0" y2="'+yI+'" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#ff6b6b" stop-opacity=".30"/><stop offset="1" stop-color="#ff6b6b" stop-opacity=".02"/></linearGradient>'+
 '<linearGradient id="gc" x1="'+XS[0].toFixed(1)+'" y1="0" x2="'+xu.toFixed(1)+'" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="rgb('+c+')" stop-opacity=".2"/><stop offset=".6" stop-color="rgb('+c+')" stop-opacity=".65"/><stop offset="1" stop-color="rgb('+c+')"/></linearGradient>'+
 '<radialGradient id="gh"><stop offset="0" stop-color="#fff" stop-opacity=".95"/><stop offset=".35" stop-color="rgb('+c+')" stop-opacity=".7"/><stop offset="1" stop-color="rgb('+c+')" stop-opacity="0"/></radialGradient>'+
 '<clipPath id="cv"><rect x="0" y="0" width="1000" height="'+yI+'"/></clipPath><clipPath id="cr"><rect x="0" y="'+yI+'" width="1000" height="220"/></clipPath>'+
 '</defs>'+
 '<path d="'+d+fe+'" fill="url(#gv)" clip-path="url(#cv)"/><path d="'+d+fe+'" fill="url(#gr)" clip-path="url(#cr)"/>'+
 '<line x1="0" x2="1000" y1="'+yI+'" y2="'+yI+'" stroke="#7f9896" stroke-dasharray="4 6" stroke-width="1" vector-effect="non-scaling-stroke"/>'+
 '<path d="'+d+'" fill="none" stroke="rgb('+c+')" stroke-opacity=".12" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'+
 '<path d="'+d+'" fill="none" stroke="url(#gc)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke" style="filter:drop-shadow(0 0 5px rgb('+c+'))"/>'+
 '<ellipse cx="'+xu.toFixed(1)+'" cy="'+yu.toFixed(1)+'" rx="'+(11*sx).toFixed(1)+'" ry="'+(11*sy).toFixed(1)+'" fill="url(#gh)"/>'+
 '<ellipse cx="'+xu.toFixed(1)+'" cy="'+yu.toFixed(1)+'" rx="'+(2.2*sx).toFixed(2)+'" ry="'+(2.2*sy).toFixed(2)+'" fill="#fff"/>'}
window.addEventListener('resize',()=>{if(ULT_G)grafico(ULT_G[0],ULT_G[1])});
async function atualizar(){try{const r=await fetch('/dados.json',{cache:'no-store'});if(!r.ok)throw 0;const d=await r.json();
 $('est').classList.toggle('on',!!d.ativo);$('est-t').textContent=d.ativo?'Bot ativo':'Bot sem atividade recente';
 $('total').textContent=usd(d.total);$('pnl').innerHTML='<span class="'+cls(d.pnl_usd)+'">'+usd(d.pnl_usd)+' ('+pct(d.pnl_pct)+')</span> desde '+esc(d.inicio)+' · início $'+d.saldo_inicial.toFixed(2);
 $('fech').innerHTML=d.fechados+' <small>'+d.ganhos+' ganhas · '+d.perdas+' perdidas</small>';
 $('acerto').textContent=d.acerto==null?'—':d.acerto.toFixed(0)+'%';$('melhor').textContent=pct(d.melhor);$('pior').textContent=pct(d.pior);
 $('anal').textContent=d.hoje.analisados.toLocaleString('pt-PT');$('aprov').textContent=d.hoje.aprovados;
 grafico(d.historico,d.saldo_inicial);
 $('posa').innerHTML=d.posicoes.length?d.posicoes.map(p=>'<div class="lin"><span>'+(dex(p.mint)?'<a href="'+dex(p.mint)+'" target="_blank" rel="noopener">$'+esc(p.simbolo)+'</a>':'$'+esc(p.simbolo))+' <span class="tag">desde '+esc(p.desde)+'</span></span><b class="'+cls(p.var_pct)+'">'+pct(p.var_pct)+'</b></div>').join(''):'<div class="vazio">Nenhuma posição aberta. O bot está à procura de oportunidades.</div>';
 $('res').innerHTML=[['Tokens recebidos hoje',d.hoje.recebidos.toLocaleString('pt-PT')],['Rejeitados hoje',d.hoje.rejeitados.toLocaleString('pt-PT')],['Resultado total',usd(d.pnl_usd)],['Última atividade',d.ultimo_ciclo?d.ultimo_ciclo.slice(11):'—']].map(([a,b])=>'<div class="lin"><span>'+a+'</span><b>'+esc(b)+'</b></div>').join('');
 $('tb').innerHTML=d.vendas.length?d.vendas.map(v=>'<tr><td>'+(dex(v.mint)?'<a href="'+dex(v.mint)+'" target="_blank" rel="noopener">$'+esc(v.simbolo)+'</a>':'$'+esc(v.simbolo))+'</td><td>'+esc(v.saida.slice(5))+'</td><td class="n '+cls(v.pnl_pct)+'">'+pct(v.pnl_pct)+'</td><td class="n '+cls(v.pnl_usd)+'">'+usd(v.pnl_usd)+'</td><td><span class="tag">'+esc(v.motivo)+'</span></td><td class="n">'+(v.duracao_min==null?'—':Math.round(v.duracao_min)+' min')+'</td></tr>').join(''):'<tr><td colspan="6" class="vazio">Ainda sem operações fechadas.</td></tr>';
 $('atu').textContent='dados de '+d.atualizado;
}catch(e){$('est').classList.remove('on');$('est-t').textContent='Sem ligação'}}
atualizar();setInterval(atualizar,10000);
</script>
</body>
</html>"""


class Servidor(ThreadingHTTPServer):
    """Servidor publico com um teto de ligacoes em simultaneo.

    A pagina corre na mesma maquina do bot: sem teto, muitas ligacoes lentas vindas da internet
    podiam gastar a memoria de que o bot precisa. Acima do teto, a ligacao e fechada logo."""
    daemon_threads = True
    request_queue_size = 32
    max_ligacoes = 48

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vagas = threading.BoundedSemaphore(self.max_ligacoes)

    def process_request(self, request, client_address):
        if not self._vagas.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._vagas.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._vagas.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # o visitante desligou a meio: nao enche o registo do servico
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "MemeBotPublico/1.0"
    sys_version = ""
    pasta = "."
    timeout = 15  # larga ligacoes paradas ou lentas (sem isto ficavam abertas para sempre)

    def _enviar(self, codigo, corpo, tipo, csp=None):
        b = corpo if isinstance(corpo, bytes) else corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp or
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                         "frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def do_GET(self):
        caminho = self.path.split("?", 1)[0]
        if caminho in ("/", "/index.html"):
            html = pagina_neural(self.pasta)
            if html is None:
                return self._enviar(200, PAGINA, "text/html; charset=utf-8")
            return self._enviar(200, html, "text/html; charset=utf-8", csp=(
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
                "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: https:; "
                "connect-src 'self'; frame-src https://dexscreener.com; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"))
        if caminho == "/resultados":
            return self._enviar(200, PAGINA, "text/html; charset=utf-8")
        if caminho == "/api/estado":
            e = estado_publico()
            if e is None:
                return self._enviar(503, '{"erro":"bot indisponivel"}', "application/json")
            return self._enviar(200, e, "application/json; charset=utf-8")
        # Lista fechada de imagens publicas: nao expor outros ficheiros de assets.
        imagens_publicas = {
            "/assets/mascote_oficial.png": "mascote_oficial.png",
            "/assets/obito-header.png": "obito-header.png",
            "/assets/obito-arena.png": "obito-arena.png",
        }
        if caminho in imagens_publicas:
            try:
                nome = imagens_publicas[caminho]
                with open(os.path.join(self.pasta, "assets", nome), "rb") as f:
                    return self._enviar(200, f.read(8_000_000), "image/png")
            except OSError:
                return self._enviar(404, "nao encontrado", "text/plain; charset=utf-8")
        if caminho == "/dados.json":
            d = dados(self.pasta)
            if d is None:
                return self._enviar(503, '{"erro":"sem dados"}', "application/json")
            return self._enviar(200, json.dumps(d, ensure_ascii=False), "application/json; charset=utf-8")
        if caminho == "/saude":
            return self._enviar(200, "ok", "text/plain; charset=utf-8")
        return self._enviar(404, "nao encontrado", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self):
        self._enviar(405, "so leitura", "text/plain; charset=utf-8")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args):  # sem registos de IPs dos visitantes
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pagina publica so de leitura do MemeBot")
    ap.add_argument("--porta", type=int, default=8080)
    ap.add_argument("--pasta", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args(argv)
    Handler.pasta = a.pasta
    srv = Servidor(("0.0.0.0", a.porta), Handler)
    print(f"Pagina publica em http://0.0.0.0:{a.porta}  (pasta {a.pasta})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
