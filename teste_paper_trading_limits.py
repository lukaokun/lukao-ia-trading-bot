import json
import csv
import io
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests

import bot
from paper_trading_limits import (
    EcbRateError, ECB_SOURCE, OperationalConfigError, ProcessInstanceLock, ResetPathError,
    canonical_reset_paths, canonical_state_path,
    fetch_ecb_rate, load_operational_config, parse_ecb_rate_csv,
    usd_cap_from_eur, validate_locked_session_rate, validate_rate,
)
from paper_operations import PaperOperationError, PaperOperationStore, atomic_write_json


FIXTURE_CSV = """KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE
EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-13,1.1052
"""


class DummyTelegram:
    def enviar(self, _message):
        pass


class DummyBot:
    def __init__(self, folder):
        self.acoes = []
        self.telegram = DummyTelegram()
        self._fx_rate_persisted = True
        self.manual_approvals = _FixtureApprovals()
        self.paper_operations = PaperOperationStore(
            os.path.join(folder, "executor_state.json"), os.path.join(folder, "trades.csv"),
            bot.CABECALHO_CSV,
        )

    def acao(self, message):
        self.acoes.append(message)


class _FixtureApprovals:
    """Explicit auto-approval test double; never used by production Bot."""
    def consume(self, token, mint, par, amount=None):
        return True


def executor_fixture(folder, capital=1000.0, cash=None, operational=True, positions=None):
    state = {
        "saldo_inicial": capital,
        "saldo": capital if cash is None else cash,
        "posicoes": positions or {},
        "operational_session": {
            "id": "fixture-session",
            "fx_usd_per_eur": "1.2",
            "fx_reference_date": date.today().isoformat(),
            "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(),
            "fx_source": ECB_SOURCE,
        },
        "paper_state_revision": 0,
        "paper_ops_applied": [],
        "stats": {"fechados": 0, "ganhos": 0, "perdas": 0, "pnl_total": 0.0,
                  "custos_total": 0.0, "melhor": 0.0, "pior": 0.0, "por_motivo": {}},
        "sinais": [],
    }
    cfg = {
        "max_posicoes_abertas": 1,
        "parar_compras_se_perda_pct": 0,
        "exposicao_maxima_pct": 55,
        "tamanho_posicao_pct": 12,
        "posicao_minima_usd": 1.0,
        "slippage_pct": 2.0,
        "taxa_pct": 1.0,
        "simular_impacto_preco": True,
        "ficheiro_trades": os.path.join(folder, "trades.csv"),
        "limites_operacionais_ativos": operational,
        "mode": "paper", "live_trading_enabled": False,
        "private_keys_in_application": False, "manual_confirmation_required": True,
        "erro_config_operacional": None,
        "max_eur_per_operation": Decimal("5"),
    }
    dummy = DummyBot(folder)
    # Mirror the production startup invariant: the initial durable state exists
    # before an operational transaction can be attempted.
    atomic_write_json(dummy.paper_operations.state_path, state)
    return bot.Executor(cfg, state, dummy), state, cfg


def sample_pair(symbol="COIN", price="0.01", liquidity=50000):
    return {
        "baseToken": {"symbol": symbol}, "priceUsd": price,
        "liquidity": {"usd": liquidity}, "pairAddress": "PAIR-" + symbol,
        "url": "https://example.invalid/" + symbol,
    }


class PaperTradingLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write_config(self, **overrides):
        data = {
            "mode": "paper", "live_trading_enabled": False,
            "private_keys_in_application": False,
            "manual_confirmation_required": True,
            "max_eur_per_operation": 5.0, "max_open_positions": 1,
        }
        data.update(overrides)
        path = Path(self.temp.name) / "config_operacoes.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def reset_fixture(self, folder):
        state_path = os.path.join(folder, "estado.json")
        trades_path = os.path.join(folder, "trades.csv")
        cfg = dict(bot.CONFIG, ficheiro_estado=state_path, ficheiro_trades=trades_path,
                   ficheiro_log=os.path.join(folder, "bot.log"),
                   ficheiro_percursos=os.path.join(folder, "percursos.jsonl"), painel_web=False)
        atomic_write_json(state_path, bot.estado_novo(cfg))
        with open(trades_path, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=bot.CABECALHO_CSV, lineterminator="\n")
            writer.writeheader()
        return cfg

    def test_operational_config_is_loaded_and_validated(self):
        limits = load_operational_config(self.write_config())
        self.assertEqual(limits["max_eur_per_operation"], Decimal("5.0"))
        self.assertEqual(limits["max_open_positions"], 1)
        for override in (
            {"mode": "live"}, {"live_trading_enabled": True},
            {"private_keys_in_application": True}, {"max_open_positions": 2},
            {"manual_confirmation_required": False},
            {"max_eur_per_operation": 5.01}, {"max_eur_per_operation": "NaN"},
        ):
            with self.subTest(override=override), self.assertRaises(OperationalConfigError):
                load_operational_config(self.write_config(**override))

    def test_project_operational_config_keeps_paper_and_requested_limits(self):
        project_config = Path(__file__).with_name("config_operacoes.json")
        limits = load_operational_config(project_config)
        self.assertEqual(limits["mode"], "paper")
        self.assertIs(limits["live_trading_enabled"], False)
        self.assertEqual(limits["max_eur_per_operation"], Decimal("5.0"))
        self.assertEqual(limits["max_open_positions"], 1)

    def test_ecb_csv_parses_usd_per_eur_and_accepts_fresh_reference_day(self):
        data = parse_ecb_rate_csv(FIXTURE_CSV, today=date(2026, 11, 13))
        self.assertEqual(data["rate_usd_per_eur"], "1.1052")
        self.assertEqual(data["reference_date"], "2026-11-13")
        self.assertEqual(data["source"], ECB_SOURCE)
        self.assertEqual(usd_cap_from_eur("5", data["rate_usd_per_eur"]), Decimal("5.5260"))

    def test_ecb_missing_wrong_malformed_stale_or_future_rate_is_rejected(self):
        for payload in (
            "", "TIME_PERIOD,OBS_VALUE\n2026-11-13,1.1\n",
            FIXTURE_CSV.replace(",USD,EUR,", ",GBP,EUR,"),
            FIXTURE_CSV.replace(",1.1052", ",NaN"),
        ):
            with self.subTest(payload=payload[:30]), self.assertRaises(EcbRateError):
                parse_ecb_rate_csv(payload, today=date(2026, 11, 13))
        stale = FIXTURE_CSV.replace("2026-11-13", "2026-11-01")
        future = FIXTURE_CSV.replace("2026-11-13", "2026-11-14")
        for payload in (stale, future):
            with self.subTest(payload=payload), self.assertRaises(EcbRateError):
                parse_ecb_rate_csv(payload, today=date(2026, 11, 13))
        for value in ("0", "-1", "Infinity", "NaN"):
            with self.subTest(value=value), self.assertRaises(EcbRateError):
                validate_rate(value, "2026-11-13", today=date(2026, 11, 13))

    def test_ecb_http_429_and_timeout_fail_closed(self):
        class Response:
            status_code = 429
            text = FIXTURE_CSV

        with self.assertRaises(EcbRateError):
            fetch_ecb_rate(get=lambda *a, **k: Response(), today=date(2026, 11, 13))

        def timeout(*_args, **_kwargs):
            raise requests.Timeout("fixture timeout")

        with self.assertRaises(EcbRateError):
            fetch_ecb_rate(get=timeout, today=date(2026, 11, 13))

        class GoodResponse:
            status_code = 200
            text = FIXTURE_CSV

        result = fetch_ecb_rate(get=lambda *a, **k: GoodResponse(), today=date(2026, 11, 13))
        self.assertEqual(result["rate_usd_per_eur"], "1.1052")

    def test_conversion_rounds_down_and_never_exceeds_five_eur(self):
        cap = usd_cap_from_eur("5", "1.10529")
        self.assertEqual(cap, Decimal("5.5264"))
        self.assertLessEqual(cap / Decimal("1.10529"), Decimal("5"))

    def test_position_size_is_capped_but_smaller_strategy_size_is_preserved(self):
        with self.subTest(capital=1000):
            executor, state, _ = executor_fixture(self.temp.name, capital=1000)
            position = executor.comprar("mint-high", sample_pair("HIGH"), {"pontuacao": 90})
            self.assertAlmostEqual(position["custo"], 6.0)
            self.assertLessEqual(Decimal(str(position["custo"])) / Decimal("1.2"), Decimal("5"))
        with self.subTest(capital=20):
            folder = tempfile.mkdtemp(dir=self.temp.name)
            executor, _state, _ = executor_fixture(folder, capital=20)
            position = executor.comprar("mint-low", sample_pair("LOW"), {"pontuacao": 90})
            self.assertAlmostEqual(position["custo"], 2.4)

    def test_insufficient_cash_blocks_without_partial_purchase(self):
        executor, state, _ = executor_fixture(self.temp.name, capital=1000, cash=5.99)
        executor.cfg["tamanho_posicao_pct"] = 200
        executor.cfg["exposicao_maxima_pct"] = 200
        before = state["saldo"]
        result = executor.comprar("mint-low-cash", sample_pair(), {"pontuacao": 90})
        self.assertIsNone(result)
        self.assertEqual(state["saldo"], before)
        self.assertEqual(state["posicoes"], {})
        self.assertIn("ordem completa", executor.bot.acoes[-1])

    def test_one_position_blocks_more_buys_and_preserves_legacy_position(self):
        old = {"mint-old": {"mint": "mint-old", "custo": 20.0, "simbolo": "OLD"}}
        executor, state, _ = executor_fixture(self.temp.name, capital=100, cash=80, positions=old)
        result = executor.comprar("mint-new", sample_pair("NEW"), {"pontuacao": 90})
        self.assertIsNone(result)
        self.assertEqual(list(state["posicoes"]), ["mint-old"])
        self.assertEqual(state["posicoes"]["mint-old"]["custo"], 20.0)

    def test_approved_scanner_signals_blocked_by_capacity_are_not_rejected(self):
        import teste_offline as offline
        folder = os.path.join(self.temp.name, "scanner-fixture")
        os.makedirs(folder)
        with patch.object(offline.B.requests, "get", offline.get_falso), \
                patch.object(offline.B.requests, "post", lambda *a, **k: offline.Resposta(200, {})), \
                patch.object(offline.B, "agora", lambda: offline.RELOGIO["t"]), \
                patch.object(offline.B, "dormir", lambda _seconds: None):
            executor_bot, cfg = offline.novo_bot(folder)
            cfg.update({"limites_operacionais_ativos": True, "erro_config_operacional": None,
                        "max_eur_per_operation": Decimal("5"), "max_posicoes_abertas": 1,
                        "mode": "paper", "live_trading_enabled": False,
                        "private_keys_in_application": False, "manual_confirmation_required": True})
            executor_bot.estado["operational_session"].update({
                "fx_usd_per_eur": "1.2", "fx_reference_date": date.today().isoformat(),
                "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(), "fx_source": ECB_SOURCE,
            })
            bot.guardar_estado(cfg, executor_bot.estado, strict=True)
            executor_bot._fx_rate_persisted = True
            for name in ("CAPA", "CAPB", "CAPC", "CAPD"):
                offline.listar(name)
            try:
                self.assertEqual(executor_bot.ciclo_scanner(), 1)
                self.assertEqual(len(executor_bot.estado["posicoes"]), 1)
                analyzed = {item["simbolo"]: item for item in executor_bot.recentes}
                self.assertEqual(set(analyzed), {"CAPA", "CAPB", "CAPC", "CAPD"})
                self.assertTrue(all(item["status"] == "approved" and item["aprovado"] is True
                                    for item in analyzed.values()))
                signals = {item["simbolo"]: item["estado"] for item in executor_bot.estado["sinais"]}
                self.assertEqual(list(signals.values()).count("comprado"), 1)
                self.assertEqual(list(signals.values()).count("sem_vaga"), 3)
                self.assertNotIn("rejeitada", [item["estado_analise"] for item in analyzed.values()])
            finally:
                executor_bot.close()
                if executor_bot in offline._BOTS_COM_SNAPSHOTS:
                    offline._BOTS_COM_SNAPSHOTS.remove(executor_bot)
                offline.fechar_log()

    def test_missing_or_stale_fx_blocks_new_purchases(self):
        executor, state, _ = executor_fixture(self.temp.name, capital=1000)
        state["operational_session"]["fx_usd_per_eur"] = None
        self.assertIsNone(executor.comprar("mint-no-fx", sample_pair(), {"pontuacao": 90}))
        state["operational_session"].update({
            "fx_usd_per_eur": "1.2", "fx_reference_date": "2020-01-01",
        })
        self.assertIsNone(executor.comprar("mint-stale-fx", sample_pair(), {"pontuacao": 90}))
        self.assertEqual(state["posicoes"], {})

    def test_operational_config_error_blocks_purchases(self):
        executor, state, cfg = executor_fixture(self.temp.name, capital=1000)
        cfg["erro_config_operacional"] = "fixture invalid config"
        result = executor.comprar("mint-config-error", sample_pair(), {"pontuacao": 90})
        self.assertIsNone(result)
        self.assertEqual(state["saldo"], 1000)
        self.assertEqual(state["posicoes"], {})

    def test_session_rate_survives_state_save_and_reload(self):
        state_path = os.path.join(self.temp.name, "estado.json")
        cfg = {"ficheiro_estado": state_path, "saldo_inicial_usd": 100.0}
        state = bot.estado_novo(cfg)
        state["operational_session"].update({
            "fx_usd_per_eur": "1.1052", "fx_reference_date": "2026-11-13",
            "fx_locked_at_utc": "2026-11-13T16:30:00+00:00", "fx_source": ECB_SOURCE,
        })
        bot.guardar_estado(cfg, state)
        reloaded = bot.carregar_estado(cfg)
        self.assertEqual(reloaded["operational_session"], state["operational_session"])

    def test_partial_or_unattributed_persisted_fx_lock_is_invalid(self):
        session = {
            "id": "fixture-session", "fx_usd_per_eur": "1.2",
            "fx_reference_date": date.today().isoformat(),
            "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(),
            "fx_source": ECB_SOURCE,
        }
        self.assertEqual(validate_locked_session_rate(session), Decimal("1.2"))
        for key, value in (("fx_source", "unknown"), ("fx_locked_at_utc", "2026-11-13T16:30:00"),
                           ("id", None)):
            invalid = dict(session, **{key: value})
            with self.subTest(key=key), self.assertRaises(EcbRateError):
                validate_locked_session_rate(invalid)

    def test_api_snapshot_exposes_effective_limits_and_locked_rate(self):
        state_path = os.path.join(self.temp.name, "estado.json")
        cfg = dict(bot.CONFIG)
        cfg.update({
            "ficheiro_estado": state_path,
            "ficheiro_trades": os.path.join(self.temp.name, "trades.csv"),
            "ficheiro_log": os.path.join(self.temp.name, "bot.log"),
            "ficheiro_percursos": os.path.join(self.temp.name, "percursos.jsonl"),
            "painel_web": False, "limpar_ecra": False, "limites_operacionais_ativos": True,
            "erro_config_operacional": None, "max_eur_per_operation": Decimal("5"),
            "max_posicoes_abertas": 1,
        })
        state = bot.estado_novo(cfg)
        state["operational_session"].update({
            "fx_usd_per_eur": "1.2", "fx_reference_date": date.today().isoformat(),
            "fx_locked_at_utc": datetime.now(timezone.utc).isoformat(), "fx_source": ECB_SOURCE,
        })
        bot.guardar_estado(cfg, state)
        instance = bot.Bot(cfg)
        try:
            data = json.loads(instance.snapshot)
            self.assertEqual(data["limites_operacionais"]["max_open_positions"], 1)
            self.assertEqual(data["limites_operacionais"]["max_eur_per_operation"], "5")
            self.assertEqual(data["limites_operacionais"]["fx_status"], "valida")
            self.assertEqual(data["limites_operacionais"]["fx_usd_per_eur"], "1.2")
            self.assertEqual(data["limites_operacionais"]["limit_usd"], 6.0)
            self.assertIn("Limite nominal por operação", [row[0] for row in data["config"]])
            terminal = io.StringIO()
            with redirect_stdout(terminal):
                instance.painel()
            output = terminal.getvalue()
            self.assertIn("USD 6.0000", output)
            self.assertIn("BCE 2026", output)
            summary = instance.resumo()
            self.assertIn("USD $6.0000", summary)
            self.assertIn("2026-", summary)
        finally:
            instance.close()

    def test_bot_locks_ecb_rate_once_and_reuses_it_after_restart(self):
        state_path = os.path.join(self.temp.name, "estado.json")
        cfg = dict(bot.CONFIG)
        cfg.update({
            "ficheiro_estado": state_path,
            "ficheiro_trades": os.path.join(self.temp.name, "trades.csv"),
            "ficheiro_log": os.path.join(self.temp.name, "bot.log"),
            "ficheiro_percursos": os.path.join(self.temp.name, "percursos.jsonl"),
            "painel_web": False, "limites_operacionais_ativos": True,
            "erro_config_operacional": None, "max_eur_per_operation": Decimal("5"),
            "max_posicoes_abertas": 1,
        })
        result = {"rate_usd_per_eur": "1.1052", "reference_date": date.today().isoformat(),
                  "source": ECB_SOURCE}
        with patch("bot.fetch_ecb_rate", return_value=result) as fetch:
            instance = bot.Bot(cfg)
            try:
                instance._atualizar_taxa_bce()
                deadline = time.monotonic() + 3
                while instance._fx_fetch_active and time.monotonic() < deadline:
                    instance._atualizar_taxa_bce()
                    time.sleep(0.01)
                self.assertFalse(instance._fx_fetch_active)
                self.assertEqual(instance.estado["operational_session"]["fx_usd_per_eur"], "1.1052")
                self.assertTrue(os.path.exists(state_path))
                self.assertEqual(fetch.call_count, 1)
            finally:
                instance.close()

        restarted = bot.Bot(cfg)
        try:
            with patch("bot.fetch_ecb_rate") as fetch_again:
                restarted._atualizar_taxa_bce()
                self.assertEqual(restarted.estado["operational_session"]["fx_usd_per_eur"], "1.1052")
                self.assertEqual(restarted._taxa_bce_info()["status"], "valida")
                fetch_again.assert_not_called()
        finally:
            restarted.close()

    def test_expired_ecb_rate_renews_without_new_operational_session_or_data_loss(self):
        state_path = os.path.join(self.temp.name, "renew-state.json")
        cfg = dict(bot.CONFIG)
        cfg.update({
            "ficheiro_estado": state_path,
            "ficheiro_trades": os.path.join(self.temp.name, "renew-trades.csv"),
            "ficheiro_log": os.path.join(self.temp.name, "renew-bot.log"),
            "ficheiro_percursos": os.path.join(self.temp.name, "renew-percursos.jsonl"),
            "painel_web": False, "limites_operacionais_ativos": True,
            "erro_config_operacional": None, "max_eur_per_operation": Decimal("5"),
            "max_posicoes_abertas": 1,
        })
        old_state = bot.estado_novo(cfg)
        session_id = old_state["operational_session"]["id"]
        old_state["saldo"] = 73.0
        old_state["posicoes"]["mint-legacy"] = {
            "mint": "mint-legacy", "custo": 27.0, "tokens": 1000.0,
            "preco_entrada": 0.027, "preco_atual": 0.027, "liq_atual": 10000.0,
            "aberta_em": time.time(), "simbolo": "LEGACY",
        }
        old_state["operational_session"].update({
            "fx_usd_per_eur": "1.10", "fx_reference_date": "2020-01-01",
            "fx_locked_at_utc": "2020-01-01T16:00:00+00:00", "fx_source": ECB_SOURCE,
        })
        bot.guardar_estado(cfg, old_state, strict=True)
        instance = bot.Bot(cfg)
        result = {"rate_usd_per_eur": "1.1052", "reference_date": date.today().isoformat(),
                  "source": ECB_SOURCE}
        try:
            self.assertEqual(instance._taxa_bce_info()["status"], "expirada")
            with patch("bot.fetch_ecb_rate", return_value=result) as fetch:
                instance._atualizar_taxa_bce()
                deadline = time.monotonic() + 3
                while instance._fx_fetch_active and time.monotonic() < deadline:
                    instance._atualizar_taxa_bce()
                    time.sleep(0.01)
                self.assertFalse(instance._fx_fetch_active)
                self.assertEqual(fetch.call_count, 1)
            session = instance.estado["operational_session"]
            self.assertEqual(session["id"], session_id)
            self.assertEqual(session["fx_generation"], 1)
            self.assertEqual(session["fx_history"][0]["rate_usd_per_eur"], "1.10")
            self.assertEqual(session["fx_usd_per_eur"], "1.1052")
            self.assertEqual(instance.estado["saldo"], 73.0)
            self.assertEqual(instance.estado["posicoes"]["mint-legacy"]["custo"], 27.0)
            persisted = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertEqual(persisted["operational_session"]["id"], session_id)
            self.assertEqual(persisted["operational_session"]["fx_usd_per_eur"], "1.1052")
        finally:
            instance.close()

    def test_unpersistable_ecb_rate_never_enables_purchase(self):
        state_path = os.path.join(self.temp.name, "unpersisted-state.json")
        cfg = dict(bot.CONFIG)
        cfg.update({
            "ficheiro_estado": state_path,
            "ficheiro_trades": os.path.join(self.temp.name, "unpersisted-trades.csv"),
            "ficheiro_log": os.path.join(self.temp.name, "unpersisted-bot.log"),
            "ficheiro_percursos": os.path.join(self.temp.name, "unpersisted-percursos.jsonl"),
            "painel_web": False, "limites_operacionais_ativos": True,
            "erro_config_operacional": None, "max_eur_per_operation": Decimal("5"),
            "max_posicoes_abertas": 1,
        })
        instance = bot.Bot(cfg)
        result = {"rate_usd_per_eur": "1.1052", "reference_date": date.today().isoformat(),
                  "source": ECB_SOURCE}
        try:
            with patch("bot.fetch_ecb_rate", return_value=result):
                with patch("bot.guardar_estado", side_effect=OSError("disk full")):
                    instance._atualizar_taxa_bce()
                    deadline = time.monotonic() + 3
                    while instance._fx_fetch_active and time.monotonic() < deadline:
                        instance._atualizar_taxa_bce()
                        time.sleep(0.01)
            self.assertFalse(instance._fx_rate_persisted)
            self.assertIsNone(instance.estado["operational_session"]["fx_usd_per_eur"])
            self.assertIsNone(instance.executor.comprar("mint-x", sample_pair(), {"pontuacao": 90}))
            self.assertEqual(instance.estado["saldo"], cfg["saldo_inicial_usd"])
            self.assertEqual(instance.estado["posicoes"], {})
        finally:
            instance.close()

    def test_thread_race_allows_only_one_open_position(self):
        executor, state, _ = executor_fixture(self.temp.name, capital=1000)
        barrier = threading.Barrier(3)
        results = []

        def buy(mint):
            barrier.wait()
            results.append(executor.comprar(mint, sample_pair(mint), {"pontuacao": 90}))

        threads = [threading.Thread(target=buy, args=(f"mint-{n}",)) for n in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(state["posicoes"]), 1)
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_buy_sell_and_cancel_each_have_one_durable_trade_id(self):
        executor, state, cfg = executor_fixture(self.temp.name, capital=100)
        bought = executor.comprar("mint-a", sample_pair("AAA"), {"pontuacao": 90})
        self.assertIsNotNone(bought)
        self.assertIsNotNone(executor.vender("mint-a", 0.012, 50000, "TEST_EXIT"))
        self.assertEqual(state["posicoes"], {})

        self.assertIsNotNone(executor.comprar("mint-b", sample_pair("BBB"), {"pontuacao": 90}))
        self.assertTrue(executor.anular("mint-b", "TEST_CANCEL"))
        self.assertEqual(state["posicoes"], {})
        self.assertEqual(state["paper_state_revision"], 4)
        with open(cfg["ficheiro_trades"], newline="", encoding="utf-8-sig") as source:
            rows = list(csv.DictReader(source))
        ids = [row["operation_id"] for row in rows]
        self.assertEqual([row["tipo"] for row in rows], ["COMPRA", "VENDA", "COMPRA", "ANULADA"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))

    def test_buy_sell_and_cancel_recover_after_failure_at_each_write_step(self):
        real_write = __import__("paper_operations").atomic_write_bytes
        for operation in ("BUY", "SELL", "CANCEL"):
            for fail_at in range(1, 5):
                with self.subTest(operation=operation, fail_at=fail_at):
                    folder = os.path.join(self.temp.name, f"{operation}-{fail_at}")
                    os.makedirs(folder)
                    positions = {}
                    cash = 100.0
                    if operation != "BUY":
                        cash = 80.0
                        positions = {"mint-a": {
                            "mint": "mint-a", "simbolo": "AAA", "custo": 20.0,
                            "tokens": 2000.0, "preco_entrada": 0.01, "preco_efetivo": 0.01,
                            "preco_atual": 0.01, "liq_atual": 50000.0, "aberta_em": time.time(),
                            "pontuacao": 90,
                        }}
                    executor, state, cfg = executor_fixture(
                        folder, capital=100.0, cash=cash, positions=positions
                    )
                    counter = {"n": 0}

                    def fail_selected(path, payload, **kwargs):
                        counter["n"] += 1
                        if counter["n"] == fail_at:
                            raise OSError("injected operation commit failure")
                        return real_write(path, payload, **kwargs)

                    with patch("paper_operations.atomic_write_bytes", side_effect=fail_selected):
                        with self.assertRaises(Exception):
                            if operation == "BUY":
                                executor.comprar("mint-a", sample_pair("AAA"), {"pontuacao": 90})
                            elif operation == "SELL":
                                executor.vender("mint-a", 0.012, 50000, "TEST_EXIT")
                            else:
                                executor.anular("mint-a", "TEST_CANCEL")

                    if fail_at == 1:
                        self.assertEqual(state["posicoes"], positions)
                        self.assertEqual(state["saldo"], cash)
                        self.assertFalse(os.path.exists(cfg["ficheiro_trades"]))
                        continue

                    executor.bot.paper_operations.error = None
                    executor.bot.paper_operations.recover(state)
                    if operation == "BUY":
                        self.assertEqual(list(state["posicoes"]), ["mint-a"])
                        self.assertLess(state["saldo"], 100.0)
                        expected_type = "COMPRA"
                    elif operation == "SELL":
                        self.assertEqual(state["posicoes"], {})
                        self.assertGreater(state["saldo"], 80.0)
                        expected_type = "VENDA"
                    else:
                        self.assertEqual(state["posicoes"], {})
                        self.assertEqual(state["saldo"], 100.0)
                        expected_type = "ANULADA"
                    with open(cfg["ficheiro_trades"], newline="", encoding="utf-8-sig") as source:
                        rows = list(csv.DictReader(source))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["tipo"], expected_type)
                    self.assertTrue(rows[0]["operation_id"])

    def test_keyboard_interrupt_at_each_trade_write_step_is_recoverable(self):
        real_write = __import__("paper_operations").atomic_write_bytes
        for operation in ("BUY", "SELL", "CANCEL"):
            for interrupt_at in range(1, 5):
                with self.subTest(operation=operation, interrupt_at=interrupt_at):
                    folder = os.path.join(self.temp.name, f"interrupt-{operation}-{interrupt_at}")
                    os.makedirs(folder)
                    positions = {}
                    cash = 100.0
                    if operation != "BUY":
                        cash = 80.0
                        positions = {"mint-a": {
                            "mint": "mint-a", "simbolo": "AAA", "custo": 20.0,
                            "tokens": 2000.0, "preco_entrada": 0.01, "preco_efetivo": 0.01,
                            "preco_atual": 0.01, "liq_atual": 50000.0, "aberta_em": time.time(),
                            "pontuacao": 90,
                        }}
                    executor, state, cfg = executor_fixture(
                        folder, capital=100.0, cash=cash, positions=positions
                    )
                    counter = {"n": 0}

                    def interrupt_selected(path, payload, **kwargs):
                        counter["n"] += 1
                        if counter["n"] == interrupt_at:
                            raise KeyboardInterrupt("injected process interruption")
                        return real_write(path, payload, **kwargs)

                    with patch("paper_operations.atomic_write_bytes", side_effect=interrupt_selected):
                        with self.assertRaises(KeyboardInterrupt):
                            if operation == "BUY":
                                executor.comprar("mint-a", sample_pair("AAA"), {"pontuacao": 90})
                            elif operation == "SELL":
                                executor.vender("mint-a", 0.012, 50000, "TEST_EXIT")
                            else:
                                executor.anular("mint-a", "TEST_CANCEL")

                    if interrupt_at == 1:
                        self.assertEqual(state["saldo"], cash)
                        self.assertEqual(state["posicoes"], positions)
                        self.assertEqual(executor.bot.paper_operations.pending_ids(), [])
                        continue

                    executor.bot.paper_operations.recover(state)
                    self.assertEqual(state["paper_state_revision"], 1)
                    self.assertEqual(len(state["paper_ops_applied"]), 1)
                    with open(cfg["ficheiro_trades"], newline="", encoding="utf-8-sig") as source:
                        rows = list(csv.DictReader(source))
                    self.assertEqual(len(rows), 1)
                    self.assertTrue(rows[0]["operation_id"])

    def test_process_lock_prevents_second_instance_and_releases_for_restart(self):
        state_path = os.path.join(self.temp.name, "estado.json")
        lock = ProcessInstanceLock(state_path)
        self.assertTrue(lock.acquire())
        script = (
            "import sys; from paper_trading_limits import ProcessInstanceLock; "
            "l=ProcessInstanceLock(sys.argv[1]); ok=l.acquire(); "
            "print('acquired' if ok else 'busy', flush=True); l.release()"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            busy = subprocess.run([sys.executable, "-c", script, state_path], capture_output=True,
                                  text=True, timeout=5, env=env, check=True)
            self.assertEqual(busy.stdout.strip(), "busy")
        finally:
            lock.release()
        free = subprocess.run([sys.executable, "-c", script, state_path], capture_output=True,
                              text=True, timeout=5, env=env, check=True)
        self.assertEqual(free.stdout.strip(), "acquired")

    def test_unlocked_instance_lock_is_idempotent(self):
        lock = ProcessInstanceLock(os.path.join(self.temp.name, "estado.json"))
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())
        lock.release()
        lock.release()

    def test_two_lock_objects_in_one_process_cannot_share_state(self):
        path = os.path.join(self.temp.name, "same-state.json")
        first, second = ProcessInstanceLock(path), ProcessInstanceLock(path)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_hardlinked_state_aliases_are_refused_without_modifying_either_name(self):
        state_a = Path(self.temp.name) / "state-a.json"
        state_b = Path(self.temp.name) / "state-b.json"
        original = b'{"saldo":100,"posicoes":{}}\n'
        state_a.write_bytes(original)
        os.link(state_a, state_b)
        lock_a = ProcessInstanceLock(str(state_a))
        lock_b = ProcessInstanceLock(str(state_b))

        acquired = []
        try:
            acquired = [lock_a.acquire(), lock_b.acquire()]
            self.assertEqual(acquired.count(True), 0, "hardlinked state deve falhar fechado")
        finally:
            if lock_b.acquired:
                lock_b.release()
            if lock_a.acquired:
                lock_a.release()
        self.assertEqual(state_a.read_bytes(), original)
        self.assertEqual(state_b.read_bytes(), original)
        self.assertEqual(state_a.stat().st_nlink, 2)
        self.assertFalse(Path(lock_a.path).exists())
        self.assertFalse(Path(lock_b.path).exists())

    def test_hardlink_appearing_between_lock_checks_is_rejected_and_lock_released(self):
        import paper_trading_limits

        state = Path(self.temp.name) / "state-link-race.json"
        alias = Path(self.temp.name) / "state-link-race-alias.json"
        original = b'{"saldo":50,"posicoes":{}}\n'
        state.write_bytes(original)
        lock = ProcessInstanceLock(str(state))
        real_validate = paper_trading_limits._validate_state_link_count
        calls = {"count": 0}

        def create_alias_before_postcheck(path):
            calls["count"] += 1
            if calls["count"] == 2:
                os.link(state, alias)
            return real_validate(path)

        with patch.object(paper_trading_limits, "_validate_state_link_count",
                          side_effect=create_alias_before_postcheck):
            self.assertFalse(lock.acquire())

        self.assertEqual(calls["count"], 2)
        self.assertFalse(lock.acquired)
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(alias.read_bytes(), original)
        alias.unlink()
        retry = ProcessInstanceLock(str(state))
        self.assertTrue(retry.acquire(), "lock do caminho canónico foi libertado")
        retry.release()

    def test_atomic_state_replacement_keeps_path_lock_exclusive(self):
        state = Path(self.temp.name) / "atomic-lock-state.json"
        state.write_text('{"revision":0}\n', encoding="utf-8")
        first = ProcessInstanceLock(str(state))
        second = ProcessInstanceLock(str(state))
        self.assertTrue(first.acquire())
        try:
            atomic_write_json(str(state), {"revision": 1})
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["revision"], 1)
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_process_contenders_share_one_path_lock(self):
        state_path = os.path.join(self.temp.name, "process-lock-state.json")
        gate = os.path.join(self.temp.name, "release-lock-contenders")
        status_paths = [os.path.join(self.temp.name, f"contender-{i}.txt") for i in range(2)]
        script = (
            "import os,sys,time; from paper_trading_limits import ProcessInstanceLock; "
            "state,gate,status=sys.argv[1:]; lock=ProcessInstanceLock(state); "
            "ok=lock.acquire(); open(status,'w').write('acquired' if ok else 'blocked'); "
            "\nif ok:\n"
            "  deadline=time.monotonic()+8\n"
            "  while not os.path.exists(gate) and time.monotonic()<deadline: time.sleep(.02)\n"
            "  lock.release()\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + env.get("PYTHONPATH", "")
        processes = [subprocess.Popen([sys.executable, "-B", "-c", script, state_path, gate, status],
                                      cwd=os.path.dirname(os.path.abspath(__file__)), env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for status in status_paths]
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not all(os.path.exists(path) for path in status_paths):
                time.sleep(.02)
            self.assertTrue(all(os.path.exists(path) for path in status_paths), "contenders nao responderam")
            outcomes = [Path(path).read_text(encoding="utf-8") for path in status_paths]
            self.assertEqual(outcomes.count("acquired"), 1)
            self.assertEqual(outcomes.count("blocked"), 1)
        finally:
            Path(gate).write_text("release", encoding="utf-8")
            for process in processes:
                try:
                    _out, err = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _out, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, err)

    def test_lock_cannot_be_reentered_or_released_from_another_thread(self):
        lock = ProcessInstanceLock(os.path.join(self.temp.name, "thread-owner.json"))
        self.assertTrue(lock.acquire())
        results = {}
        thread = threading.Thread(target=lambda: results.update(
            acquire=lock.acquire(), release_error=None
        ))
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(results["acquire"])
        errors = []

        def release_elsewhere():
            try:
                lock.release()
            except RuntimeError as error:
                errors.append(error)

        thread = threading.Thread(target=release_elsewhere)
        thread.start()
        thread.join(timeout=2)
        self.assertEqual(len(errors), 1)
        self.assertTrue(lock.acquired)
        lock.release()

    def test_bot_constructor_acquires_lock_before_state_or_database_access(self):
        state_path = os.path.join(self.temp.name, "locked-state.json")
        lock = ProcessInstanceLock(state_path)
        self.assertTrue(lock.acquire())
        cfg = dict(bot.CONFIG, ficheiro_estado=state_path,
                   ficheiro_trades=os.path.join(self.temp.name, "trades.csv"),
                   ficheiro_log=os.path.join(self.temp.name, "bot.log"),
                   painel_web=False)
        try:
            with patch("bot.carregar_estado") as load_state:
                with self.assertRaises(RuntimeError):
                    bot.Bot(cfg)
                load_state.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(self.temp.name, "smart_filters_market.db")))
        finally:
            lock.release()

    def test_constructor_and_correr_initialization_failures_release_lock(self):
        state_path = os.path.join(self.temp.name, "failure-state.json")
        cfg = dict(bot.CONFIG, ficheiro_estado=state_path,
                   ficheiro_trades=os.path.join(self.temp.name, "trades.csv"),
                   ficheiro_log=os.path.join(self.temp.name, "bot.log"),
                   ficheiro_percursos=os.path.join(self.temp.name, "percursos.jsonl"),
                   painel_web=False)
        with patch.object(bot.Bot, "_inicializar_contadores_scanner", side_effect=RuntimeError("init")):
            with self.assertRaises(RuntimeError):
                bot.Bot(cfg)
        lock = ProcessInstanceLock(state_path)
        self.assertTrue(lock.acquire())
        lock.release()

        instance = bot.Bot(cfg)
        with patch("bot.iniciar_painel_web", side_effect=OSError("web init")):
            with self.assertRaises(OSError):
                instance.correr()
        after = ProcessInstanceLock(state_path)
        self.assertTrue(after.acquire())
        after.release()

    def test_two_complete_bot_constructors_cannot_operate_same_state(self):
        state_path = os.path.join(self.temp.name, "concurrent-state.json")
        trades_path = os.path.join(self.temp.name, "concurrent-trades.csv")
        log_path = os.path.join(self.temp.name, "concurrent-bot.log")
        script = (
            "import os,sys,time,bot; "
            "d,s,t,l=sys.argv[1:]; c=dict(bot.CONFIG, ficheiro_estado=s, ficheiro_trades=t, "
            "ficheiro_log=l, ficheiro_percursos=os.path.join(d,'p.jsonl'), painel_web=False); "
            "b=None; "
            "\ntry:\n b=bot.Bot(c); print('acquired',flush=True); time.sleep(1.2)\n"
            "except RuntimeError:\n print('blocked',flush=True)\n"
            "finally:\n b and b.close()\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + env.get("PYTHONPATH", "")
        procs = [subprocess.Popen([sys.executable, "-c", script, self.temp.name, state_path,
                                   trades_path, log_path], cwd=os.path.dirname(os.path.abspath(__file__)),
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(out.strip())
        self.assertEqual(sorted(outputs), ["acquired", "blocked"])

    def test_process_crash_releases_os_lock(self):
        state_path = os.path.join(self.temp.name, "crash-state.json")
        script = (
            "import sys,time; from paper_trading_limits import ProcessInstanceLock; "
            "l=ProcessInstanceLock(sys.argv[1]); print(l.acquire(),flush=True); time.sleep(30)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen([sys.executable, "-c", script, state_path], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "True")
            proc.terminate()
            proc.wait(timeout=5)
            after_crash = ProcessInstanceLock(state_path)
            self.assertTrue(after_crash.acquire())
            after_crash.release()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_corrupt_state_is_preserved_and_does_not_reset_balance(self):
        state_path = os.path.join(self.temp.name, "corrupt-state.json")
        contents = "{broken-state"
        Path(state_path).write_text(contents, encoding="utf-8")
        cfg = dict(bot.CONFIG, ficheiro_estado=state_path,
                   ficheiro_trades=os.path.join(self.temp.name, "trades.csv"),
                   ficheiro_log=os.path.join(self.temp.name, "bot.log"), painel_web=False)
        with self.assertRaises(bot.StateIntegrityError):
            bot.Bot(cfg)
        self.assertEqual(Path(state_path).read_text(encoding="utf-8"), contents)
        lock = ProcessInstanceLock(state_path)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_reset_requires_explicit_confirmation_without_changing_files(self):
        folder = os.path.join(self.temp.name, "reset-confirm")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        state_before = Path(cfg["ficheiro_estado"]).read_bytes()
        trades_before = Path(cfg["ficheiro_trades"]).read_bytes()
        with self.assertRaises(PaperOperationError):
            bot.reiniciar(cfg)
        self.assertEqual(Path(cfg["ficheiro_estado"]).read_bytes(), state_before)
        self.assertEqual(Path(cfg["ficheiro_trades"]).read_bytes(), trades_before)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_reset_paths_are_canonical_and_lock_uses_same_state_key(self):
        folder = os.path.join(self.temp.name, "reset-canonical")
        os.makedirs(os.path.join(folder, "nested"))
        cfg = self.reset_fixture(folder)
        alias = os.path.join(folder, "nested", "..", "estado.json")
        paths = canonical_reset_paths(alias, cfg["ficheiro_trades"])
        self.assertEqual(paths["state"], canonical_state_path(cfg["ficheiro_estado"]))
        self.assertEqual(paths["lock"], ProcessInstanceLock(alias).path)
        self.assertEqual(paths["journal"], paths["state"] + ".paper_operations")
        self.assertEqual(paths["marker"], paths["state"] + ".reset.pending.json")
        self.assertEqual(paths["archive_root"], os.path.join(os.path.dirname(paths["state"]), "reset_archives"))

        first = ProcessInstanceLock(cfg["ficheiro_estado"])
        second = ProcessInstanceLock(alias)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()

    def test_reset_rejects_state_and_csv_alias_before_any_mutation(self):
        folder = os.path.join(self.temp.name, "reset-same-path")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        before_state = Path(cfg["ficheiro_estado"]).read_bytes()
        cfg["ficheiro_trades"] = cfg["ficheiro_estado"]
        with self.assertRaises(ResetPathError):
            bot.reiniciar(cfg, confirmado=True)
        self.assertEqual(Path(cfg["ficheiro_estado"]).read_bytes(), before_state)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_reset_rejects_hardlinked_state_and_csv_without_mutation(self):
        folder = os.path.join(self.temp.name, "reset-hardlink-collision")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        state = Path(cfg["ficheiro_estado"])
        trades = Path(cfg["ficheiro_trades"])
        trades.unlink()
        os.link(state, trades)
        original = state.read_bytes()
        self.assertTrue(os.path.samefile(state, trades))

        with self.assertRaises(ResetPathError):
            bot.reiniciar(cfg, confirmado=True)

        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(trades.read_bytes(), original)
        self.assertTrue(os.path.samefile(state, trades))
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))
        self.assertFalse(os.path.lexists(bot.reset_pending_path(cfg["ficheiro_estado"])))

    def test_reset_rejects_journal_file_before_mutation_and_preserves_hashes(self):
        folder = os.path.join(self.temp.name, "reset-journal-file")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        journal = Path(cfg["ficheiro_estado"] + ".paper_operations")
        journal.write_bytes(b"existing non-directory journal bytes")
        originals = {path: Path(path).read_bytes() for path in
                     (cfg["ficheiro_estado"], cfg["ficheiro_trades"], str(journal))}
        with self.assertRaises(ResetPathError):
            bot.reiniciar(cfg, confirmado=True)
        for path, contents in originals.items():
            self.assertEqual(Path(path).read_bytes(), contents)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))
        self.assertFalse(os.path.exists(bot.reset_pending_path(cfg["ficheiro_estado"])))

    def test_reset_rejects_journal_symlink_before_mutation(self):
        folder = os.path.join(self.temp.name, "reset-journal-link")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        journal = Path(cfg["ficheiro_estado"] + ".paper_operations")
        target = Path(folder) / "journal-target"
        target.mkdir()
        try:
            os.symlink(str(target), str(journal), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"criar symlink nao permitido neste Windows/ambiente: {exc}")
        originals = {path: Path(path).read_bytes() for path in
                     (cfg["ficheiro_estado"], cfg["ficheiro_trades"])}
        with self.assertRaises(ResetPathError):
            bot.reiniciar(cfg, confirmado=True)
        for path, contents in originals.items():
            self.assertEqual(Path(path).read_bytes(), contents)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_reset_rejects_simulated_windows_reparse_point_before_mutation(self):
        from types import SimpleNamespace
        import paper_trading_limits

        folder = os.path.join(self.temp.name, "reset-reparse")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        journal = os.path.abspath(cfg["ficheiro_estado"] + ".paper_operations")
        originals = {path: Path(path).read_bytes() for path in
                     (cfg["ficheiro_estado"], cfg["ficheiro_trades"])}
        reparse_metadata = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(paper_trading_limits._is_reparse_stat(reparse_metadata))
        real_reject = paper_trading_limits._reject_reparse_components

        def reject_journal_reparse(path, label):
            if label == "journal" and canonical_state_path(path) == canonical_state_path(journal):
                raise ResetPathError("simulated Windows reparse point")
            return real_reject(path, label)

        with patch.object(paper_trading_limits, "_reject_reparse_components",
                          side_effect=reject_journal_reparse):
            with self.assertRaises(ResetPathError):
                bot.reiniciar(cfg, confirmado=True)
        for path, contents in originals.items():
            self.assertEqual(Path(path).read_bytes(), contents)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_reset_rejects_symlinked_state_without_changing_target(self):
        folder = os.path.join(self.temp.name, "reset-state-link")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        target = Path(cfg["ficheiro_estado"])
        alias = Path(folder) / "estado-alias.json"
        try:
            os.symlink(str(target), str(alias), target_is_directory=False)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"criar symlink nao permitido neste Windows/ambiente: {exc}")
        before = target.read_bytes()
        cfg["ficheiro_estado"] = str(alias)
        with self.assertRaises(ResetPathError):
            bot.reiniciar(cfg, confirmado=True)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(alias.is_symlink())
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_reset_lock_blocks_other_instance_for_full_archive_and_replace_window(self):
        folder = os.path.join(self.temp.name, "reset-lock-window")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        entered_copy = threading.Event()
        continue_reset = threading.Event()
        failures = []
        real_copy = bot.shutil.copy2

        def pause_copy(source, destination, *args, **kwargs):
            entered_copy.set()
            if not continue_reset.wait(5):
                raise TimeoutError("test lock barrier timed out")
            return real_copy(source, destination, *args, **kwargs)

        def reset_worker():
            try:
                with patch.object(bot.shutil, "copy2", side_effect=pause_copy):
                    bot.reiniciar(cfg, confirmado=True)
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=reset_worker)
        worker.start()
        try:
            self.assertTrue(entered_copy.wait(3), "reset nao chegou a fase de arquivo")
            contender = ProcessInstanceLock(cfg["ficheiro_estado"])
            self.assertFalse(contender.acquire())
        finally:
            continue_reset.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def test_reset_failures_after_marker_keep_marker_and_exact_archive(self):
        import paper_operations

        for stage in ("journal_move", "state_replace", "csv_replace", "marker_remove"):
            with self.subTest(stage=stage):
                folder = os.path.join(self.temp.name, "reset-failure-" + stage)
                os.makedirs(folder)
                cfg = self.reset_fixture(folder)
                store = PaperOperationStore(cfg["ficheiro_estado"], cfg["ficheiro_trades"],
                                            bot.CABECALHO_CSV)
                state_path = Path(cfg["ficheiro_estado"])
                trades_path = Path(cfg["ficheiro_trades"])
                old_state = state_path.read_bytes()
                old_trades = trades_path.read_bytes()
                old_journal = {name: Path(store.directory, name).read_bytes()
                               for name in os.listdir(store.directory)}
                marker = bot.reset_pending_path(cfg["ficheiro_estado"])
                injected = {"done": False}
                real_replace = os.replace
                real_json = bot.atomic_write_json
                real_bytes = bot.atomic_write_bytes
                real_remove = os.remove

                def fail_replace(source, destination):
                    if (stage == "journal_move" and not injected["done"]
                            and canonical_state_path(source) == canonical_state_path(store.directory)):
                        injected["done"] = True
                        raise OSError("injected journal move failure")
                    return real_replace(source, destination)

                def fail_json(path, value):
                    if (stage == "state_replace" and not injected["done"]
                            and canonical_state_path(path) == canonical_state_path(state_path)):
                        injected["done"] = True
                        raise OSError("injected state replace failure")
                    return real_json(path, value)

                def fail_bytes(path, payload, **kwargs):
                    if (stage == "csv_replace" and not injected["done"]
                            and canonical_state_path(path) == canonical_state_path(trades_path)):
                        injected["done"] = True
                        raise OSError("injected CSV replace failure")
                    return real_bytes(path, payload, **kwargs)

                def fail_remove(path):
                    if stage == "marker_remove" and not injected["done"] and path == marker:
                        injected["done"] = True
                        raise OSError("injected marker removal failure")
                    return real_remove(path)

                with patch.object(bot.os, "replace", side_effect=fail_replace), \
                     patch.object(bot, "atomic_write_json", side_effect=fail_json), \
                     patch.object(bot, "atomic_write_bytes", side_effect=fail_bytes), \
                     patch.object(bot.os, "remove", side_effect=fail_remove):
                    with self.assertRaises(PaperOperationError):
                        bot.reiniciar(cfg, confirmado=True)

                self.assertTrue(injected["done"], "falha injetada nao foi atingida")
                self.assertTrue(Path(marker).is_file(), "marcador tem de bloquear o arranque")
                archives = list(Path(folder, "reset_archives").iterdir())
                self.assertEqual(len(archives), 1)
                archive = archives[0]
                self.assertEqual((archive / "estado.json").read_bytes(), old_state)
                self.assertEqual((archive / "trades.csv").read_bytes(), old_trades)
                archived_journal = archive / "paper_operations"
                if stage == "journal_move":
                    self.assertEqual({name: Path(store.directory, name).read_bytes()
                                      for name in os.listdir(store.directory)}, old_journal)
                else:
                    self.assertEqual({p.name: p.read_bytes() for p in archived_journal.iterdir()}, old_journal)

    def test_reset_with_pending_intent_blocks_and_preserves_every_source_file(self):
        folder = os.path.join(self.temp.name, "reset-pending")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        state = json.loads(Path(cfg["ficheiro_estado"]).read_text(encoding="utf-8"))
        candidate = json.loads(json.dumps(state))
        candidate["saldo"] -= 5.0
        candidate["posicoes"]["mint-pending"] = {"mint": "mint-pending", "custo": 5.0}
        row = {"data_hora": "fixture", "tipo": "COMPRA", "simbolo": "PENDING",
               "mint": "mint-pending", "valor_usd": "5.0000"}
        store = PaperOperationStore(cfg["ficheiro_estado"], cfg["ficheiro_trades"], bot.CABECALHO_CSV)
        real_write = __import__("paper_operations").atomic_write_bytes
        calls = {"n": 0}

        def fail_state_write(path, payload, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected failure after durable intent")
            return real_write(path, payload, **kwargs)

        with patch("paper_operations.atomic_write_bytes", side_effect=fail_state_write):
            with self.assertRaises(PaperOperationError):
                store.commit(state, candidate, row, "BUY")
        before_state = Path(cfg["ficheiro_estado"]).read_bytes()
        before_csv = Path(cfg["ficheiro_trades"]).read_bytes()
        journal = store.directory
        before_journal = {name: Path(journal, name).read_bytes() for name in os.listdir(journal)}

        with self.assertRaises(PaperOperationError):
            bot.reiniciar(cfg, confirmado=True)

        self.assertEqual(Path(cfg["ficheiro_estado"]).read_bytes(), before_state)
        self.assertEqual(Path(cfg["ficheiro_trades"]).read_bytes(), before_csv)
        self.assertEqual({name: Path(journal, name).read_bytes() for name in os.listdir(journal)}, before_journal)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_reset_with_corrupt_journal_blocks_without_mutating_sources(self):
        folder = os.path.join(self.temp.name, "reset-corrupt")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        store = PaperOperationStore(cfg["ficheiro_estado"], cfg["ficheiro_trades"], bot.CABECALHO_CSV)
        bad_intent = os.path.join(store.directory, "broken.intent.json")
        Path(bad_intent).write_text("{broken", encoding="utf-8")
        before = {
            cfg["ficheiro_estado"]: Path(cfg["ficheiro_estado"]).read_bytes(),
            cfg["ficheiro_trades"]: Path(cfg["ficheiro_trades"]).read_bytes(),
            bad_intent: Path(bad_intent).read_bytes(),
        }
        with self.assertRaises(PaperOperationError):
            bot.reiniciar(cfg, confirmado=True)
        for path, contents in before.items():
            self.assertEqual(Path(path).read_bytes(), contents)
        self.assertFalse(os.path.exists(os.path.join(folder, "reset_archives")))

    def test_clean_reset_archives_state_csv_and_journal_before_replacing(self):
        folder = os.path.join(self.temp.name, "reset-clean")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        base = json.loads(Path(cfg["ficheiro_estado"]).read_text(encoding="utf-8"))
        candidate = json.loads(json.dumps(base))
        candidate["saldo"] -= 5.0
        candidate["posicoes"]["mint-a"] = {"mint": "mint-a", "custo": 5.0}
        row = {"data_hora": "fixture", "tipo": "COMPRA", "simbolo": "AAA",
               "mint": "mint-a", "valor_usd": "5.0000", "saldo_depois": "95.0000"}
        store = PaperOperationStore(cfg["ficheiro_estado"], cfg["ficheiro_trades"], bot.CABECALHO_CSV)
        store.commit(base, candidate, row, "BUY")
        old_state = Path(cfg["ficheiro_estado"]).read_bytes()
        old_csv = Path(cfg["ficheiro_trades"]).read_bytes()
        old_journal = {name: Path(store.directory, name).read_bytes() for name in os.listdir(store.directory)}

        bot.reiniciar(cfg, confirmado=True)

        archives = list(Path(folder, "reset_archives").iterdir())
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        self.assertEqual((archive / "estado.json").read_bytes(), old_state)
        self.assertEqual((archive / "trades.csv").read_bytes(), old_csv)
        archived_journal = archive / "paper_operations"
        self.assertEqual({p.name: p.read_bytes() for p in archived_journal.iterdir()}, old_journal)
        self.assertTrue((archive / "manifest.json").is_file())
        new_state = json.loads(Path(cfg["ficheiro_estado"]).read_text(encoding="utf-8"))
        self.assertEqual(new_state["saldo"], cfg["saldo_inicial_usd"])
        self.assertEqual(new_state["posicoes"], {})
        self.assertEqual(new_state["paper_state_revision"], 0)
        with open(cfg["ficheiro_trades"], newline="", encoding="utf-8") as source:
            self.assertEqual(list(csv.DictReader(source)), [])
        self.assertFalse(os.path.exists(store.directory))
        self.assertFalse(os.path.exists(bot.reset_pending_path(cfg["ficheiro_estado"])))

    def test_incomplete_reset_marker_blocks_bot_before_state_or_database_load(self):
        folder = os.path.join(self.temp.name, "reset-marker")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        marker = bot.reset_pending_path(cfg["ficheiro_estado"])
        atomic_write_json(marker, {"status": "reset_in_progress", "archive": "fixture"})
        with self.assertRaises(bot.StateIntegrityError):
            bot.Bot(cfg)
        self.assertTrue(os.path.exists(marker))
        self.assertFalse(os.path.exists(os.path.join(folder, "smart_filters_market.db")))
        lock = ProcessInstanceLock(cfg["ficheiro_estado"])
        self.assertTrue(lock.acquire())
        lock.release()

    def test_dangling_reset_marker_lexists_blocks_startup_without_mutating_state(self):
        folder = os.path.join(self.temp.name, "reset-dangling-marker")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        marker = bot.reset_pending_path(cfg["ficheiro_estado"])
        missing_target = os.path.join(folder, "marker-target-that-does-not-exist")
        state_before = Path(cfg["ficheiro_estado"]).read_bytes()
        trades_before = Path(cfg["ficheiro_trades"]).read_bytes()
        try:
            os.symlink(missing_target, marker)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink quebrado nao permitido neste Windows/ambiente: {exc}")
        try:
            self.assertFalse(os.path.exists(marker))
            self.assertTrue(os.path.lexists(marker))
            with self.assertRaises(bot.StateIntegrityError):
                bot.Bot(cfg)
            self.assertEqual(Path(cfg["ficheiro_estado"]).read_bytes(), state_before)
            self.assertEqual(Path(cfg["ficheiro_trades"]).read_bytes(), trades_before)
        finally:
            if os.path.lexists(marker):
                os.unlink(marker)

    def test_ambiguous_dangling_marker_semantics_block_startup(self):
        folder = os.path.join(self.temp.name, "reset-dangling-marker-simulated")
        os.makedirs(folder)
        cfg = self.reset_fixture(folder)
        marker = bot.reset_pending_path(cfg["ficheiro_estado"])
        state_before = Path(cfg["ficheiro_estado"]).read_bytes()
        trades_before = Path(cfg["ficheiro_trades"]).read_bytes()
        real_exists = os.path.exists
        real_lexists = os.path.lexists

        def exists(path):
            if canonical_state_path(path) == canonical_state_path(marker):
                return False
            return real_exists(path)

        def lexists(path):
            if canonical_state_path(path) == canonical_state_path(marker):
                return True
            return real_lexists(path)

        with patch.object(bot.os.path, "exists", side_effect=exists), \
             patch.object(bot.os.path, "lexists", side_effect=lexists):
            with self.assertRaises(bot.StateIntegrityError):
                bot.Bot(cfg)
        self.assertEqual(Path(cfg["ficheiro_estado"]).read_bytes(), state_before)
        self.assertEqual(Path(cfg["ficheiro_trades"]).read_bytes(), trades_before)


if __name__ == "__main__":
    unittest.main()
