import unittest
from wallet_ranking import rank_wallet_reports


def report(wallet, closed, wins, losses, pnl, tokens, enough=True):
    total = wins + losses
    return {
        "wallet": wallet, "lotes_fechados": closed,
        "wins_lotes": wins, "losses_lotes": losses,
        "win_rate_lotes_pct": 100 * wins / total if total else None,
        "pnl_realizado_usd_fifo": pnl, "tokens_distintos": tokens,
        "custo_posicao_aberta_usd": 20,
        "amostra_suficiente": enough
    }


class WalletRankingTests(unittest.TestCase):
    def test_insufficient_sample_never_outranks_eligible(self):
        items = [
            report("tiny", 2, 2, 0, 500, 1, False),
            report("steady", 20, 13, 7, 100, 6, True)
        ]
        ranked = rank_wallet_reports(items)
        self.assertEqual(ranked[0]["wallet"], "steady")
        self.assertLess(ranked[1]["ranking_score"], 25)

    def test_negative_pnl_score_is_capped(self):
        ranked = rank_wallet_reports([report("loss", 30, 25, 5, -50, 10)])
        self.assertLessEqual(ranked[0]["ranking_score"], 49.9)

    def test_diversity_and_consistency_are_in_score(self):
        a = report("a", 20, 12, 8, 80, 1)
        b = report("b", 20, 16, 4, 80, 8)
        out = {x["wallet"]: x for x in rank_wallet_reports([a, b])}
        self.assertGreater(out["b"]["ranking_score"], out["a"]["ranking_score"])

    def test_invalid_and_nonfinite_rows_are_skipped(self):
        rows = [report("ok", 10, 6, 4, 10, 2), {"wallet": "bad", "pnl_realizado_usd_fifo": float("nan")}]
        out = rank_wallet_reports(rows)
        self.assertEqual([x["wallet"] for x in out], ["ok"])

    def test_limit_is_applied(self):
        rows = [report(str(i), 10, 6, 4, 10, 2) for i in range(10)]
        self.assertEqual(len(rank_wallet_reports(rows, limit=3)), 3)


if __name__ == "__main__":
    unittest.main()
