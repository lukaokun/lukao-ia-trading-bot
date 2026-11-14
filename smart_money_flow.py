# -*- coding: utf-8 -*-
"""Sinais de fluxo Smart Money a partir de swaps já classificados.

Este módulo NÃO recolhe transações nem identifica insiders. Recebe eventos
confirmados e classificados por uma fonte upstream (ex.: decoder Solana).
"""
from collections import defaultdict
import math


def score_smart_money_flow(events, wallet_reports=None, window_seconds=3600,
                           now=None, min_wallet_trades=5):
    """Calcula um sinal conservador por mint.

    Evento esperado: {wallet, mint, side ('buy'|'sell'), quote_usd,
    timestamp, signature}. Duplicados por signature são ignorados.
    wallet_reports: dict wallet -> wallet_report() do SmartWalletTracker.
    """
    import time
    now = time.time() if now is None else float(now)
    reports = wallet_reports or {}
    grouped = defaultdict(list)
    seen = set()
    for e in events or []:
        try:
            sig = str(e.get("signature", ""))
            wallet, mint = str(e["wallet"]), str(e["mint"])
            side = str(e["side"]).lower()
            value = float(e["quote_usd"])
            ts = float(e["timestamp"])
            event_key = (sig, wallet, mint, side)
            if not sig or event_key in seen or not wallet or not mint:
                continue
            if side not in ("buy", "sell") or not math.isfinite(value) or value <= 0:
                continue
            if not math.isfinite(ts) or ts > now + 60 or now - ts > window_seconds:
                continue
            seen.add(event_key)
            grouped[mint].append((wallet, side, value, ts))
        except (KeyError, TypeError, ValueError):
            continue

    result = {}
    for mint, rows in grouped.items():
        buys = [r for r in rows if r[1] == "buy"]
        sells = [r for r in rows if r[1] == "sell"]
        buy_usd = sum(r[2] for r in buys)
        sell_usd = sum(r[2] for r in sells)
        net = buy_usd - sell_usd
        buyers = {r[0] for r in buys}
        sellers = {r[0] for r in sells}
        qualified = set()
        for wallet in buyers:
            report = reports.get(wallet, {})
            if (report.get("amostra_suficiente") and
                report.get("lotes_fechados", 0) >= min_wallet_trades and
                report.get("pnl_realizado_usd_fifo", 0) > 0):
                qualified.add(wallet)
        # Score 0-10. No score if there is no net buying pressure.
        breadth = min(3.0, len(buyers) / 5.0 * 3.0)
        net_pressure = min(4.0, max(0.0, net / max(buy_usd + sell_usd, 1.0)) * 4.0)
        smart = min(3.0, len(qualified) * 1.0)
        score = round(breadth + net_pressure + smart, 1)
        result[mint] = {
            "score": score,
            "label": "forte" if score >= 7 else "moderada" if score >= 4 else "fraca",
            "buyers": len(buyers), "sellers": len(sellers),
            "buy_usd": round(buy_usd, 2), "sell_usd": round(sell_usd, 2),
            "net_buy_usd": round(net, 2),
            "historically_profitable_buyers": len(qualified),
            "observed_swaps": len(rows),
            "warning": "Sinal experimental; qualidade depende da cobertura e classificacao dos swaps."
        }
    return result
