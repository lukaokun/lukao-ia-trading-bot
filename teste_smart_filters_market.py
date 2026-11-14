import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot as B
from smart_filters_market import DEFAULT_INTERVAL_SECONDS, MarketSnapshotRecorder, utc_now


MINT = "11111111111111111111111111111111"


def par(address="pair-a", dex="orca", liquidity=12000):
    return {
        "pairAddress": address, "dexId": dex, "priceUsd": "0.00042",
        "liquidity": {"usd": liquidity}, "marketCap": 420000,
        "fdv": 480000, "pairCreatedAt": 1770000000000,
        "volume": {"m5": 100, "h1": 200, "h6": 300, "h24": 400},
        "txns": {w: {"buys": i, "sells": i + 1} for w, i in
                 (("m5", 2), ("h1", 4), ("h6", 6), ("h24", 8))},
        "priceChange": {"m5": 1.2, "h1": 3.4, "h6": 5.6, "h24": 7.8},
    }


class MarketSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bots = []
        self.path = os.path.join(self.temp.name, "smart_filters_market.db")
        self.now = utc_now()
        self.store = MarketSnapshotRecorder(self.path, interval_seconds=45)

    def tearDown(self):
        for bot in self.bots:
            bot.close()
        self.store.close()
        for handler in list(B.log.handlers):
            B.log.removeHandler(handler)
            handler.close()
        self.temp.cleanup()

    def save(self, value, when=None):
        count = self.store.record_pairs({MINT: value}, [MINT], when or self.now)
        self.assertTrue(self.store.flush())
        return count

    def test_default_cadence_is_sixty_seconds(self):
        self.assertEqual(DEFAULT_INTERVAL_SECONDS, 60)

    def rows(self):
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            result = [dict(row) for row in db.execute(
                "SELECT * FROM market_snapshots ORDER BY observed_at_utc")]
        finally:
            db.close()
        return result

    def test_valid_snapshot_all_windows_and_distinct_market_cap_fdv(self):
        self.assertEqual(self.save([par()]), 1)
        row = self.rows()[0]
        self.assertEqual(row["data_status"], "valid")
        self.assertEqual(row["source"], "DexScreener")
        self.assertEqual(row["market_cap_usd"], 420000)
        self.assertEqual(row["fdv_usd"], 480000)
        self.assertEqual((row["volume_m5"], row["volume_h1"], row["volume_h6"], row["volume_h24"]),
                         (100, 200, 300, 400))
        self.assertEqual((row["buys_m5"], row["sells_m5"], row["buys_h1"], row["sells_h1"],
                          row["buys_h6"], row["sells_h6"], row["buys_h24"], row["sells_h24"]),
                         (2, 3, 4, 5, 6, 7, 8, 9))
        self.assertEqual(row["price_change_h24"], 7.8)
        self.assertTrue(row["observed_at_utc"].endswith("Z"))
        self.assertEqual(row["freshness_status"], "unknown_source_timestamp")

    def test_optional_missing_fields_are_null_and_reported(self):
        payload = par()
        del payload["marketCap"]
        del payload["volume"]["h6"]
        del payload["txns"]["h24"]
        del payload["priceChange"]["h1"]
        self.assertEqual(self.save([payload]), 1)
        row = self.rows()[0]
        self.assertEqual(row["data_status"], "incomplete")
        self.assertIsNone(row["market_cap_usd"])
        self.assertIsNone(row["volume_h6"])
        self.assertIsNone(row["buys_h24"])
        self.assertIsNone(row["price_change_h1"])
        missing = json.loads(row["missing_fields"])
        self.assertTrue({"market_cap_usd", "volume_h6", "buys_h24", "sells_h24", "price_change_h1"}.issubset(missing))

    def test_interval_deduplicates_per_mint_then_allows_next_snapshot(self):
        self.assertEqual(self.save([par()]), 1)
        self.assertEqual(self.save([par(liquidity=13000)], self.now + timedelta(seconds=30)), 0)
        self.assertEqual(self.save([par(liquidity=13000)], self.now + timedelta(seconds=45)), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_pair_selection_is_stable_and_pair_switch_is_explicit(self):
        self.assertEqual(self.save([par("pair-a", liquidity=10000), par("pair-b", liquidity=15000)]), 1)
        self.assertEqual(self.rows()[0]["pair_address"], "pair-b")
        self.assertEqual(self.save([par("pair-b", liquidity=9000), par("pair-a", liquidity=20000)],
                                   self.now + timedelta(seconds=45)), 1)
        same = self.rows()[1]
        self.assertEqual(same["pair_address"], "pair-b")
        self.assertEqual(same["pair_changed"], 0)
        self.assertEqual(self.save([par("pair-c", liquidity=11000)], self.now + timedelta(seconds=90)), 1)
        switched = self.rows()[2]
        self.assertEqual(switched["pair_address"], "pair-c")
        self.assertEqual(switched["previous_pair_address"], "pair-b")
        self.assertEqual(switched["pair_changed"], 1)

    def test_no_pair_and_failed_request_are_not_confused(self):
        self.store.record_pairs({MINT: []}, [MINT], self.now)
        self.assertTrue(self.store.flush())
        self.store.record_pairs({MINT: None}, [MINT], self.now + timedelta(seconds=45))
        self.assertTrue(self.store.flush())
        rows = self.rows()
        self.assertEqual([r["data_status"] for r in rows], ["no_pair", "unavailable"])
        self.assertTrue(all(r["pair_address"] is None for r in rows))

    def test_stale_input_is_marked_without_fabricating_metrics(self):
        old = self.now - timedelta(minutes=3)
        self.assertEqual(self.save([par()], old), 1)
        row = self.rows()[0]
        self.assertEqual(row["data_status"], "stale")
        self.assertEqual(row["freshness_status"], "stale_at_collection")

    def test_history_survives_recorder_recreation(self):
        self.save([par()])
        self.store.close()
        self.store = MarketSnapshotRecorder(self.path)
        later = self.now + timedelta(seconds=45)
        self.assertEqual(self.save([par(liquidity=13500)], later), 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["liquidity_usd"], 13500)

    def test_http_429_retry_after_and_timeout_are_handled_offline(self):
        old_get, old_sleep = B.requests.get, B.dormir
        sleeps, responses = [], [type("Response", (), {"status_code": 429,
                    "headers": {"Retry-After": "4"}})(),
                    type("Response", (), {"status_code": 200,
                    "json": lambda self: {"pairs": []}})()]
        B.requests.get = lambda *a, **k: responses.pop(0)
        B.dormir = lambda seconds: sleeps.append(seconds)
        try:
            cfg = dict(B.CONFIG, tentativas=2, espera_base_seg=0.1,
                       intervalo_min_pares_seg=0, intervalo_min_perfis_seg=0,
                       intervalo_min_rugcheck_seg=0)
            result = B.ClienteHTTP(cfg).get_json("https://offline.invalid/test", "pares")
            self.assertEqual(result, {"pairs": []})
            self.assertEqual(sleeps, [4.0])
            B.requests.get = lambda *a, **k: (_ for _ in ()).throw(B.requests.Timeout("offline timeout"))
            cfg["tentativas"] = 1
            self.assertIsNone(B.ClienteHTTP(cfg).get_json("https://offline.invalid/test", "pares"))
        finally:
            B.requests.get, B.dormir = old_get, old_sleep

    def test_bot_cycle_consumes_existing_pairs_without_extra_http_or_trades(self):
        cfg = dict(B.CONFIG)
        cfg.update(ficheiro_estado=os.path.join(self.temp.name, "estado.json"),
                   ficheiro_trades=os.path.join(self.temp.name, "trades.csv"),
                   ficheiro_log=os.path.join(self.temp.name, "bot.log"), painel_web=False)
        B.configurar_log(cfg)
        bot = B.Bot(cfg)
        self.bots.append(bot)

        class ScannerStub:
            respondeu = True
            ultimos_mints = [MINT]
            ultimos_pares = {MINT: [par()]}

            def procurar(self, *args, **kwargs):
                return [], []

        bot.scanner = ScannerStub()
        before_balance = bot.estado["saldo"]
        self.assertEqual(bot.ciclo_scanner(), 0)
        self.assertTrue(bot.market_snapshots.flush())
        self.assertEqual(bot.estado["saldo"], before_balance)
        self.assertEqual(bot.estado["posicoes"], {})
        self.assertEqual(bot.estado["stats"]["fechados"], 0)
        self.assertEqual(len(self.rows()), 1)
        bot.modo = "demo"
        self.assertEqual(bot.registar_market_snapshots({MINT: [par()]}, [MINT]), 0)
        self.assertEqual(len(self.rows()), 1)
        bot.market_snapshots.close()
        for handler in list(B.log.handlers):
            B.log.removeHandler(handler)
            handler.close()

    def test_storage_initialization_failure_does_not_stop_bot(self):
        cfg = dict(B.CONFIG)
        cfg.update(ficheiro_estado=os.path.join(self.temp.name, "state-fail.json"),
                   ficheiro_trades=os.path.join(self.temp.name, "trades-fail.csv"),
                   ficheiro_log=os.path.join(self.temp.name, "bot-fail.log"), painel_web=False)
        B.configurar_log(cfg)
        try:
            with patch.object(B, "MarketSnapshotRecorder", side_effect=OSError("offline disk")):
                bot = B.Bot(cfg)
            self.bots.append(bot)
            self.assertIsNone(bot.market_snapshots)
            self.assertEqual(bot.registar_market_snapshots({MINT: [par()]}, [MINT]), 0)
            self.assertEqual(bot.estado["posicoes"], {})
        finally:
            bot.close()
            for handler in list(B.log.handlers):
                B.log.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
