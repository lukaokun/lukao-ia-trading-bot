# -*- coding: utf-8 -*-
"""Detector heuristico de acumulação inicial para dados agregados da DexScreener.

Não identifica carteiras individuais nem prova compras de smart money.
Usa apenas métricas agregadas de preço, volume e transações do par.
"""
from __future__ import annotations

def score_acumulacao(par):
    def n(v):
        try:
            x = float(v)
            return x if x == x and abs(x) != float("inf") else 0.0
        except (TypeError, ValueError):
            return 0.0

    def get(obj, *keys):
        for k in keys:
            if not isinstance(obj, dict):
                return 0.0
            obj = obj.get(k)
        return n(obj)

    liq = get(par, "liquidity", "usd")
    vol5 = get(par, "volume", "m5")
    buys5 = get(par, "txns", "m5", "buys")
    sells5 = get(par, "txns", "m5", "sells")
    buys1 = get(par, "txns", "h1", "buys")
    sells1 = get(par, "txns", "h1", "sells")
    ch5 = get(par, "priceChange", "m5")
    ch1 = get(par, "priceChange", "h1")

    # Dados ausentes ou pools sem liquidez não recebem pontuação artificial.
    if liq <= 0:
        return {"score": 0, "label": "dados insuficientes", "details": {}}

    total5 = buys5 + sells5
    total1 = buys1 + sells1
    ratio5 = buys5 / total5 if total5 else 0.5
    ratio1 = buys1 / total1 if total1 else 0.5

    # Componentes 0-10: pressão compradora, aceleração de atividade,
    # volume relativo à liquidez e preço ainda não excessivamente esticado.
    pressure = 4.0 * max(0.0, min(1.0, (ratio5 - 0.45) / 0.30))
    pressure += 2.0 * max(0.0, min(1.0, (ratio1 - 0.45) / 0.30))
    activity = 2.0 * max(0.0, min(1.0, total5 / 30.0))
    volume = 1.0 * max(0.0, min(1.0, (vol5 / liq) / 0.15))
    # Pequena subida é aceitável; pump rápido é penalizado.
    price = 1.0 if 0 <= ch5 <= 8 and -5 <= ch1 <= 35 else (
        0.5 if -3 <= ch5 <= 12 and -10 <= ch1 <= 50 else 0.0)
    score = round(max(0.0, min(10.0, pressure + activity + volume + price)), 1)
    label = "forte" if score >= 7.5 else "moderada" if score >= 5.0 else "fraca"
    return {
        "score": score,
        "label": label,
        "details": {
            "buy_ratio_m5": round(ratio5, 3),
            "buy_ratio_h1": round(ratio1, 3),
            "txns_m5": int(total5),
            "volume_liquidity_m5": round(vol5 / liq, 4),
            "price_change_m5": ch5,
            "price_change_h1": ch1,
        },
    }
