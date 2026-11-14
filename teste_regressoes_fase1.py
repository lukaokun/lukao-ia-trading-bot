"""Regression coverage for audit findings E1, E2, E3, E5, E8, E9 and E10."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from solana_test_utils import test_pubkey
from solana_swap_decoder import SOL, USDC, WSOL, decode_swaps
from smart_money_pipeline import SmartMoneyPipeline
from smart_wallet_tracker import SmartWalletTracker
from wallet_monitor import WalletMonitor
from wallet_ranking import rank_wallet_reports
from bot import Analista, CONFIG

WALLET = test_pubkey(100)
TOKEN_A = test_pubkey(101)
TOKEN_B = test_pubkey(102)


def bal(owner, mint, value):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmountString": str(value)}}


def token_tx(signature, mint, token_before, token_after, quote_mint=USDC,
             quote_before=100, quote_after=90, block_time=100):
    return {
        "blockTime": block_time,
        "transaction": {"signatures": [signature], "message": {"accountKeys": []}},
        "meta": {"err": None,
                 "preTokenBalances": [bal(WALLET, mint, token_before), bal(WALLET, quote_mint, quote_before)],
                 "postTokenBalances": [bal(WALLET, mint, token_after), bal(WALLET, quote_mint, quote_after)]},
    }


class Phase1RegressionTests(unittest.TestCase):
    def test_e1_fifo_separates_mints(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = SmartWalletTracker(str(Path(d) / "ledger.db"))
            tracker.record_swap(signature="a-buy", wallet="w", mint="A", side="buy",
                                token_amount=10, quote_usd=100, fee_usd=0, timestamp=1)
            tracker.record_swap(signature="b-buy", wallet="w", mint="B", side="buy",
                                token_amount=10, quote_usd=10, fee_usd=0, timestamp=2)
            tracker.record_swap(signature="b-sell", wallet="w", mint="B", side="sell",
                                token_amount=10, quote_usd=20, fee_usd=0, timestamp=3)
            report = tracker.wallet_report("w", min_closed_trades=1)
            self.assertEqual(report["pnl_realizado_usd_fifo"], 10)
            self.assertEqual(report["posicao_aberta_tokens"], 10)
            self.assertEqual(report["custo_posicao_aberta_usd"], 100)

    def test_e2_native_sol_is_not_inferred_and_wsol_needs_explicit_price(self):
        native = token_tx("sol-buy", TOKEN_A, 0, 5)
        native["meta"]["preTokenBalances"] = [bal(WALLET, TOKEN_A, 0)]
        native["meta"]["postTokenBalances"] = [bal(WALLET, TOKEN_A, 5)]
        native["transaction"]["message"]["accountKeys"] = [{"pubkey": WALLET}]
        native["meta"].update(preBalances=[20_000_000_000], postBalances=[19_899_995_000], fee=5_000)
        self.assertEqual(decode_swaps(native), [])
        self.assertEqual(decode_swaps(native, quote_prices_usd={SOL: 20}), [])
        # Transfers, rent, account lifecycle changes and network fees are
        # indistinguishable from swap value in net lamport deltas alone.
        for pre, post, fee in (([20, 30], [18, 32], 0),
                               ([20, 0], [18, 2], 0),
                               ([20, 2], [18, 0], 0),
                               ([20], [19], 1_000_000_000)):
            ambiguous = token_tx("ambiguous-sol", TOKEN_A, 0, 5)
            ambiguous["meta"]["preTokenBalances"] = [bal(WALLET, TOKEN_A, 0)]
            ambiguous["meta"]["postTokenBalances"] = [bal(WALLET, TOKEN_A, 5)]
            ambiguous["transaction"]["message"]["accountKeys"] = [
                {"pubkey": WALLET} for _ in pre
            ]
            ambiguous["meta"].update(preBalances=pre, postBalances=post, fee=fee)
            self.assertEqual(decode_swaps(ambiguous, quote_prices_usd={SOL: 20}), [])
        wrapped = token_tx("wsol-buy", TOKEN_A, 0, 5, WSOL, 2, 1.9)
        self.assertEqual(decode_swaps(wrapped), [])
        event = decode_swaps(wrapped, quote_prices_usd={WSOL: 20})
        self.assertEqual(event, [])  # WSOL price alone is not instruction evidence
        wrapped_with_rent_and_fee = token_tx("wsol-rent", TOKEN_A, 0, 5, WSOL, 2, 1.9)
        wrapped_with_rent_and_fee["transaction"]["message"]["accountKeys"] = [{"pubkey": WALLET}]
        wrapped_with_rent_and_fee["meta"].update(preBalances=[10_000_000_000],
                                                   postBalances=[8_999_995_000], fee=5_000)
        event = decode_swaps(wrapped_with_rent_and_fee, quote_prices_usd={WSOL: 20})
        self.assertEqual(event, [])  # rent and fee cannot establish a swap

    def test_e3_paginates_before_advancing_cursor(self):
        class Pipeline:
            def __init__(self): self.seen = []
            def ingest_transaction(self, tx, wallet_filter=None, **kwargs):
                self.seen.append(tx["sig"])
                return [{"signature": tx["sig"]}]

        class RPC:
            ordered = ["new3", "new2", "new1", "old"]
            def __init__(self): self.calls = []
            def call(self, method, params):
                self.calls.append((method, params))
                if method == "getSignaturesForAddress":
                    cfg = params[1]
                    start = (self.ordered.index(cfg["before"]) + 1
                             if cfg.get("before") in self.ordered else 0)
                    rows = self.ordered[start:]
                    if cfg.get("until") in self.ordered:
                        rows = rows[:rows.index(cfg["until"]) + 1]
                    return [{"signature": s, "err": None} for s in rows[:cfg["limit"]]]
                return {"sig": params[0]}

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            state.write_text(json.dumps({WALLET: "old"}))
            pipe, rpc = Pipeline(), RPC()
            monitor = WalletMonitor(pipe, rpc, str(state), max_signatures_per_wallet=2, pause_seconds=0)
            result = monitor.poll_wallet(WALLET)
            self.assertEqual(pipe.seen, ["new1", "new2", "new3"])
            self.assertTrue(result["cursor_updated"])
            self.assertEqual(json.loads(state.read_text())[WALLET], "new3")
            self.assertGreaterEqual(sum(m == "getSignaturesForAddress" for m, _ in rpc.calls), 2)

    def test_e3_cursor_stays_put_when_pages_cannot_be_completed(self):
        class Pipeline:
            def ingest_transaction(self, tx, wallet_filter=None, **kwargs): return []
        class TruncatedRPC:
            def call(self, method, params):
                if method == "getSignaturesForAddress":
                    return [{"signature": "n2"}, {"signature": "n1"}]
                return {"sig": params[0]}
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            state.write_text(json.dumps({WALLET: "old"}))
            monitor = WalletMonitor(Pipeline(), TruncatedRPC(), str(state),
                                    max_signatures_per_wallet=2, pause_seconds=0)
            result = monitor.poll_wallet(WALLET)
            self.assertFalse(result["cursor_updated"])
            self.assertEqual(json.loads(state.read_text())[WALLET], "old")

    def test_e5_unavailable_rugcheck_blocks_candidate(self):
        cfg = dict(CONFIG)
        cfg.update(usar_rugcheck=True, pontuacao_minima=1, idade_minima_min=1,
                   idade_maxima_horas=1000, liquidez_minima_usd=100,
                   fdv_minimo_usd=1, fdv_maximo_usd=10_000_000,
                   volume_h1_minimo_usd=1, txns_h1_minimo=1)
        class HTTP:
            def get_json(self, *args, **kwargs): return None
        pair = {"priceUsd": "1", "liquidity": {"usd": 100_000}, "marketCap": 200_000,
                "pairCreatedAt": int((time.time() - 3600) * 1000),
                "volume": {"m5": 10_000, "h1": 100_000},
                "txns": {"m5": {"buys": 20, "sells": 2}, "h1": {"buys": 100, "sells": 10}},
                "priceChange": {"m5": 2, "h1": 20}}
        result = Analista(cfg, HTTP()).analisar(TOKEN_A, pair)
        self.assertFalse(result["aprovado"])
        self.assertTrue(result["reavaliar"])
        self.assertIn("RugCheck indisponivel", result["motivo"])

    def test_e5_malformed_rugcheck_blocks_candidate(self):
        cfg = dict(CONFIG)
        cfg.update(usar_rugcheck=True, pontuacao_minima=1, idade_minima_min=1,
                   idade_maxima_horas=1000, liquidez_minima_usd=100,
                   fdv_minimo_usd=1, fdv_maximo_usd=10_000_000,
                   volume_h1_minimo_usd=1, txns_h1_minimo=1)
        pair = {"priceUsd": "1", "liquidity": {"usd": 100_000}, "marketCap": 200_000,
                "pairCreatedAt": int((time.time() - 3600) * 1000),
                "volume": {"m5": 10_000, "h1": 100_000},
                "txns": {"m5": {"buys": 20, "sells": 2}, "h1": {"buys": 100, "sells": 10}},
                "priceChange": {"m5": 2, "h1": 20}}
        malformed = [
            {"risks": []},
            {"score_normalised": "invalid", "risks": []},
            {"score_normalised": float("nan"), "risks": []},
            {"score_normalised": True, "risks": []},
            {"score": "invalid", "risks": []},
            {"score": float("inf"), "risks": []},
            {"score_normalised": 10, "risks": "not-a-list"},
            {"score_normalised": 10, "risks": [None]},
            {"score_normalised": 101, "risks": []},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                class HTTP:
                    def get_json(self, *args, **kwargs): return payload
                result = Analista(cfg, HTTP()).analisar(TOKEN_A, pair)
                self.assertFalse(result["aprovado"])
                self.assertTrue(result["reavaliar"])
                self.assertIn("RugCheck indisponivel", result["motivo"])

    def test_e8_unknown_transaction_time_is_separate_from_ingestion(self):
        with tempfile.TemporaryDirectory() as d:
            pipeline = SmartMoneyPipeline(str(Path(d) / "ledger.db"))
            before = time.time()
            tx = token_tx("unknown-time", TOKEN_A, 0, 1, block_time=None)
            self.assertEqual(decode_swaps(tx), [])
            self.assertEqual(pipeline.ingest_transaction(tx), [])
            # This synthetic transaction has no supported swap instructions;
            # the direct ledger row isolates E8 timestamp filtering.
            pipeline.tracker.record_swap(
                signature="unknown-time", wallet=WALLET, mint=TOKEN_A,
                side="buy", token_amount=1, quote_usd=1, fee_usd=0.0,
                timestamp=None)
            with pipeline.tracker._db() as db:
                tx_time, ingest_time = db.execute(
                    "SELECT ts,ingestion_ts FROM swaps WHERE signature='unknown-time'").fetchone()
            report = pipeline.wallet_report(WALLET)
            self.assertIsNone(tx_time)
            self.assertGreaterEqual(ingest_time, before)
            self.assertEqual(report["swaps_sem_data_transacao"], 1)
            self.assertEqual(report["swaps_confirmados"], 0)
            self.assertEqual(report["tokens_distintos"], 0)
            self.assertEqual(report["lotes_fechados"], 0)
            self.assertEqual(rank_wallet_reports([report])[0]["ranking_components"]["token_breadth"], 0)

    def test_e8_unknown_swap_can_become_verified_on_reimport(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = SmartWalletTracker(str(Path(d) / "ledger.db"))
            args = dict(signature="reimport", wallet=WALLET, mint=TOKEN_A, side="buy",
                        token_amount=2, quote_usd=20)
            self.assertTrue(tracker.record_swap(**args, timestamp=None))
            self.assertTrue(tracker.record_swap(**args, timestamp=1234))
            self.assertFalse(tracker.record_swap(**args, timestamp=1235))
            report = tracker.wallet_report(WALLET)
            self.assertEqual(report["swaps_sem_data_transacao"], 0)
            self.assertEqual(report["swaps_confirmados"], 1)
            self.assertEqual(report["tokens_distintos"], 1)

    def test_e8_legacy_rows_are_unverified_until_reimported(self):
        schemas = [
            """CREATE TABLE swaps (signature TEXT PRIMARY KEY, wallet TEXT NOT NULL,
                mint TEXT NOT NULL, ts REAL NOT NULL, side TEXT NOT NULL,
                token_amount REAL NOT NULL, quote_usd REAL NOT NULL, fee_usd REAL NOT NULL)""",
            """CREATE TABLE swaps (signature TEXT NOT NULL, wallet TEXT NOT NULL,
                mint TEXT NOT NULL, ts REAL, ingestion_ts REAL NOT NULL,
                side TEXT NOT NULL, token_amount REAL NOT NULL, quote_usd REAL NOT NULL,
                fee_usd REAL NOT NULL, PRIMARY KEY(signature,wallet,mint,side))""",
        ]
        for index, schema in enumerate(schemas):
            with self.subTest(schema=index), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "legacy.db"
                db = sqlite3.connect(path)
                try:
                    db.execute(schema)
                    if index == 0:
                        db.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?)",
                                   ("legacy", WALLET, TOKEN_A, time.time(), "buy", 1, 10, 0))
                    else:
                        db.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?)",
                                   ("legacy", WALLET, TOKEN_A, time.time(), time.time(),
                                    "buy", 1, 10, 0))
                    db.commit()
                finally:
                    db.close()
                tracker = SmartWalletTracker(str(path))
                with tracker._db() as db:
                    self.assertIn("idx_swaps_wallet_mint_ts", {
                        row[1] for row in db.execute("PRAGMA index_list(swaps)")
                    })
                report = tracker.wallet_report(WALLET)
                self.assertEqual(report["swaps_confirmados"], 0)
                self.assertEqual(report["swaps_sem_data_transacao"], 1)
                self.assertEqual(report["tokens_distintos"], 0)
                self.assertEqual(tracker.top_wallets(), [])
                self.assertEqual(rank_wallet_reports([report])[0]["ranking_components"]["token_breadth"], 0)
                self.assertTrue(tracker.record_swap(signature="legacy", wallet=WALLET,
                    mint=TOKEN_A, side="buy", token_amount=1, quote_usd=10, timestamp=42))
                report = tracker.wallet_report(WALLET)
                self.assertEqual(report["swaps_confirmados"], 1)
                self.assertEqual(report["swaps_sem_data_transacao"], 0)

    def test_e9_one_sell_across_multiple_lots_is_one_operation(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = SmartWalletTracker(str(Path(d) / "ledger.db"))
            for i in range(5):
                tracker.record_swap(signature=f"buy-{i}", wallet="w", mint="A", side="buy",
                                    token_amount=1, quote_usd=10, timestamp=i + 1)
            tracker.record_swap(signature="single-sell", wallet="w", mint="A", side="sell",
                                token_amount=5, quote_usd=60, timestamp=10)
            report = tracker.wallet_report("w", min_closed_trades=2)
            self.assertEqual(report["operacoes_fechadas"], 1)
            self.assertFalse(report["amostra_suficiente"])

    def test_e10_failed_transaction_and_invalid_addresses_are_ignored(self):
        failed = token_tx("failed", TOKEN_A, 0, 1)
        failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
        self.assertEqual(decode_swaps(failed), [])
        invalid_owner = token_tx("invalid", TOKEN_A, 0, 1)
        for side in ("preTokenBalances", "postTokenBalances"):
            for entry in invalid_owner["meta"][side]:
                entry["owner"] = "not-a-public-key"
        self.assertEqual(decode_swaps(invalid_owner), [])


if __name__ == "__main__":
    unittest.main()
