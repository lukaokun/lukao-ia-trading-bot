# -*- coding: utf-8 -*-
import unittest
from early_accumulation import score_acumulacao

class TestEarlyAccumulation(unittest.TestCase):
    def par(self, **kw):
        p = {
            "liquidity": {"usd": 50000},
            "volume": {"m5": 4000, "h1": 50000},
            "txns": {"m5": {"buys": 24, "sells": 6}, "h1": {"buys": 120, "sells": 60}},
            "priceChange": {"m5": 3, "h1": 12},
        }
        for k,v in kw.items():
            p[k]=v
        return p

    def test_sinal_forte(self):
        r=score_acumulacao(self.par())
        self.assertGreaterEqual(r["score"], 7.5)
        self.assertEqual(r["label"], "forte")

    def test_pressao_vendedora_reduz_nota(self):
        p=self.par()
        p["txns"]["m5"]={"buys": 5, "sells": 25}
        p["txns"]["h1"]={"buys": 40, "sells": 120}
        self.assertLess(score_acumulacao(p)["score"], 5)

    def test_pump_excessivo_nao_recebe_bonus_preco(self):
        p=self.par()
        p["priceChange"]={"m5": 35, "h1": 120}
        self.assertLess(score_acumulacao(p)["score"], score_acumulacao(self.par())["score"])

    def test_liquidez_ausente(self):
        self.assertEqual(score_acumulacao({"liquidity":{"usd":0}})["score"], 0)

if __name__ == "__main__":
    unittest.main()
