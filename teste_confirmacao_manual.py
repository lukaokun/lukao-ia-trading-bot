import csv
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import bot
from teste_paper_trading_limits import executor_fixture, sample_pair


class ManualApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def new_executor(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return executor_fixture(folder.name, capital=100)[0]

    def make_proposal(self, manager, mint, pair, analysis, amount=5, executor=None):
        executor = executor or self.new_executor()
        plan = executor.preparar_execucao(mint, pair, amount)
        proposal = manager.propose(mint, pair, analysis, amount, plan)
        return proposal, executor

    @staticmethod
    def pair_with_plan(executor, mint, pair, amount):
        prepared = dict(pair)
        prepared["_paper_execution_plan"] = executor.preparar_execucao(
            mint, pair, amount).canonical_json
        return prepared

    def test_valid_approval_executes_exactly_once(self):
        executor, state, _cfg = executor_fixture(self.temp.name, capital=100)
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        executor.bot.manual_approvals = manager
        pair = sample_pair("VALID")
        analysis = {"pontuacao": 90}
        amount, reason = executor.pode_comprar()
        self.assertGreater(amount, 0, reason)
        plan = executor.preparar_execucao("mint-valid", pair, amount)
        proposal = manager.propose("mint-valid", pair, analysis, amount, plan)
        public = proposal.public_fields()
        self.assertEqual(manager.public_pending()[0], public)
        manager.start()
        with patch.object(bot.ManualApprovalManager, "_read_line_until",
                          return_value=f"APROVAR {proposal['proposal_id']} {proposal['confirmation_code']}"):
            manager._requests.put(proposal["proposal_id"])
            result = manager._results.get(timeout=1)
        manager._results.put(result)
        proposal_data, token, accepted, _ = manager.drain_results()[0]
        self.assertTrue(accepted)
        position = executor.comprar("mint-valid", pair, analysis, token)
        self.assertIsNotNone(position)
        self.assertIn("mint-valid", state["posicoes"])
        self.assertEqual(Decimal(str(position["custo"])), Decimal(public["debit_total_usd"]))
        self.assertEqual(Decimal(str(100 - state["saldo"])), Decimal(public["debit_total_usd"]))
        self.assertEqual(Decimal(str(position["preco_entrada"])), Decimal(public["reference_price_usd"]))
        self.assertEqual(Decimal(str(position["preco_efetivo"])), Decimal(public["effective_price_usd"]))
        self.assertEqual(Decimal(str(position["tokens"])), Decimal(public["net_tokens"]))
        with open(_cfg["ficheiro_trades"], newline="", encoding="utf-8-sig") as source:
            trade = next(csv.DictReader(source))
        self.assertEqual(trade["valor_usd"], public["debit_total_usd"])
        self.assertEqual(trade["debito_total_usd"], public["debit_total_usd"])
        self.assertEqual(trade["preco_mercado"], public["reference_price_usd"])
        self.assertEqual(trade["preco_efetivo"], public["effective_price_usd"])
        self.assertEqual(trade["tokens"], public["net_tokens"])
        self.assertEqual(trade["quantidade_liquida"], public["net_tokens"])
        self.assertEqual(trade["taxa_estimada_usd"], public["estimated_fee_usd"])
        self.assertEqual(trade["slippage_total_pct"], public["slippage_total_pct"])
        self.assertEqual(trade["impacto_mercado_pct"], public["market_impact_pct"])
        self.assertIsNone(executor.comprar("mint-valid-2", sample_pair("SECOND"), analysis, token))
        self.assertEqual(len(state["posicoes"]), 1)

    def test_wrong_code_is_rejected(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        proposal, _executor = self.make_proposal(
            manager, "mint-wrong", sample_pair(), {"pontuacao": 80, "motivo": "fixture"})
        manager.start()
        with patch.object(bot.ManualApprovalManager, "_read_line_until",
                          return_value=f"APROVAR {proposal['proposal_id']} WRONG"):
            manager._requests.put(proposal["proposal_id"])
            result = manager._results.get(timeout=1)
        manager._results.put(result)
        _p, token, accepted, _reason = manager.drain_results()[0]
        self.assertFalse(accepted)
        self.assertIsNone(token)

    def test_confirmation_code_cannot_issue_a_second_permit(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        proposal, executor = self.make_proposal(manager, "mint-once", sample_pair(), {"pontuacao": 80})
        answer = f"APROVAR {proposal['proposal_id']} {proposal['confirmation_code']}"
        manager._results.put((proposal["proposal_id"], answer))
        _p, token, accepted, _ = manager.drain_results()[0]
        self.assertTrue(accepted)
        manager._results.put((proposal["proposal_id"], answer))
        self.assertEqual(manager.drain_results(), [])
        current = self.pair_with_plan(executor, "mint-once", proposal["pair"], 5)
        self.assertTrue(manager.consume(token, "mint-once", current, 5))
        self.assertFalse(manager.consume(token, "mint-once", current, 5))

    def test_concurrent_consumption_allows_exactly_one_use(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        pair = sample_pair("RACE")
        proposal, executor = self.make_proposal(manager, "mint-race", pair, {"pontuacao": 80})
        manager._results.put((proposal.proposal_id,
                              f"APROVAR {proposal.proposal_id} {proposal.confirmation_code}"))
        _p, token, accepted, _ = manager.drain_results()[0]
        self.assertTrue(accepted)
        barrier = threading.Barrier(3)
        results = []

        def consume_once():
            barrier.wait()
            results.append(manager.consume(
                token, "mint-race", self.pair_with_plan(executor, "mint-race", pair, 5), 5))

        threads = [threading.Thread(target=consume_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sorted(results), [False, True])

    def test_wrong_full_proposal_id_is_rejected(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        proposal, _executor = self.make_proposal(manager, "mint-id", sample_pair("ID"), {"pontuacao": 80})
        answer = f"APROVAR {proposal['proposal_id'][:-1]}X {proposal['confirmation_code']}"
        manager._results.put((proposal["proposal_id"], answer))
        _p, token, accepted, reason = manager.drain_results()[0]
        self.assertFalse(accepted)
        self.assertIsNone(token)
        self.assertIn("ID", reason)

    def test_code_from_another_proposal_cannot_approve(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        first, _ = self.make_proposal(manager, "mint-a", sample_pair("A"), {"pontuacao": 80})
        second, _ = self.make_proposal(manager, "mint-b", sample_pair("B"), {"pontuacao": 80})
        manager._results.put((second["proposal_id"],
                              f"APROVAR {second['proposal_id']} {first['confirmation_code']}"))
        _p, token, accepted, _ = manager.drain_results()[0]
        self.assertFalse(accepted)
        self.assertIsNone(token)

    def test_canonical_public_and_terminal_proposal_values_match(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        pair = sample_pair("CANON")
        pair["priceUsd"] = "0.0000012300"
        proposal, _executor = self.make_proposal(
            manager, "mint-canonical", pair, {"pontuacao": 91, "motivo": "fixture"},
            Decimal("4.125000"))
        public = proposal.public_fields()
        lines = proposal.terminal_lines()
        self.assertEqual(public["proposal_id"], proposal.proposal_id)
        self.assertEqual(public["mint"], "mint-canonical")
        self.assertEqual(public["pair_address"], pair["pairAddress"])
        self.assertEqual(public["amount_usd"], "4.125")
        self.assertEqual(public["price_usd"], "0.00000123")
        self.assertEqual(public["debit_total_usd"], "4.125")
        self.assertEqual(public["reference_price_usd"], "0.00000123")
        self.assertEqual(public["slippage_base_applied_pct"], "2")
        self.assertIn(
            f"Slippage base aplicado (%): {public['slippage_base_applied_pct']}", lines)
        self.assertIn("Slippage base aplicado (%): '+v(p.slippage_base_applied_pct)",
                      bot.PAGINA_HTML)
        self.assertIn("Preco efetivo simulado USD: " + public["effective_price_usd"], lines)
        self.assertIn(f"Debito total USD: {public['debit_total_usd']}", lines)
        self.assertIn(f"Preco de referencia USD: {public['reference_price_usd']}", lines)
        self.assertIn(f"ID completo: {public['proposal_id']}", lines)
        # Returned mappings and nested JSON copies cannot mutate the frozen proposal.
        pair_copy = proposal["pair"]
        pair_copy["pairAddress"] = "tampered"
        self.assertEqual(proposal["pair"]["pairAddress"], pair["pairAddress"])
        with self.assertRaises((AttributeError, TypeError)):
            proposal.amount_usd = "0"

    def test_zero_applied_base_slippage_matches_proposal_terminal_and_dashboard(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        pair = sample_pair("ZERO-SLIPPAGE")
        pair["liquidity"]["usd"] = 0
        proposal, _executor = self.make_proposal(
            manager, "mint-zero-slippage", pair, {"pontuacao": 80}, 5)
        public = proposal.public_fields()
        self.assertEqual(public["slippage_base_applied_pct"], "0")
        self.assertIn("Slippage base aplicado (%): 0", proposal.terminal_lines())
        self.assertIn("Slippage base aplicado (%): '+v(p.slippage_base_applied_pct)",
                      bot.PAGINA_HTML)

    def test_changed_mint_pair_amount_price_liquidity_or_conditions_consumes_and_rejects(self):
        cases = (
            ("mint-other", None, 5),
            ("mint-bound", {"pairAddress": "other", "priceUsd": "0.25",
                            "liquidity": {"usd": 100000}}, 5),
            ("mint-bound", None, Decimal("4.99")),
            ("mint-bound", {"pairAddress": "pair", "priceUsd": "0.2500001",
                            "liquidity": {"usd": 100000}}, 5),
            ("mint-bound", {"pairAddress": "pair", "priceUsd": "0.25",
                            "liquidity": {"usd": 99999}}, 5),
        )
        for index, (mint, replacement, amount) in enumerate(cases):
            with self.subTest(index=index):
                manager = bot.ManualApprovalManager()
                self.addCleanup(manager.close)
                pair = {"pairAddress": "pair", "priceUsd": "0.25",
                        "liquidity": {"usd": 100000}, "baseToken": {"symbol": "X"}}
                executor = self.new_executor()
                proposal, _ = self.make_proposal(
                    manager, "mint-bound", pair, {"pontuacao": 80}, 5, executor)
                answer = f"APROVAR {proposal.proposal_id} {proposal.confirmation_code}"
                manager._results.put((proposal.proposal_id, answer))
                _p, token, accepted, _ = manager.drain_results()[0]
                self.assertTrue(accepted)
                current_pair = replacement or pair
                current_pair = dict(current_pair)
                current_pair["liquidity"] = dict(current_pair["liquidity"])
                current_pair["_paper_execution_plan"] = executor.preparar_execucao(
                    mint, current_pair, amount).canonical_json
                self.assertFalse(manager.consume(token, mint, current_pair, amount))

    def test_changed_execution_conditions_reject_and_consume_permit(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        pair = sample_pair("TERMS")
        executor = self.new_executor()
        proposal, _ = self.make_proposal(manager, "mint-terms", pair, {"pontuacao": 80}, 5, executor)
        manager._results.put((proposal.proposal_id,
                              f"APROVAR {proposal.proposal_id} {proposal.confirmation_code}"))
        _p, token, accepted, _ = manager.drain_results()[0]
        self.assertTrue(accepted)
        mutated = dict(pair)
        executor.cfg["slippage_pct"] += 0.1
        mutated["_paper_execution_plan"] = executor.preparar_execucao(
            "mint-terms", pair, 5).canonical_json
        self.assertFalse(manager.consume(token, "mint-terms", mutated, 5))
        self.assertFalse(manager.consume(token, "mint-terms", self.pair_with_plan(
            executor, "mint-terms", pair, 5), 5))

    def test_cost_plan_matches_existing_slippage_function_and_fallbacks(self):
        executor = self.new_executor()
        pair = sample_pair("IMPACT", price="0.02", liquidity=1000)
        amount = 5
        plan = executor.preparar_execucao("mint-impact", pair, amount).fields()
        expected_total = bot.slippage_total(executor.cfg, amount, 1000)
        self.assertEqual(Decimal(plan["slippage_total_pct"]), Decimal(str(expected_total * 100)))
        self.assertEqual(Decimal(plan["slippage_base_pct"]), Decimal("2"))
        self.assertEqual(Decimal(plan["slippage_base_applied_pct"]), Decimal("2"))
        self.assertEqual(Decimal(plan["market_impact_pct"]),
                         Decimal(str(max(0.0, expected_total - 0.02) * 100.0)))
        self.assertEqual(Decimal(plan["effective_price_usd"]),
                         Decimal(str(float(pair["priceUsd"]) * (1 + expected_total))))
        self.assertEqual(Decimal(plan["estimated_fee_usd"]), Decimal("0.05"))
        self.assertEqual(Decimal(plan["debit_total_usd"]), Decimal("5"))
        self.assertEqual(Decimal(plan["net_tokens"]), Decimal(str(
            (amount - 0.05) / float(plan["effective_price_usd"]))))

        capped = executor.preparar_execucao(
            "mint-cap", sample_pair("CAP", price="0.02", liquidity=0.1), amount).fields()
        self.assertEqual(Decimal(capped["slippage_total_pct"]), Decimal("95"))
        self.assertEqual(Decimal(capped["slippage_base_applied_pct"]), Decimal("2"))
        self.assertEqual(Decimal(capped["market_impact_pct"]), Decimal("93"))
        self.assertFalse(capped["impact_fallback"])

        zero_pair = sample_pair("ZERO", price="0.02", liquidity=0)
        zero = executor.preparar_execucao("mint-zero", zero_pair, amount).fields()
        self.assertEqual(zero["liquidity_status"], "observed_zero")
        self.assertEqual(zero["liquidity_observed_usd"], "0")
        self.assertIsNone(zero["market_impact_pct"])
        self.assertEqual(Decimal(zero["slippage_total_pct"]), Decimal("95"))
        self.assertTrue(zero["impact_fallback"])

        missing_pair = sample_pair("MISSING", price="0.02", liquidity=1000)
        del missing_pair["liquidity"]
        missing = executor.preparar_execucao("mint-missing", missing_pair, amount).fields()
        self.assertEqual(missing["liquidity_status"], "unavailable")
        self.assertIsNone(missing["liquidity_observed_usd"])
        self.assertIsNone(missing["market_impact_pct"])
        self.assertTrue(missing["impact_fallback"])

        executor.cfg["simular_impacto_preco"] = False
        disabled = executor.preparar_execucao("mint-disabled", zero_pair, amount).fields()
        self.assertEqual(Decimal(disabled["slippage_total_pct"]), Decimal("2"))
        self.assertEqual(Decimal(disabled["market_impact_pct"]), Decimal("0"))
        self.assertFalse(disabled["impact_fallback"])

    def test_dashboard_public_proposal_never_contains_confirmation_code(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        proposal, _executor = self.make_proposal(
            manager, "mint-public", sample_pair("PUBLIC"), {"pontuacao": 80})
        public = manager.public_pending()[0]
        self.assertEqual(public, proposal.public_fields())
        self.assertNotIn("confirmation_code", public)
        self.assertNotIn(proposal.confirmation_code, json.dumps(public))
        self.assertNotIn("confirmation_code", bot.PAGINA_HTML)

    def test_expired_proposal_is_rejected(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        proposal, _executor = self.make_proposal(
            manager, "mint-expired", sample_pair(), {"pontuacao": 80})
        with manager._lock:
            manager._pending[proposal["proposal_id"]] = replace(
                proposal, expires_monotonic=time.monotonic() - 1)
        manager.start()
        manager._requests.put(proposal["proposal_id"])
        result = manager._results.get(timeout=1)
        manager._results.put(result)
        _p, token, accepted, reason = manager.drain_results()[0]
        self.assertFalse(accepted)
        self.assertIsNone(token)
        self.assertIn("expirada", reason)

    def test_restart_invalidates_pending_proposal(self):
        first = bot.ManualApprovalManager()
        proposal, _executor = self.make_proposal(
            first, "mint-restart", sample_pair(), {"pontuacao": 80})
        first.close()
        restarted = bot.ManualApprovalManager()
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.public_pending(), [])
        self.assertFalse(restarted.consume("old-permit", "mint-restart", proposal["pair"], 5))

    def test_persistence_failure_consumes_approval(self):
        executor, state, _cfg = executor_fixture(self.temp.name, capital=100)
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        executor.bot.manual_approvals = manager
        pair = sample_pair("FAIL")
        amount, _ = executor.pode_comprar()
        proposal = manager.propose("mint-fail", pair, {"pontuacao": 90, "motivo": "fixture"},
                                   amount, executor.preparar_execucao("mint-fail", pair, amount))
        manager.start()
        with patch.object(bot.ManualApprovalManager, "_read_line_until",
                          return_value=f"APROVAR {proposal['proposal_id']} {proposal['confirmation_code']}"):
            manager._requests.put(proposal["proposal_id"])
            result = manager._results.get(timeout=1)
        manager._results.put(result)
        _p, token, accepted, _ = manager.drain_results()[0]
        self.assertTrue(accepted)
        with patch.object(executor.bot.paper_operations, "commit", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                executor.comprar("mint-fail", pair, {"pontuacao": 90}, token)
        self.assertIsNone(executor.comprar("mint-fail", pair, {"pontuacao": 90}, token))
        self.assertNotIn("mint-fail", state["posicoes"])

    def test_no_answer_rejects_and_risk_cycle_remains_callable(self):
        manager = bot.ManualApprovalManager()
        self.addCleanup(manager.close)
        proposal, _executor = self.make_proposal(
            manager, "mint-timeout", sample_pair(), {"pontuacao": 80})
        manager.start()
        waiting = threading.Event()
        release = threading.Event()

        def no_answer(_deadline, _stop):
            waiting.set()
            release.wait(0.5)
            return None

        with patch.object(bot.ManualApprovalManager, "_read_line_until", side_effect=no_answer):
            manager._requests.put(proposal["proposal_id"])
            self.assertTrue(waiting.wait(1))
            started = time.monotonic()
            # Empty-position risk monitoring returns without waiting for terminal input.
            risk_bot = type("RiskStub", (), {"estado": {"posicoes": {}, "percursos": {}}})()
            self.assertEqual(bot.Bot.ciclo_risco(risk_bot), 0)
            self.assertLess(time.monotonic() - started, 0.25)
            release.set()
            result = manager._results.get(timeout=1)
        manager._results.put(result)
        _p, token, accepted, _reason = manager.drain_results()[0]
        self.assertFalse(accepted)
        self.assertIsNone(token)

    def test_executor_blocks_without_approval(self):
        executor, state, _cfg = executor_fixture(self.temp.name, capital=100)
        executor.bot.manual_approvals = bot.ManualApprovalManager()
        self.addCleanup(executor.bot.manual_approvals.close)
        self.assertIsNone(executor.comprar("mint-no-approval", sample_pair(), {"pontuacao": 90}))
        self.assertEqual(state["posicoes"], {})


class DashboardBindTests(unittest.TestCase):
    def test_dashboard_is_local_only_and_has_no_approval_or_trade_post(self):
        class Stub:
            cfg = {"painel_web": True, "porta_web": 0, "painel_web_rede": True}
            snapshot = json.dumps({"propostas_paper": []})

        webbot = Stub()
        address = bot.iniciar_painel_web(webbot)
        self.assertTrue(address.startswith("http://localhost:"))
        server = webbot.servidor_web
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        with urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/estado", timeout=2) as response:
            self.assertEqual(json.load(response), {"propostas_paper": []})
        for endpoint in ("/api/aprovar", "/api/comprar"):
            request = Request(f"http://127.0.0.1:{server.server_address[1]}{endpoint}",
                              data=b"request", method="POST")
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertIn(error.exception.code, (404, 501))


if __name__ == "__main__":
    unittest.main()
