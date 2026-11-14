# -*- coding: utf-8 -*-
"""Executa o monitor da watchlist em ciclos lentos; apenas recolha e paper analysis."""
import argparse
import logging
import os
import time

from smart_money_pipeline import SmartMoneyPipeline
from wallet_monitor import SolanaRPC, WalletMonitor, load_watchlist


def main():
    parser = argparse.ArgumentParser(description="Monitor Solana de carteiras (sem trading)")
    parser.add_argument("--watchlist", default="wallet_watchlist.json")
    parser.add_argument("--db", default="smart_wallets.sqlite3")
    parser.add_argument("--state", default="wallet_monitor_state.json")
    parser.add_argument("--interval", type=int, default=300,
                        help="segundos entre ciclos; minimo recomendado 60")
    parser.add_argument("--max-signatures", type=int, default=20)
    parser.add_argument("--once", action="store_true",
                        help="executa um ciclo e termina")
    args = parser.parse_args()
    if args.interval < 60 and not args.once:
        parser.error("--interval deve ser >= 60 segundos para evitar excesso de pedidos")
    wallets = load_watchlist(args.watchlist)
    if not wallets:
        parser.error("A watchlist está vazia.")
    if any("SUBSTITUIR" in w for w in wallets):
        parser.error("Substitui os endereços de exemplo por endereços Solana reais.")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    pipeline = SmartMoneyPipeline(args.db)
    rpc = SolanaRPC()
    monitor = WalletMonitor(
        pipeline, rpc, args.state,
        max_signatures_per_wallet=args.max_signatures,
        pause_seconds=0.5
    )
    logging.info("A monitorizar %d carteiras; apenas leitura, sem ordens.", len(wallets))
    while True:
        results = monitor.poll_watchlist(wallets)
        for result in results:
            logging.info("%s", result)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\\nMonitor parado pelo utilizador.")
