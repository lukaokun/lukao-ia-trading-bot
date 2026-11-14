import os, tempfile, unittest
from smart_wallet_tracker import SmartWalletTracker

class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = SmartWalletTracker(os.path.join(self.tmp.name, "wallets.db"))
    def tearDown(self):
        self.tmp.cleanup()
    def test_fifo_profit_and_winrate(self):
        self.t.record_swap(signature="b1", wallet="W", mint="M", side="buy",
                           token_amount=10, quote_usd=100, fee_usd=0, timestamp=1)
        self.t.record_swap(signature="s1", wallet="W", mint="M", side="sell",
                           token_amount=5, quote_usd=70, fee_usd=0, timestamp=2)
        r = self.t.wallet_report("W", min_closed_trades=1)
        self.assertAlmostEqual(r["pnl_realizado_usd_fifo"], 20)
        self.assertEqual(r["win_rate_lotes_pct"], 100)
        self.assertAlmostEqual(r["posicao_aberta_tokens"], 5)
    def test_same_signature_can_record_multiple_wallets(self):
        self.assertTrue(self.t.record_swap(signature="shared", wallet="W1", mint="M",
            side="buy", token_amount=1, quote_usd=2, timestamp=1))
        self.assertTrue(self.t.record_swap(signature="shared", wallet="W2", mint="M",
            side="buy", token_amount=1, quote_usd=2, timestamp=1))

    def test_fifo_proceeds_are_proportional_across_multiple_lots(self):
        self.t.record_swap(signature="b1", wallet="W", mint="M", side="buy",
            token_amount=1, quote_usd=10, fee_usd=0, timestamp=1)
        self.t.record_swap(signature="b2", wallet="W", mint="M", side="buy",
            token_amount=1, quote_usd=30, fee_usd=0, timestamp=2)
        self.t.record_swap(signature="s1", wallet="W", mint="M", side="sell",
            token_amount=2, quote_usd=60, fee_usd=0, timestamp=3)
        r=self.t.wallet_report("W", min_closed_trades=1)
        self.assertAlmostEqual(r["pnl_realizado_usd_fifo"], 20)

    def test_duplicate_signature_ignored(self):
        args=dict(signature="same",wallet="W",mint="M",side="buy",
                  token_amount=1,quote_usd=2,fee_usd=0,timestamp=1)
        self.assertTrue(self.t.record_swap(**args))
        self.assertFalse(self.t.record_swap(**args))
        self.assertEqual(self.t.wallet_report("W")["swaps_confirmados"],1)
    def test_unknown_sample_not_smart_money(self):
        self.t.record_swap(signature="b",wallet="W",mint="M",side="buy",
                           token_amount=1,quote_usd=2,fee_usd=0,timestamp=1)
        r=self.t.wallet_report("W")
        self.assertFalse(r["amostra_suficiente"])
        self.assertEqual(r["classificacao"],"historico_insuficiente")
    def test_invalid_side_rejected(self):
        with self.assertRaises(ValueError):
            self.t.record_swap(signature="x",wallet="W",mint="M",side="transfer",
                               token_amount=1,quote_usd=2)

    def test_unknown_usd_values_remain_null_and_zero_remains_zero(self):
        self.t.record_swap(signature="unknown", wallet="W", mint="M1", side="buy",
                           token_amount=1, quote_usd=None, fee_usd=None, timestamp=1,
                           quote_amount=2, quote_amount_raw=2_000_000,
                           quote_decimals=6, quote_mint="Q")
        self.t.record_swap(signature="zero", wallet="W", mint="M2", side="buy",
                           token_amount=1, quote_usd=0.0, fee_usd=0.0, timestamp=2)
        with self.t._db() as db:
            rows = db.execute("SELECT signature,quote_usd,fee_usd FROM swaps ORDER BY signature").fetchall()
        self.assertEqual(rows, [("unknown", None, None), ("zero", 0.0, 0.0)])
        report = self.t.wallet_report("W")
        self.assertEqual(report["swaps_sem_preco_usd"], 1)
        self.assertEqual(report["swaps_sem_taxa_usd"], 1)

    def test_fifo_does_not_invent_unknown_cost_or_proceeds(self):
        self.t.record_swap(signature="buy-unknown", wallet="W", mint="M", side="buy",
                           token_amount=2, quote_usd=10, fee_usd=None, timestamp=1)
        self.t.record_swap(signature="sell-known", wallet="W", mint="M", side="sell",
                           token_amount=2, quote_usd=20, fee_usd=0, timestamp=2)
        report = self.t.wallet_report("W", min_closed_trades=1)
        self.assertEqual(report["pnl_realizado_usd_fifo"], 0.0)
        self.assertEqual(report["operacoes_registadas"], 1)
        self.assertEqual(report["operacoes_rentabilidade_desconhecida"], 1)
        self.assertEqual(report["operacoes_rentabilidade_calculavel"], 0)

    def test_audit_status_is_idempotent_and_upgrades_unavailable(self):
        self.t.record_transaction_status(signature="s", wallet_scope="W", status="unavailable",
                                         reason="RPC null")
        self.t.record_transaction_status(signature="s", wallet_scope="W", status="obtained_unsupported",
                                         raw_tx={"transaction": {"signatures": ["s"]}},
                                         reason="unknown program")
        audit = self.t.get_transaction_audit("s", "W")
        self.assertEqual(audit["status"], "obtained_unsupported")
        self.assertEqual(audit["attempts"], 2)
        self.assertEqual(audit["raw_tx"]["transaction"]["signatures"], ["s"])

    def test_migration_preserves_legacy_zero_values_and_unknown_ingestion_time(self):
        legacy = os.path.join(self.tmp.name, "legacy.db")
        import sqlite3
        db = sqlite3.connect(legacy)
        db.execute("""CREATE TABLE swaps (
            signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, mint TEXT NOT NULL,
            ts REAL, side TEXT NOT NULL, token_amount REAL NOT NULL,
            quote_usd REAL NOT NULL, fee_usd REAL NOT NULL)""")
        db.executemany("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?)", [
            ("legacy-zero", "W", "M0", 10, "buy", 1, 0.0, 0.0),
            ("legacy-value", "W", "M1", None, "buy", 2, 12.5, 0.25),
        ])
        db.commit()
        db.close()
        migrated = SmartWalletTracker(legacy)
        with migrated._db() as db:
            rows = db.execute("""SELECT signature,ts,ingestion_ts,ts_verified,
                quote_usd,fee_usd FROM swaps ORDER BY signature""").fetchall()
        self.assertEqual(rows, [
            ("legacy-value", None, None, 0, 12.5, 0.25),
            ("legacy-zero", 10.0, None, 0, 0.0, 0.0),
        ])
        self.assertEqual(migrated.wallet_report("W")["swaps_sem_data_transacao"], 2)
if __name__ == "__main__":
    unittest.main()
