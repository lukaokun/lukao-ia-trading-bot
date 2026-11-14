# -*- coding: utf-8 -*-
"""Exporta relatório local de desempenho observado das carteiras."""
import argparse
import csv
import json
from smart_wallet_tracker import SmartWalletTracker
from wallet_ranking import rank_wallet_reports


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="smart_wallets.sqlite3")
    ap.add_argument("--min-closed-trades",type=int,default=5)
    ap.add_argument("--limit",type=int,default=50)
    ap.add_argument("--json",default="wallet_performance_report.json")
    ap.add_argument("--csv",default="wallet_performance_report.csv")
    args=ap.parse_args()
    tracker=SmartWalletTracker(args.db)
    reports=rank_wallet_reports(
        tracker.top_wallets(limit=max(args.limit,1),
                            min_closed_trades=args.min_closed_trades),
        min_closed_trades=args.min_closed_trades,limit=args.limit)
    with open(args.json,"w",encoding="utf-8") as f:
        json.dump(reports,f,ensure_ascii=False,indent=2)
    fields=["wallet","ranking_score","ranking_status","swaps_confirmados",
            "tokens_distintos","lotes_fechados","win_rate_lotes_pct",
            "pnl_realizado_usd_fifo","posicao_aberta_tokens",
            "custo_posicao_aberta_usd"]
    with open(args.csv,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
        w.writeheader()
        w.writerows(reports)
    print(f"{len(reports)} carteiras exportadas para {args.json} e {args.csv}")
    print("PnL e ranking são estimativas do histórico recolhido, não previsões.")


if __name__=="__main__":
    main()
