# -*- coding: utf-8 -*-
"""Ranking conservador de carteiras com base no ledger observado."""
import math


def rank_wallet_reports(reports, min_closed_trades=5, limit=50):
    """Ordena relatórios wallet_report() sem confundir amostra pequena com edge.

    O score 0-100 é heurístico, não é previsão de rentabilidade.
    Carteiras sem amostra mínima ficam sempre abaixo das elegíveis.
    """
    ranked = []
    for raw in reports or []:
        r = dict(raw)
        try:
            closed = max(0, int(r.get("operacoes_fechadas", r.get("lotes_fechados", 0))))
            wins = max(0, int(r.get("wins_operacoes", r.get("wins_lotes", 0))))
            losses = max(0, int(r.get("losses_operacoes", r.get("losses_lotes", 0))))
            tokens = max(0, int(r.get("tokens_distintos", 0)))
            pnl = float(r.get("pnl_realizado_usd_fifo", 0.0))
            win_rate = r.get("win_rate_lotes_pct")
            win_rate = None if win_rate is None else float(win_rate)
            open_cost = max(0.0, float(r.get("custo_posicao_aberta_usd", 0.0)))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) for x in [pnl, open_cost] + ([] if win_rate is None else [win_rate])):
            continue
        enough = closed >= int(min_closed_trades) and bool(r.get("amostra_suficiente"))
        # Components: sample confidence, win consistency, realized PnL quality,
        # and breadth. No component claims future returns.
        sample = min(1.0, closed / max(1, int(min_closed_trades) * 4))
        consistency = 0.0 if win_rate is None else max(0.0, min(1.0, (win_rate - 35.0) / 45.0))
        pnl_component = 0.5 if pnl == 0 else (1.0 if pnl > 0 else 0.0)
        breadth = min(1.0, tokens / 8.0)
        score = round(100 * (
            0.30 * sample + 0.30 * consistency +
            0.25 * pnl_component + 0.15 * breadth
        ), 1)
        if not enough:
            score = min(score, 24.9)
        elif pnl <= 0:
            score = min(score, 49.9)
        r.update({
            "ranking_score": score,
            "ranking_status": "amostra_elegivel" if enough else "amostra_insuficiente",
            "ranking_components": {
                "sample": round(sample, 3),
                "consistency": round(consistency, 3),
                "realized_pnl": round(pnl_component, 3),
                "token_breadth": round(breadth, 3),
            },
            "ranking_warning": (
                "Ranking descritivo do historico observado; FIFO incompleto se houver "
                "transferencias, airdrops, swaps nao recolhidos ou custos em falta. "
                "Nao e recomendacao nem previsao."
            )
        })
        ranked.append(r)
    ranked.sort(key=lambda x: (
        x["ranking_status"] == "amostra_elegivel",
        x["ranking_score"],
        x.get("pnl_realizado_usd_fifo", 0)
    ), reverse=True)
    return ranked[:max(1, min(200, int(limit)))]
