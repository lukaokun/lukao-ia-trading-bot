import tempfile
import unittest
from pathlib import Path
from backtest_csv import load_rows, run_backtest

class BacktestTests(unittest.TestCase):
    def test_run_includes_costs_and_closes(self):
        data = {"M": [(1, 1.0), (2, 1.5)]}
        result = run_backtest(data, fee_pct=0, slippage_pct=0,
                              take_profit_pct=0.2, stop_loss_pct=0.1)
        self.assertEqual(result["summary"]["trades"], 1)
        self.assertGreater(result["summary"]["net_pnl_usd"], 0)

    def test_csv_required_columns(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "bad.csv"
            f.write_text("mint,price_usd\\nM,1\\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rows(f)

    def test_invalid_nan_rejected(self):
        with self.assertRaises(ValueError):
            run_backtest({"M": [(1, 1)]}, initial_cash=float("nan"))

if __name__ == "__main__":
    unittest.main()
