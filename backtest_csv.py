# -*- coding: utf-8 -*-
"""Backtest simples por snapshots CSV. Não envia ordens nem usa rede."""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime

REQUIRED = {"timestamp", "mint", "price_usd"}

def _number(value, field):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field}: valor numérico inválido: {value!r}")
    if not math.isfinite(n) or n <= 0:
        raise ValueError(f"{field}: valor tem de ser finito e > 0")
    return n

def load_rows(path):
    grouped = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        missing = REQUIRED - fields
        if missing:
            raise ValueError("CSV sem colunas obrigatórias: " + ", ".join(sorted(missing)))
        for line, row in enumerate(reader, start=2):
            mint = (row.get("mint") or "").strip()
            if not mint:
                raise ValueError(f"linha {line}: mint vazio")
            ts = (row.get("timestamp") or "").strip()
            # ISO-8601 or Unix seconds.
            try:
                stamp = float(ts)
                if not math.isfinite(stamp) or stamp < 0:
                    raise ValueError()
            except ValueError:
                try:
                    stamp = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    raise ValueError(f"linha {line}: timestamp inválido")
            price = _number(row.get("price_usd"), "price_usd")
            grouped[mint].append((stamp, price))
    for mint in grouped:
        grouped[mint].sort(key=lambda x: x[0])
    return grouped

def run_backtest(data, *, initial_cash=1000.0, fee_pct=0.001,
                 slippage_pct=0.005, take_profit_pct=0.30,
                 stop_loss_pct=0.15, max_hold_bars=24):
    params = [initial_cash, fee_pct, slippage_pct, take_profit_pct, stop_loss_pct]
    if not all(math.isfinite(float(x)) for x in params):
        raise ValueError("Parâmetros têm de ser finitos")
    if initial_cash <= 0 or not (0 <= fee_pct < 1) or not (0 <= slippage_pct < 1):
        raise ValueError("Capital, taxas ou slippage inválidos")
    if take_profit_pct <= 0 or not (0 < stop_loss_pct < 1) or int(max_hold_bars) < 1:
        raise ValueError("Parâmetros de saída inválidos")

    trades = []
    # Independent paper position per mint, with equal allocation of starting capital.
    mints = sorted(data)
    allocation = initial_cash / max(1, len(mints))
    for mint in mints:
        rows = data[mint]
        if len(rows) < 2:
            continue
        entry = None
        for i, (ts, price) in enumerate(rows):
            if entry is None:
                # Entry only at observed bar i; no future data is used to decide entry.
                entry = {"index": i, "ts": ts, "price": price * (1 + slippage_pct),
                         "qty": allocation * (1 - fee_pct) / (price * (1 + slippage_pct))}
                continue
            gross = (price / entry["price"]) - 1
            held = i - entry["index"]
            reason = None
            if gross >= take_profit_pct:
                reason = "take_profit"
            elif gross <= -stop_loss_pct:
                reason = "stop_loss"
            elif held >= int(max_hold_bars):
                reason = "max_hold"
            elif i == len(rows) - 1:
                reason = "fim_dados"
            if reason:
                exit_price = price * (1 - slippage_pct)
                proceeds = entry["qty"] * exit_price * (1 - fee_pct)
                invested = allocation
                pnl = proceeds - invested
                trades.append({
                    "mint": mint, "entry_timestamp": entry["ts"],
                    "exit_timestamp": ts, "entry_price": round(entry["price"], 12),
                    "exit_price": round(exit_price, 12), "pnl_usd": round(pnl, 6),
                    "return_pct": round(100 * pnl / invested, 4),
                    "bars_held": held, "exit_reason": reason
                })
                entry = None
                break
    pnl = sum(t["pnl_usd"] for t in trades)
    wins = sum(t["pnl_usd"] > 0 for t in trades)
    losses = sum(t["pnl_usd"] < 0 for t in trades)
    return {
        "mode": "paper_backtest_csv", "trades": trades,
        "summary": {
            "mints_in_csv": len(data), "trades": len(trades),
            "initial_cash_usd": round(float(initial_cash), 2),
            "net_pnl_usd": round(pnl, 6),
            "final_cash_usd": round(float(initial_cash) + pnl, 6),
            "return_pct": round(100 * pnl / float(initial_cash), 4),
            "wins": wins, "losses": losses,
            "win_rate_pct": round(100 * wins / len(trades), 2) if trades else None,
            "fees_and_slippage_included": True,
            "warning": "Backtest ilustrativo; snapshots CSV não garantem execução real nem ausência de viés."
        }
    }

def main():
    ap = argparse.ArgumentParser(description="Backtest offline de snapshots CSV (paper only)")
    ap.add_argument("csv_file")
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--fee", type=float, default=0.001, help="taxa por lado, ex. 0.001 = 0,1%")
    ap.add_argument("--slippage", type=float, default=0.005, help="slippage por lado, ex. 0.005 = 0,5%")
    ap.add_argument("--take-profit", type=float, default=0.30)
    ap.add_argument("--stop-loss", type=float, default=0.15)
    ap.add_argument("--max-hold-bars", type=int, default=24)
    ap.add_argument("--out", default="backtest_resultado.json")
    args = ap.parse_args()
    result = run_backtest(load_rows(args.csv_file), initial_cash=args.capital,
                          fee_pct=args.fee, slippage_pct=args.slippage,
                          take_profit_pct=args.take_profit,
                          stop_loss_pct=args.stop_loss,
                          max_hold_bars=args.max_hold_bars)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Relatório guardado em: {args.out}")

if __name__ == "__main__":
    main()
