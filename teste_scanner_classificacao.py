import copy
import unittest
from unittest.mock import patch

import bot


MINT = "AbCdEf0123456789" * 2 + "AbCdEf0123456789" * 1  # 48 chars, test-only mint
NOW = 1_800_000_000.0


def mercado_valido():
    return {
        "chainId": "solana", "pairAddress": "PAIR-TEST",
        "baseToken": {"address": MINT, "symbol": "TEST", "name": "Test Token"},
        "priceUsd": "0.001", "liquidity": {"usd": 60000}, "marketCap": 400000,
        "fdv": 500000, "pairCreatedAt": int((NOW - 3600) * 1000),
        "volume": {"m5": 4000, "h1": 90000},
        "txns": {"m5": {"buys": 30, "sells": 12}, "h1": {"buys": 300, "sells": 150}},
        "priceChange": {"m5": 5, "h1": 30},
    }


def analista(cfg=None):
    c = copy.deepcopy(bot.CONFIG)
    c["usar_rugcheck"] = False
    if cfg:
        c.update(cfg)
    return bot.Analista(c, None)


class TestScannerClassificacao(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(bot, "agora", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_liquidez_null_e_zero_sao_distintos(self):
        a = analista()
        p = mercado_valido()
        p["liquidity"]["usd"] = None
        missing = a.analisar(MINT, p)
        self.assertEqual(missing["status"], "pending_data")
        self.assertIn("liquidity.usd", missing["missing_fields"])
        p["liquidity"]["usd"] = 0
        zero = a.analisar(MINT, p)
        self.assertEqual(zero["status"], "rejected")
        self.assertEqual(zero["blocking_criterion"]["observed"], 0)

    def test_preco_ausente_e_zero_sao_distintos(self):
        a = analista()
        p = mercado_valido()
        p.pop("priceUsd")
        self.assertEqual(a.analisar(MINT, p)["status"], "pending_data")
        p["priceUsd"] = "0"
        zero = a.analisar(MINT, p)
        self.assertEqual(zero["status"], "rejected")
        self.assertEqual(zero["blocking_criterion"]["observed"], 0)

    def test_rejeicao_real_tem_primeiro_bloqueio_e_criterios_posteriores(self):
        p = mercado_valido()
        p["liquidity"]["usd"] = 1
        r = analista().analisar(MINT, p)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["blocking_criterion"]["id"], "liquidez")
        self.assertEqual(r["blocking_criterion"]["threshold"], bot.CONFIG["liquidez_minima_usd"])
        self.assertTrue(any(x["status"] == "not_evaluated" for x in r["criteria"]))
        self.assertFalse(r["score_calculated"])
        self.assertIsNone(r["pontuacao"])

    def test_dados_obrigatorios_em_falta_sao_incompletos(self):
        p = mercado_valido()
        p["liquidity"].pop("usd")
        r = analista().analisar(MINT, p)
        self.assertEqual(r["status"], "pending_data")
        self.assertIsNone(r["aprovado"])
        self.assertIsNone(r["pontuacao"])
        self.assertTrue(r["blocking_criterion"])
        self.assertEqual(r["blocking_criterion"]["threshold"], bot.CONFIG["liquidez_minima_usd"])

    def test_erro_tecnico_nao_se_converte_em_score_zero(self):
        p = mercado_valido()
        with patch.object(bot, "score_acumulacao", side_effect=RuntimeError("fixture")):
            r = analista().analisar(MINT, p)
        self.assertEqual(r["status"], "technical_error")
        self.assertIsNone(r["pontuacao"])
        self.assertFalse(r["score_calculated"])
        self.assertEqual(r["technical_error"]["type"], "RuntimeError")

    def test_sem_par_tem_estado_proprio(self):
        r = analista().analisar(MINT, None)
        self.assertEqual(r["status"], "no_pair")
        self.assertIsNone(r["pontuacao"])
        self.assertFalse(r["score_calculated"])

    def test_score_nao_calculado_nao_parece_zero(self):
        r = analista().analisar(MINT, mercado_valido())
        self.assertIn(r["status"], ("approved", "rejected"))
        self.assertTrue(r["score_calculated"])
        with patch.object(bot, "score_acumulacao", side_effect=RuntimeError("fixture")):
            erro = analista().analisar(MINT, mercado_valido())
        self.assertIsNone(erro["pontuacao"])
        self.assertFalse(erro["score_calculated"])

    def test_historico_nao_promove_sinal_a_aprovacao_atual(self):
        pagina = bot.PAGINA_HTML
        self.assertIn("a.historical_approval=true", pagina)
        self.assertIn("Aprovada anteriormente; esta informação não confirma aprovação atual.", pagina)
        self.assertNotIn("estado_analise:'aprovada',aprovado:true", pagina)

    def test_estado_paper_aberto_e_fechado_distingue_operacoes(self):
        pagina = bot.PAGINA_HTML
        self.assertIn("paper_status:'open'", pagina)
        self.assertIn("a.paper_status='closed'", pagina)
        self.assertIn("Paper aberta", pagina)
        self.assertIn("Paper fechada", pagina)
        self.assertIn("Posição Paper:", pagina)

    def test_novas_descobertas_e_reanalises_tem_contadores_separados(self):
        b = object.__new__(bot.Bot)
        b.estado = {"scanner_counters": {"daily": {}}}
        b._inicializar_contadores_scanner()
        b._contar_novo_mint(MINT)
        b._contar_resultado_analise({"status": "pending_data"}, reanalisado=False)
        b._contar_resultado_analise({"status": "rejected"}, reanalisado=True)
        counts = b._contadores_scanner_publicos()["session"]
        self.assertEqual(counts["unique_new"], 1)
        self.assertEqual(counts["reanalyzed"], 1)
        self.assertEqual(counts["pending_data"], 1)
        self.assertEqual(counts["rejected"], 1)

    def test_reiniciar_sessao_preserva_unicidade_diaria(self):
        estado = {"scanner_counters": {"daily": {}}}
        b1 = object.__new__(bot.Bot); b1.estado = estado
        b1._inicializar_contadores_scanner(); b1._contar_novo_mint(MINT)
        self.assertEqual(b1._contadores_scanner_publicos()["daily"]["unique_new"], 1)
        b2 = object.__new__(bot.Bot); b2.estado = estado
        b2._inicializar_contadores_scanner(); b2._contar_novo_mint(MINT)
        self.assertEqual(b2._contadores_scanner_publicos()["daily"]["unique_new"], 1)
        self.assertEqual(b2._contadores_scanner_publicos()["session"]["unique_new"], 1)


if __name__ == "__main__":
    unittest.main()
