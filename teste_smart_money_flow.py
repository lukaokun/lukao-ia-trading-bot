import unittest
from smart_money_flow import score_smart_money_flow


class SmartMoneyFlowTests(unittest.TestCase):
    def test_net_buying_and_breadth(self):
        events = [
            {"signature": str(i), "wallet": f"w{i}", "mint": "M",
             "side": "buy", "quote_usd": 100, "timestamp": 1000}
            for i in range(5)
        ]
        out = score_smart_money_flow(events, now=1000)["M"]
        self.assertEqual(out["buyers"], 5)
        self.assertEqual(out["net_buy_usd"], 500)
        self.assertGreaterEqual(out["score"], 6.9)

    def test_sells_reduce_net_pressure(self):
        events = [
            {"signature": "a", "wallet": "w1", "mint": "M",
             "side": "buy", "quote_usd": 100, "timestamp": 1000},
            {"signature": "b", "wallet": "w2", "mint": "M",
             "side": "sell", "quote_usd": 90, "timestamp": 1000},
        ]
        out = score_smart_money_flow(events, now=1000)["M"]
        self.assertEqual(out["net_buy_usd"], 10)

    def test_dedup_and_old_events(self):
        e = {"signature": "a", "wallet": "w", "mint": "M",
             "side": "buy", "quote_usd": 50, "timestamp": 1000}
        out = score_smart_money_flow([e, e], now=1000)
        self.assertEqual(out["M"]["observed_swaps"], 1)
        self.assertEqual(score_smart_money_flow([e], now=5000), {})

    def test_same_signature_keeps_distinct_wallet_and_mint_events(self):
        events = [
            {"signature": "shared", "wallet": "w1", "mint": "M1",
             "side": "buy", "quote_usd": 10, "timestamp": 1000},
            {"signature": "shared", "wallet": "w2", "mint": "M2",
             "side": "buy", "quote_usd": 20, "timestamp": 1000},
        ]
        out = score_smart_money_flow(events, now=1000)
        self.assertEqual(out["M1"]["observed_swaps"], 1)
        self.assertEqual(out["M2"]["observed_swaps"], 1)

    def test_deduplication_key_matches_tracker_side_identity(self):
        events = [
            {"signature": "same", "wallet": "w", "mint": "M",
             "side": "buy", "quote_usd": 100, "timestamp": 1000},
            {"signature": "same", "wallet": "w", "mint": "M",
             "side": "sell", "quote_usd": 40, "timestamp": 1000},
            {"signature": "same", "wallet": "w", "mint": "M",
             "side": "buy", "quote_usd": 100, "timestamp": 1000},
        ]
        out = score_smart_money_flow(events, now=1000)["M"]
        self.assertEqual(out["observed_swaps"], 2)
        self.assertEqual(out["net_buy_usd"], 60)

    def test_only_qualified_wallets_count_as_smart(self):
        events = [{"signature": "a", "wallet": "w", "mint": "M",
                   "side": "buy", "quote_usd": 100, "timestamp": 1000}]
        reports = {"w": {"amostra_suficiente": True, "lotes_fechados": 8,
                         "pnl_realizado_usd_fifo": 25}}
        out = score_smart_money_flow(events, reports, now=1000)["M"]
        self.assertEqual(out["historically_profitable_buyers"], 1)


if __name__ == "__main__":
    unittest.main()
