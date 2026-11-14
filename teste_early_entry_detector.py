import unittest
from early_entry_detector import score_early_entry


def pair(liq=50000):
    return {
        "liquidity": {"usd": liq},
        "volume": {"m5": 8000},
        "txns": {"m5": {"buys": 25, "sells": 5},
                 "h1": {"buys": 80, "sells": 20}},
        "priceChange": {"m5": 2, "h1": 10}
    }


class EarlyEntryTests(unittest.TestCase):
    def test_good_converging_signal_is_review_candidate_only(self):
        flow={"score": 8, "observed_swaps": 12,
              "historically_profitable_buyers": 2}
        risk={"rugcheck_ok": True, "mint_authority_revoked": True,
              "freeze_authority_revoked": True, "slippage_pct": 1}
        out=score_early_entry(pair(), flow, risk)
        self.assertEqual(out["status"], "candidato_para_revisao")
        self.assertIn("nenhuma_ordem", out["action"])

    def test_low_liquidity_blocks(self):
        out=score_early_entry(pair(1000), {"score": 9, "observed_swaps": 20,
                              "historically_profitable_buyers": 3},
                              {"rugcheck_ok": True})
        self.assertEqual(out["status"], "bloqueado")

    def test_missing_smart_money_sample_is_observe(self):
        out=score_early_entry(pair(), {"score": 0, "observed_swaps": 0},
                              {"rugcheck_ok": True})
        self.assertEqual(out["status"], "observar")
        self.assertIn("amostra de swaps insuficiente", out["reasons"])

    def test_suspicious_risk_blocks_even_with_high_scores(self):
        out=score_early_entry(pair(), {"score": 10, "observed_swaps": 50,
                              "historically_profitable_buyers": 10},
                              {"rugcheck_ok": True, "suspicious": True})
        self.assertEqual(out["status"], "bloqueado")


if __name__ == "__main__":
    unittest.main()
