# -*- coding: utf-8 -*-
"""Local Smart Wallet performance ledger.

Only records swaps explicitly classified by an upstream parser. It does not
infer buys/sells from token balance deltas or label wallets as insiders.
SQLite is local; no paid API is required.
"""
import json
import os
import sqlite3
import time
import math
import re
from contextlib import contextmanager


class SmartWalletTracker:
    def __init__(self, db_path="smart_wallets.sqlite3"):
        self.db_path = db_path
        folder = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(folder, exist_ok=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "swaps" not in tables:
                self._create_swaps_table(db, "swaps")
            else:
                info = db.execute("PRAGMA table_info(swaps)").fetchall()
                cols = {row[1]: row for row in info}
                pk_cols = [row[1] for row in sorted(info, key=lambda x: x[5]) if row[5]]
                needs_migration = (
                    pk_cols != ["signature", "wallet", "mint", "side"] or
                    any(name not in cols for name in (
                        "signature", "wallet", "mint", "ts", "side", "token_amount",
                        "quote_usd", "fee_usd",
                        "ingestion_ts", "ts_verified", "quote_mint", "quote_amount",
                        "quote_amount_raw", "quote_decimals", "fee_lamports", "fee_sol")) or
                    ("ts" in cols and cols["ts"][3] == 1) or
                    ("ingestion_ts" in cols and cols["ingestion_ts"][3] == 1) or
                    ("quote_usd" in cols and cols["quote_usd"][3] == 1) or
                    ("fee_usd" in cols and cols["fee_usd"][3] == 1)
                )
                if needs_migration:
                    self._migrate_swaps(db)
            db.execute("CREATE INDEX IF NOT EXISTS idx_swaps_wallet_mint_ts ON swaps(wallet,mint,ts)")
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                wallet TEXT NOT NULL, mint TEXT NOT NULL, ts REAL NOT NULL,
                token_balance REAL NOT NULL CHECK(token_balance >= 0),
                PRIMARY KEY(wallet,mint,ts)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_obs_wallet_ts ON observations(wallet,ts)")
            db.execute("""CREATE TABLE IF NOT EXISTS transaction_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT,
                wallet_scope TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN (
                    'obtained_decoded','obtained_unsupported','failed',
                    'unavailable','rejected_invalid')),
                raw_json TEXT,
                reason TEXT,
                rpc_endpoint TEXT,
                attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts > 0),
                first_seen_ts REAL,
                last_attempt_ts REAL NOT NULL,
                UNIQUE(signature,wallet_scope)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_audit_status ON transaction_audit(status,last_attempt_ts)")

    @staticmethod
    def _create_swaps_table(db, name):
        if name not in ("swaps", "swaps_migration_new"):
            raise ValueError("nome de tabela de migração inválido")
        db.execute(f"""CREATE TABLE {name} (
            signature TEXT NOT NULL, wallet TEXT NOT NULL, mint TEXT NOT NULL,
            ts REAL, ingestion_ts REAL,
            ts_verified INTEGER NOT NULL DEFAULT 0 CHECK(ts_verified IN (0,1)),
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            token_amount REAL NOT NULL CHECK(token_amount > 0),
            quote_usd REAL CHECK(quote_usd IS NULL OR quote_usd >= 0),
            fee_usd REAL CHECK(fee_usd IS NULL OR fee_usd >= 0),
            quote_mint TEXT, quote_amount REAL CHECK(quote_amount IS NULL OR quote_amount > 0),
            quote_amount_raw TEXT, quote_decimals INTEGER CHECK(quote_decimals IS NULL OR quote_decimals >= 0),
            fee_lamports INTEGER CHECK(fee_lamports IS NULL OR fee_lamports >= 0),
            fee_sol REAL CHECK(fee_sol IS NULL OR fee_sol >= 0),
            PRIMARY KEY(signature,wallet,mint,side)
        )""")

    @classmethod
    def _migrate_swaps(cls, db):
        # The table rebuild and row copy are part of the surrounding
        # BEGIN IMMEDIATE transaction. Missing legacy fields remain NULL;
        # old zero values are copied verbatim, never reinterpreted.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='swaps_migration_new'").fetchone():
            raise RuntimeError("tabela temporária de migração já existe; nenhuma migração aplicada")
        cls._create_swaps_table(db, "swaps_migration_new")
        rows = db.execute("SELECT * FROM swaps").fetchall()
        names = [row[1] for row in db.execute("PRAGMA table_info(swaps)")]
        data = [dict(zip(names, row)) for row in rows]
        target_names = ("signature", "wallet", "mint", "ts", "ingestion_ts", "ts_verified",
                        "side", "token_amount", "quote_usd", "fee_usd", "quote_mint",
                        "quote_amount", "quote_amount_raw", "quote_decimals",
                        "fee_lamports", "fee_sol")
        for record in data:
            values = []
            for name in target_names:
                if name in record:
                    value = record[name]
                elif name == "ts_verified":
                    value = 0
                else:
                    value = None
                values.append(value)
            db.execute(
                "INSERT INTO swaps_migration_new (" + ",".join(target_names) + ") VALUES (" +
                ",".join("?" for _ in target_names) + ")", values)
        db.execute("DROP INDEX IF EXISTS idx_swaps_wallet_mint_ts")
        db.execute("ALTER TABLE swaps RENAME TO swaps_migration_old")
        db.execute("ALTER TABLE swaps_migration_new RENAME TO swaps")
        db.execute("DROP TABLE swaps_migration_old")

    def record_transaction_status(self, *, signature=None, wallet_scope="", status,
                                  raw_tx=None, reason=None, rpc_endpoint=None):
        allowed = {"obtained_decoded", "obtained_unsupported", "failed",
                   "unavailable", "rejected_invalid"}
        if status not in allowed:
            raise ValueError("estado de auditoria inválido")
        if signature is None and status != "rejected_invalid":
            raise ValueError("signature obrigatória para este estado")
        raw_json = None if raw_tx is None else json.dumps(
            raw_tx, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        now = time.time()
        with self._db() as db:
            if signature is None:
                db.execute("""INSERT INTO transaction_audit
                    (signature,wallet_scope,status,raw_json,reason,rpc_endpoint,
                     attempts,first_seen_ts,last_attempt_ts)
                    VALUES(NULL,?,?,?,?,?,1,?,?)""",
                    (str(wallet_scope or ""), status, raw_json, reason, rpc_endpoint, now, now))
                return db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("""INSERT INTO transaction_audit
                (signature,wallet_scope,status,raw_json,reason,rpc_endpoint,
                 attempts,first_seen_ts,last_attempt_ts)
                VALUES(?,?,?,?,?,?,1,?,?)
                ON CONFLICT(signature,wallet_scope) DO UPDATE SET
                    status=CASE WHEN excluded.raw_json IS NULL AND
                        transaction_audit.raw_json IS NOT NULL
                        THEN transaction_audit.status ELSE excluded.status END,
                    raw_json=COALESCE(excluded.raw_json,transaction_audit.raw_json),
                    reason=excluded.reason,
                    rpc_endpoint=COALESCE(excluded.rpc_endpoint,transaction_audit.rpc_endpoint),
                    attempts=transaction_audit.attempts+1,
                    last_attempt_ts=excluded.last_attempt_ts""",
                (str(signature), str(wallet_scope or ""), status, raw_json,
                 reason, rpc_endpoint, now, now))
            return db.execute("""SELECT audit_id FROM transaction_audit
                WHERE signature=? AND wallet_scope=?""",
                (str(signature), str(wallet_scope or ""))).fetchone()[0]

    def get_transaction_audit(self, signature, wallet_scope=""):
        with self._db() as db:
            row = db.execute("""SELECT audit_id,signature,wallet_scope,status,raw_json,
                reason,rpc_endpoint,attempts,first_seen_ts,last_attempt_ts
                FROM transaction_audit WHERE signature=? AND wallet_scope=?""",
                (str(signature), str(wallet_scope or ""))).fetchone()
        if row is None:
            return None
        result = dict(zip(("audit_id", "signature", "wallet_scope", "status", "raw_json",
                           "reason", "rpc_endpoint", "attempts", "first_seen_ts", "last_attempt_ts"), row))
        if result["raw_json"] is not None:
            result["raw_tx"] = json.loads(result.pop("raw_json"))
        return result

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def record_swap(self, *, signature, wallet, mint, side, token_amount,
                    quote_usd=None, fee_usd=None, timestamp=None,
                    quote_mint=None, quote_amount=None, quote_amount_raw=None,
                    quote_decimals=None, fee_lamports=None, fee_sol=None):
        """Record a parser-classified swap, preserving unknown USD fields as NULL."""
        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise ValueError("side tem de ser 'buy' ou 'sell'")
        if not signature or not wallet or not mint:
            raise ValueError("signature, wallet e mint sao obrigatorios")
        try:
            token_amount = float(token_amount)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("token_amount inválido") from exc
        if not math.isfinite(token_amount) or token_amount <= 0:
            raise ValueError("token_amount tem de ser finito e > 0")
        def optional_number(value, name, *, positive=False):
            if value is None:
                return None
            try:
                converted = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{name} inválido") from exc
            if not math.isfinite(converted) or converted < 0 or (positive and converted == 0):
                raise ValueError(f"{name} inválido")
            return converted
        def optional_integer(value, name, *, positive=False):
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError(f"{name} inválido")
            if isinstance(value, int):
                converted = value
            elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
                converted = int(value)
            else:
                raise ValueError(f"{name} inválido")
            if converted < 0 or (positive and converted == 0):
                raise ValueError(f"{name} inválido")
            return converted
        quote_usd = optional_number(quote_usd, "quote_usd")
        fee_usd = optional_number(fee_usd, "fee_usd")
        quote_amount = optional_number(quote_amount, "quote_amount", positive=True)
        fee_sol = optional_number(fee_sol, "fee_sol")
        quote_amount_raw = optional_integer(quote_amount_raw, "quote_amount_raw", positive=True)
        quote_decimals = optional_integer(quote_decimals, "quote_decimals")
        fee_lamports = optional_integer(fee_lamports, "fee_lamports")
        quote_amount_raw = None if quote_amount_raw is None else str(quote_amount_raw)
        ingested_at = time.time()
        ts = None if timestamp is None else float(timestamp)
        if ts is not None and (not math.isfinite(ts) or ts < 0):
            raise ValueError("timestamp inválido")
        with self._db() as db:
            cur = db.execute("""INSERT INTO swaps
                (signature,wallet,mint,ts,ingestion_ts,ts_verified,side,token_amount,
                 quote_usd,fee_usd,quote_mint,quote_amount,quote_amount_raw,
                 quote_decimals,fee_lamports,fee_sol)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(signature,wallet,mint,side) DO UPDATE SET
                    ts=CASE WHEN swaps.ts_verified=0 AND excluded.ts_verified=1 THEN excluded.ts ELSE swaps.ts END,
                    ingestion_ts=COALESCE(swaps.ingestion_ts,excluded.ingestion_ts),
                    ts_verified=MAX(swaps.ts_verified,excluded.ts_verified),
                    quote_usd=COALESCE(swaps.quote_usd,excluded.quote_usd),
                    fee_usd=COALESCE(swaps.fee_usd,excluded.fee_usd),
                    quote_mint=COALESCE(swaps.quote_mint,excluded.quote_mint),
                    quote_amount=COALESCE(swaps.quote_amount,excluded.quote_amount),
                    quote_amount_raw=COALESCE(swaps.quote_amount_raw,excluded.quote_amount_raw),
                    quote_decimals=COALESCE(swaps.quote_decimals,excluded.quote_decimals),
                    fee_lamports=COALESCE(swaps.fee_lamports,excluded.fee_lamports),
                    fee_sol=COALESCE(swaps.fee_sol,excluded.fee_sol)
                WHERE (swaps.ts_verified=0 AND excluded.ts_verified=1)
                   OR (swaps.quote_usd IS NULL AND excluded.quote_usd IS NOT NULL)
                   OR (swaps.fee_usd IS NULL AND excluded.fee_usd IS NOT NULL)
                   OR (swaps.quote_mint IS NULL AND excluded.quote_mint IS NOT NULL)
                   OR (swaps.quote_amount IS NULL AND excluded.quote_amount IS NOT NULL)
                   OR (swaps.quote_amount_raw IS NULL AND excluded.quote_amount_raw IS NOT NULL)
                   OR (swaps.quote_decimals IS NULL AND excluded.quote_decimals IS NOT NULL)
                   OR (swaps.fee_lamports IS NULL AND excluded.fee_lamports IS NOT NULL)
                   OR (swaps.fee_sol IS NULL AND excluded.fee_sol IS NOT NULL)""",
                (str(signature), str(wallet), str(mint), ts, ingested_at,
                 int(ts is not None), side, token_amount, quote_usd, fee_usd,
                 quote_mint, quote_amount, quote_amount_raw, quote_decimals,
                 fee_lamports, fee_sol))
            return cur.rowcount == 1

    def record_balance(self, wallet, mint, token_balance, timestamp=None):
        bal = float(token_balance)
        if not math.isfinite(bal) or bal < 0:
            raise ValueError("saldo tem de ser finito e não negativo")
        ts = time.time() if timestamp is None else float(timestamp)
        if not math.isfinite(ts) or ts < 0:
            raise ValueError("timestamp inválido")
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?)",
                       (str(wallet), str(mint), ts, bal))

    def _fifo_metrics(self, rows):
        # rows chronological: mint, side, qty, quote_usd, fee_usd. Unknown
        # costs remain unknown lots and can never be converted to zero.
        lots_by_mint = {}
        realized = 0.0
        wins = losses = closed = calculable = unknown_return = recorded_sells = 0
        for mint, side, qty, quote, fee in rows:
            qty = float(qty)
            quote = None if quote is None else float(quote)
            fee = None if fee is None else float(fee)
            lots = lots_by_mint.setdefault(mint, [])
            if side == "buy":
                cost = None if quote is None or fee is None else quote + fee
                lots.append([qty, cost])
                continue
            recorded_sells += 1
            qty_left = qty
            original_sell_qty = qty
            sale_cost = 0.0
            matched = 0.0
            cost_known = True
            while qty_left > 1e-12 and lots:
                lot_qty, lot_cost = lots[0]
                take = min(qty_left, lot_qty)
                if lot_cost is None:
                    cost_known = False
                else:
                    sale_cost += lot_cost * (take / lot_qty)
                matched += take
                lots[0][0] -= take
                if lot_cost is not None:
                    lots[0][1] -= lot_cost * (take / lot_qty)
                qty_left -= take
                if lots[0][0] <= 1e-12:
                    lots.pop(0)
            if matched > 1e-12:
                closed += 1
            fully_matched = qty_left <= 1e-12
            if not fully_matched or not cost_known or quote is None or fee is None:
                unknown_return += 1
                continue
            sale_pnl = quote - fee - sale_cost
            calculable += 1
            realized += sale_pnl
            if sale_pnl > 1e-12:
                wins += 1
            elif sale_pnl < -1e-12:
                losses += 1
        open_qty = sum(lot[0] for lots in lots_by_mint.values() for lot in lots)
        unknown_open_qty = sum(lot[0] for lots in lots_by_mint.values()
                               for lot in lots if lot[1] is None)
        known_open_cost = sum(lot[1] for lots in lots_by_mint.values()
                              for lot in lots if lot[1] is not None)
        open_cost = None if unknown_open_qty > 1e-12 else known_open_cost
        return (realized, wins, losses, closed, calculable, open_qty, open_cost,
                unknown_return, recorded_sells, known_open_cost, unknown_open_qty)

    def wallet_report(self, wallet, min_closed_trades=5):
        min_closed_trades = int(min_closed_trades)
        if min_closed_trades < 1:
            raise ValueError("min_closed_trades tem de ser >= 1")
        with self._db() as db:
            rows = db.execute("""SELECT mint,side,token_amount,quote_usd,fee_usd
                FROM swaps WHERE wallet=? AND ts IS NOT NULL AND ts_verified=1
                ORDER BY ts,signature""", (str(wallet),)).fetchall()
            unknown_time = db.execute("SELECT COUNT(*) FROM swaps WHERE wallet=? AND (ts IS NULL OR ts_verified=0)",
                                      (str(wallet),)).fetchone()[0]
            total_swaps = db.execute("SELECT COUNT(*) FROM swaps WHERE wallet=? AND ts IS NOT NULL AND ts_verified=1",
                                     (str(wallet),)).fetchone()[0]
            total_recorded = db.execute("SELECT COUNT(*) FROM swaps WHERE wallet=?",
                                        (str(wallet),)).fetchone()[0]
            no_price = db.execute("SELECT COUNT(*) FROM swaps WHERE wallet=? AND quote_usd IS NULL",
                                  (str(wallet),)).fetchone()[0]
            no_fee = db.execute("SELECT COUNT(*) FROM swaps WHERE wallet=? AND fee_usd IS NULL",
                                (str(wallet),)).fetchone()[0]
            priced_rows = db.execute("""SELECT COUNT(*) FROM swaps WHERE wallet=?
                AND ts IS NOT NULL AND ts_verified=1 AND quote_usd IS NOT NULL AND fee_usd IS NOT NULL""",
                                     (str(wallet),)).fetchone()[0]
            tokens = db.execute("SELECT COUNT(DISTINCT mint) FROM swaps WHERE wallet=? AND ts IS NOT NULL AND ts_verified=1",
                                (str(wallet),)).fetchone()[0]
            obs = db.execute("SELECT COUNT(*) FROM observations WHERE wallet=?",
                             (str(wallet),)).fetchone()[0]
        (pnl, wins, losses, closed, calculable, open_qty, open_cost, unknown_return,
         recorded_sells, known_open_cost, unknown_open_qty) = self._fifo_metrics(rows)
        denom = wins + losses
        return {
            "wallet": str(wallet), "swaps_registados": int(total_recorded),
            "swaps_confirmados": int(total_swaps),
            "swaps_sem_data_transacao": int(unknown_time),
            "swaps_sem_preco_usd": int(no_price),
            "swaps_sem_taxa_usd": int(no_fee),
            "swaps_avaliaveis_fifo": int(priced_rows),
            "tokens_distintos": int(tokens), "observacoes_saldo": int(obs),
            "lotes_fechados": closed, "wins_lotes": wins, "losses_lotes": losses,
            "operacoes_registadas": recorded_sells,
            "operacoes_fechadas": closed,
            "operacoes_rentabilidade_calculavel": calculable,
            "operacoes_rentabilidade_desconhecida": unknown_return,
            "wins_operacoes": wins,
            "losses_operacoes": losses,
            "win_rate_lotes_pct": round(100 * wins / denom, 2) if denom else None,
            "pnl_realizado_usd_fifo": round(pnl, 6),
            "posicao_aberta_tokens": round(open_qty, 9),
            "custo_posicao_aberta_usd": None if open_cost is None else round(open_cost, 6),
            "custo_conhecido_posicao_aberta_usd": round(known_open_cost, 6),
            "posicao_aberta_tokens_custo_desconhecido": round(unknown_open_qty, 9),
            "amostra_suficiente": calculable >= int(min_closed_trades),
            "classificacao": ("historico_insuficiente" if calculable < int(min_closed_trades)
                              else "historico_observado"),
            "nota": ("Estimativa FIFO apenas para swaps confirmados e registados. "
                     "Nao inclui tokens/contas nao observados, airdrops, transferencias, "
                     "impostos ou custos externos. Operacoes sem preco, taxa ou custo de aquisicao "
                     "comprovados ficam fora do PnL realizado; nao e garantia de desempenho futuro.")
        }

    def top_wallets(self, limit=20, min_closed_trades=5):
        with self._db() as db:
            wallets = [r[0] for r in db.execute(
                "SELECT DISTINCT wallet FROM swaps WHERE ts IS NOT NULL AND ts_verified=1").fetchall()]
        reports = [self.wallet_report(w, min_closed_trades) for w in wallets]
        reports.sort(key=lambda x: (x["amostra_suficiente"],
                                    x["pnl_realizado_usd_fifo"]), reverse=True)
        return reports[:max(1, min(100, int(limit)))]
