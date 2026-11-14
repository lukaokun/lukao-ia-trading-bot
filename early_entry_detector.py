# -*- coding: utf-8 -*-
"""Cruzamento conservador de acumulação + fluxo Smart Money + filtros de risco."""
import math
from early_accumulation import score_acumulacao


def score_early_entry(pair, smart_flow=None, risk=None,
                      min_liquidity_usd=10000,
                      min_swaps=5,
                      min_smart_wallets=1):
    """Produz um sinal de observação, nunca uma ordem de compra.

    smart_flow é o resultado por mint de score_smart_money_flow().
    risk pode conter: rugcheck_ok (bool|None), mint_authority_revoked,
    freeze_authority_revoked, suspicious (bool), slippage_pct (número).
    """
    smart_flow = smart_flow or {}
    risk = risk or {}
    accum = score_acumulacao(pair)
    try:
        liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    except (AttributeError, TypeError, ValueError):
        liquidity = 0.0

    reasons = []
    blockers = []
    if liquidity < float(min_liquidity_usd):
        blockers.append("liquidez abaixo do mínimo configurado")
    if risk.get("suspicious") is True:
        blockers.append("sinal de risco suspeito")
    # Missing safety data is UNKNOWN, not a pass. This detector is conservative.
    if risk.get("rugcheck_ok") is False:
        blockers.append("verificação de risco reprovada")
    elif risk.get("rugcheck_ok") is not True:
        reasons.append("verificação de risco ainda não confirmada")
    if risk.get("mint_authority_revoked") is False:
        blockers.append("autoridade de mint ativa")
    elif risk.get("mint_authority_revoked") is not True:
        reasons.append("estado da autoridade de mint desconhecido")
    if risk.get("freeze_authority_revoked") is False:
        blockers.append("autoridade de freeze ativa")
    elif risk.get("freeze_authority_revoked") is not True:
        reasons.append("estado da autoridade de freeze desconhecido")
    try:
        if risk.get("slippage_pct") is None:
            raise TypeError("slippage ausente")
        slip = float(risk.get("slippage_pct"))
        if not math.isfinite(slip) or slip < 0:
            raise ValueError("slippage inválido")
    except TypeError:
        slip = 0.0
        reasons.append("slippage ainda não estimado")
    except ValueError:
        slip = 100.0
        blockers.append("slippage inválido")
    if slip > 5:
        blockers.append("slippage estimado acima de 5%")

    try:
        swaps = int(smart_flow.get("observed_swaps", 0) or 0)
        smart_wallets = int(smart_flow.get("historically_profitable_buyers", 0) or 0)
        smart_score = float(smart_flow.get("score", 0) or 0)
        if swaps < 0 or smart_wallets < 0 or not math.isfinite(smart_score):
            raise ValueError("métricas Smart Money inválidas")
    except (TypeError, ValueError, OverflowError):
        swaps, smart_wallets, smart_score = 0, 0, 0.0
        blockers.append("métricas Smart Money inválidas")
    if swaps < int(min_swaps):
        reasons.append("amostra de swaps insuficiente")
    if smart_wallets < int(min_smart_wallets):
        reasons.append("sem compradores Smart Money qualificados suficientes")
    if accum["score"] < 5:
        reasons.append("pressão agregada de acumulação fraca")

    # Weighted observation score. Missing smart-money sample does not get
    # fabricated into a positive score.
    score = round(0.55 * accum["score"] + 0.45 * min(10, max(0, smart_score)), 1)
    ready = (
        not blockers and not reasons and
        accum["score"] >= 5 and smart_score >= 4
    )
    if blockers:
        status = "bloqueado"
    elif ready:
        status = "candidato_para_revisao"
    else:
        status = "observar"
    return {
        "status": status,
        "score": score,
        "accumulation_score": accum["score"],
        "smart_money_score": round(smart_score, 1),
        "liquidity_usd": round(liquidity, 2),
        "observed_swaps": swaps,
        "qualified_smart_wallets": smart_wallets,
        "reasons": reasons,
        "blockers": blockers,
        "action": "nenhuma_ordem_paper_trading_ou_revisao_manual",
        "warning": "Sinal experimental; nao e recomendacao nem previsao de lucro."
    }
