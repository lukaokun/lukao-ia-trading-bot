# -*- coding: utf-8 -*-
"""Testes offline: aprovar/recusar propostas Paper no dashboard (com confirmacao e chave da sessao)."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import bot
import paper_trading_limits as limits_module
from teste_paper_trading_limits import executor_fixture, sample_pair


class DecisaoDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        politica = patch.object(limits_module, "APPROVED_MAX_SLIPPAGE_PCT", Decimal("95"))
        politica.start()
        self.addCleanup(politica.stop)

    def proposta(self, manager, mint="mint-dash"):
        executor, _state, _cfg = executor_fixture(self.temp.name, capital=100)
        amount, reason = executor.pode_comprar()
        self.assertGreater(amount, 0, reason)
        plan = executor.preparar_execucao(mint, sample_pair("DASH"), amount)
        return manager.propose(mint, sample_pair("DASH"), {"pontuacao": 88}, amount, plan)

    # ---------- gestor ----------
    def test_aprovar_no_dashboard_gera_permissao_unica(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        p = self.proposta(manager)
        self.assertEqual(manager.decidir_dashboard(p.proposal_id, "aprovar")[0], True)
        self.assertEqual(manager.decidir_dashboard(p.proposal_id, "aprovar"),
                         (False, "proposta ja decidida no dashboard"))
        self.assertFalse(manager.decidir_dashboard(p.proposal_id, "recusar")[0])
        _p, token, aceite, motivo = manager.drain_results()[0]
        self.assertTrue(aceite)
        self.assertTrue(token)
        self.assertEqual(motivo, "aprovada no dashboard")

    def test_recusar_no_dashboard(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        p = self.proposta(manager)
        self.assertEqual(manager.decidir_dashboard(p.proposal_id, "recusar"), (True, "recusada"))
        _p, token, aceite, motivo = manager.drain_results()[0]
        self.assertFalse(aceite)
        self.assertIsNone(token)
        self.assertEqual(motivo, "recusada no dashboard")
        self.assertEqual(manager.public_pending(), [])

    def test_recusa_decisoes_invalidas_expiradas_ou_desconhecidas(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        p = self.proposta(manager)
        self.assertEqual(manager.decidir_dashboard(p.proposal_id, "comprar"), (False, "decisao invalida"))
        self.assertFalse(manager.decidir_dashboard("0" * 32, "aprovar")[0])
        manager._pending[p.proposal_id] = replace(p, expires_monotonic=time.monotonic() - 1)
        self.assertEqual(manager.decidir_dashboard(p.proposal_id, "aprovar"), (False, "proposta expirada"))

    # ---------- endpoint HTTP ----------
    def servidor(self, manager, modo="paper", emergencia=False):
        class Stub:
            cfg = {"painel_web": True, "porta_web": 0, "painel_web_rede": False, "mode": modo,
                   "live_trading_enabled": False, "manual_confirmation_required": True}
            snapshot = json.dumps({"propostas_paper": []})
            dashboard_token = "chave-de-teste-secreta"
            manual_approvals = manager

            def _emergency_stop_is_active(self):
                return emergencia

        class AnaliseFalsa(bot.AnaliseManual):
            def _analisar(self, endereco):
                return {"mint": endereco, "simbolo": "TST", "status": "rejected", "pontuacao": 55}
        stub = Stub()
        stub.acompanhar_pedidos = []
        stub.pedir_acompanhamento = lambda mint, origem: (stub.acompanhar_pedidos.append((mint, origem)),
                                                          (True, "Vou acompanhar"))[1]
        self.stub = stub
        stub.info_acompanhados = [{"simbolo": "VEXA", "agora_pct": 12.5}]
        stub.manuais = []
        stub.pedir_compra_manual = lambda mint, origem: (stub.manuais.append(("comprar", mint, origem)), (True, "proposta"))[1]
        stub.pedir_venda = lambda mint, origem, parcial=False: (
            stub.manuais.append(("recuperar" if parcial else "vender", mint, origem)), (True, "pedido"))[1]
        stub.manuais_terminados = []
        stub.analise_manual = AnaliseFalsa({"analise_manual": True, "analise_manual_intervalo_seg": 0}, lambda: None)
        bot.iniciar_painel_web(stub)
        srv = stub.servidor_web
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def post(self, porta, corpo, token="chave-de-teste-secreta", origin=None, host=None, caminho="/api/proposta/decisao"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-MemeBot-Token"] = token
        headers["Origin"] = origin if origin is not None else f"http://127.0.0.1:{porta}"
        if host:
            headers["Host"] = host
        pedido = Request(f"http://127.0.0.1:{porta}{caminho}", data=json.dumps(corpo).encode(), method="POST", headers=headers)
        try:
            with urlopen(pedido, timeout=3) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, {}

    def test_endpoint_exige_chave_origem_e_host_locais(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        p = self.proposta(manager)
        porta = self.servidor(manager)
        corpo = {"proposal_id": p.proposal_id, "decisao": "aprovar"}
        self.assertEqual(self.post(porta, corpo, token=None)[0], 403)
        self.assertEqual(self.post(porta, corpo, token="errada")[0], 403)
        self.assertEqual(self.post(porta, corpo, origin="https://site-malicioso.example")[0], 403)
        self.assertEqual(self.post(porta, corpo, host="evil.example:80")[0], 403)
        self.assertEqual(self.post(porta, {"proposal_id": "xyz", "decisao": "aprovar"})[0], 400)
        self.assertEqual(self.post(porta, {"proposal_id": p.proposal_id, "decisao": "vender"})[0], 400)
        self.assertEqual(self.post(porta, corpo, caminho="/api/comprar")[0], 404)
        self.assertEqual(manager.drain_results(), [])  # nada foi decidido pelos pedidos recusados
        estado, resposta = self.post(porta, corpo)
        self.assertEqual((estado, resposta["ok"]), (200, True))
        _p, token, aceite, motivo = manager.drain_results()[0]
        self.assertTrue(aceite and token)
        self.assertEqual(motivo, "aprovada no dashboard")
        self.assertEqual(self.post(porta, corpo)[0], 409)  # uso unico

    def test_endpoint_recusa_fora_de_paper_e_com_paragem_de_emergencia(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        p = self.proposta(manager)
        corpo = {"proposal_id": p.proposal_id, "decisao": "aprovar"}
        self.assertEqual(self.post(self.servidor(manager, modo="live"), corpo)[0], 409)
        self.assertEqual(self.post(self.servidor(manager, emergencia=True), corpo)[0], 409)
        porta = self.servidor(manager, emergencia=True)
        self.assertEqual(self.post(porta, {"proposal_id": p.proposal_id, "decisao": "recusar"})[0], 200)
        self.assertFalse(manager.drain_results()[0][2])

    def test_analise_manual_no_dashboard(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        porta = self.servidor(manager)
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

        def get(q, token="chave-de-teste-secreta", host=None):
            headers = {"X-MemeBot-Token": token} if token else {}
            if host:
                headers["Host"] = host
            try:
                with urlopen(Request(f"http://127.0.0.1:{porta}/api/analisar?q={q}", headers=headers), timeout=3) as r:
                    return r.status, json.load(r)
            except HTTPError as e:
                return e.code, json.load(e)
        self.assertEqual(get(mint, token=None)[0], 403)
        self.assertEqual(get(mint, token="errada")[0], 403)
        self.assertEqual(get(mint, host="evil.example:80")[0], 403)
        self.assertEqual(get("nada")[0], 400)
        estado, resposta = get(mint)
        self.assertEqual((estado, resposta["ok"], resposta["resultado"]["simbolo"]), (200, True, "TST"))
        self.assertEqual(manager.drain_results(), [])                    # nada foi decidido nem comprado

    def test_acompanhar_no_dashboard(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        porta = self.servidor(manager)
        corpo = {"mint": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"}
        self.assertEqual(self.post(porta, corpo, token=None, caminho="/api/acompanhar")[0], 403)
        self.assertEqual(self.post(porta, corpo, origin="https://site-malicioso.example", caminho="/api/acompanhar")[0], 403)
        self.assertEqual(self.post(porta, corpo, host="evil.example:80", caminho="/api/acompanhar")[0], 403)
        self.assertEqual(self.stub.acompanhar_pedidos, [])
        estado, resposta = self.post(porta, corpo, caminho="/api/acompanhar")
        self.assertEqual((estado, resposta["ok"]), (200, True))
        self.assertEqual(self.stub.acompanhar_pedidos, [(corpo["mint"], "dashboard")])
        self.assertEqual(manager.drain_results(), [])                    # nada foi decidido nem comprado

    def test_acompanhados_no_dashboard(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        porta = self.servidor(manager)

        def get(token="chave-de-teste-secreta", host=None):
            headers = {"X-MemeBot-Token": token} if token else {}
            if host:
                headers["Host"] = host
            try:
                with urlopen(Request(f"http://127.0.0.1:{porta}/api/acompanhados", headers=headers), timeout=3) as r:
                    return r.status, json.load(r)
            except HTTPError as e:
                return e.code, json.load(e)
        self.assertEqual(get(token=None)[0], 403)
        self.assertEqual(get(host="evil.example:80")[0], 403)
        estado, j = get()
        self.assertEqual((estado, j["ok"], j["ativos"][0]["simbolo"]), (200, True, "VEXA"))

    def test_manual_no_dashboard(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        porta = self.servidor(manager)
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        self.assertEqual(self.post(porta, {"acao": "comprar", "mint": mint}, token=None, caminho="/api/manual")[0], 403)
        self.assertEqual(self.post(porta, {"acao": "comprar", "mint": mint}, origin="https://x.example", caminho="/api/manual")[0], 403)
        self.assertEqual(self.post(porta, {"acao": "levantar", "mint": mint}, caminho="/api/manual")[0], 400)
        self.assertEqual(self.stub.manuais, [])
        for acao in ("comprar", "vender", "recuperar"):
            self.assertEqual(self.post(porta, {"acao": acao, "mint": mint}, caminho="/api/manual")[0], 200)
        self.assertEqual(self.stub.manuais, [("comprar", mint, "dashboard"), ("vender", mint, "dashboard"),
                                             ("recuperar", mint, "dashboard")])
        self.assertEqual(manager.drain_results(), [])                      # o dashboard nunca aprova nada sozinho

    def test_pagina_recebe_chave_da_sessao_e_so_tem_um_post(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        porta = self.servidor(manager)
        with urlopen(f"http://127.0.0.1:{porta}/", timeout=3) as r:
            pagina = r.read().decode("utf-8")
        if "Neural Sphere" not in pagina:
            self.skipTest("dashboard neural ausente nesta copia")
        self.assertIn("const TOKEN='chave-de-teste-secreta'", pagina)
        self.assertNotIn("__MEMEBOT_TOKEN__", pagina)
        self.assertEqual(pagina.count("method:'POST'"), 3)          # decisao + acompanhar + manual (so Paper)
        self.assertIn("'/api/proposta/decisao'", pagina)
        self.assertIn("Sim, aprovar", pagina)
        self.assertIn("Sim, recusar", pagina)
        with urlopen(f"http://127.0.0.1:{porta}/classico", timeout=3) as r:
            self.assertNotIn("chave-de-teste-secreta", r.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
