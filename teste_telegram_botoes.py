# -*- coding: utf-8 -*-
"""Testes offline dos botoes Aprovar/Recusar no Telegram (sem rede)."""
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import bot
import paper_trading_limits as limits_module
from teste_paper_trading_limits import executor_fixture, sample_pair

CHAT = "987654321"


class _Resp:
    def __init__(self, codigo, dados):
        self.status_code = codigo
        self._dados = dados

    def json(self):
        return self._dados


class _ApiFalsa:
    """Guarda os pedidos ao Telegram e devolve respostas preparadas."""
    def __init__(self):
        self.pedidos = []
        self.updates = []
        self.codigo_updates = 200

    def __call__(self, url, json=None, timeout=None):
        metodo = url.rsplit("/", 1)[1]
        self.pedidos.append((metodo, json))
        if metodo == "getUpdates":
            if self.codigo_updates != 200:
                return _Resp(self.codigo_updates, {"ok": False})
            lista, self.updates = self.updates, []
            return _Resp(200, {"ok": True, "result": lista})
        return _Resp(200, {"ok": True, "result": True})

    def metodos(self):
        return [m for m, _ in self.pedidos]


def clique(dados, de=CHAT, chat=CHAT, update_id=1, message_id=77):
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": dados,
            "from": {"id": int(de)}, "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}


class TelegramBotoesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        politica = patch.object(limits_module, "APPROVED_MAX_SLIPPAGE_PCT", Decimal("95"))
        politica.start()
        self.addCleanup(politica.stop)
        self.manager = bot.ManualApprovalManager()
        self.addCleanup(self.manager.close)
        executor, _state, _cfg = executor_fixture(self.temp.name, capital=100)
        amount, reason = executor.pode_comprar()
        self.assertGreater(amount, 0, reason)
        plan = executor.preparar_execucao("mint-tg", sample_pair("TG"), amount)
        self.p = self.manager.propose("mint-tg", sample_pair("TG"), {"pontuacao": 77}, amount, plan)
        self.api = _ApiFalsa()
        self.decisoes = []

        def decidir(pid, decisao):
            self.decisoes.append((pid, decisao))
            return self.manager.decidir_dashboard(pid, decisao, origem="telegram")

        self.tg = bot.TelegramDecisoes("123:abc", CHAT, decidir, self.manager.esta_pendente, post=self.api)
        self.tg.registar_mensagem(self.p.proposal_id, 77, "🔔 [PAPER] NOVA PROPOSTA")

    def test_aprovar_pede_confirmacao_e_so_depois_decide(self):
        pid = self.p.proposal_id
        self.assertEqual(self.tg.processar(clique("a:" + pid)), "confirmar")
        self.assertEqual(self.decisoes, [])                                 # carregar em Aprovar nao decide nada
        markup = [j for m, j in self.api.pedidos if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "s:" + pid)
        self.assertEqual(self.tg.processar(clique("n:" + pid, update_id=2)), "voltou")
        self.assertEqual(self.decisoes, [])
        self.assertTrue(self.manager.esta_pendente(pid))
        self.assertEqual(self.tg.processar(clique("s:" + pid, update_id=3)), "aprovar")
        self.assertEqual(self.decisoes, [(pid, "aprovar")])
        _p, token, aceite, motivo = self.manager.drain_results()[0]
        self.assertTrue(aceite and token)
        self.assertEqual(motivo, "aprovada no Telegram")
        texto = [j for m, j in self.api.pedidos if m == "editMessageText"][-1]["text"]
        self.assertIn("Aprovada no Telegram", texto)
        self.assertEqual(self.tg.processar(clique("s:" + pid, update_id=4)), "fechada")   # segundo clique: nada
        self.assertEqual(len(self.decisoes), 1)

    def test_recusar(self):
        pid = self.p.proposal_id
        self.assertEqual(self.tg.processar(clique("r:" + pid)), "recusar")
        _p, token, aceite, motivo = self.manager.drain_results()[0]
        self.assertFalse(aceite)
        self.assertIsNone(token)
        self.assertEqual(motivo, "recusada no Telegram")
        self.assertFalse(self.manager.esta_pendente(pid))

    def test_so_aceita_o_chat_privado_configurado(self):
        pid = self.p.proposal_id
        self.assertEqual(self.tg.processar(clique("s:" + pid, de="111")), "nao_autorizado")
        self.assertEqual(self.tg.processar(clique("s:" + pid, chat="-100555")), "nao_autorizado")
        self.assertEqual(self.decisoes, [])
        self.assertTrue(self.manager.esta_pendente(pid))
        grupo = bot.TelegramDecisoes("123:abc", "-100555", lambda *a: (True, ""), lambda pid: True, post=self.api)
        self.assertFalse(grupo.ativo)                                       # num grupo os botoes ficam desligados
        self.assertFalse(bot.TelegramDecisoes("", CHAT, None, None).ativo)

    def test_botao_invalido_expirado_ou_desconhecido(self):
        self.assertEqual(self.tg.processar(clique("x:" + self.p.proposal_id)), "invalido")
        self.assertEqual(self.tg.processar(clique("s:" + "0" * 32, update_id=2)), "fechada")
        self.manager._pending[self.p.proposal_id] = replace(self.p, expires_monotonic=time.monotonic() - 1)
        self.assertEqual(self.tg.processar(clique("s:" + self.p.proposal_id, update_id=3)), "fechada")
        self.assertEqual(self.decisoes, [])
        self.assertIsNone(self.tg.processar({"update_id": 9, "message": {"text": "ola"}}))

    def test_mensagem_de_texto_pede_analise_manual(self):
        pedidos = []
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                  post=self.api, analisar=lambda txt: (pedidos.append(txt), "RESULTADO")[1],
                                  em_fundo=False)
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        msg = lambda texto, de=CHAT, chat=CHAT: {"update_id": 5, "message": {
            "text": texto, "from": {"id": int(de)}, "chat": {"id": int(chat)}}}
        self.assertEqual(tg.processar(msg(mint, de="111", chat="111")), "nao_autorizado")
        self.assertEqual(tg.processar(msg(mint, chat="-100123")), "nao_autorizado")    # grupo: ignorado
        self.assertNotIn("sendMessage", self.api.metodos())
        self.assertEqual(tg.processar(msg("/start")), "ajuda")
        self.assertIn("Análise manual", [j for m, j in self.api.pedidos if m == "sendMessage"][-1]["text"])
        self.assertEqual(tg.processar(msg("/analisar " + mint)), "analise")
        self.assertEqual(pedidos, ["/analisar " + mint])
        enviado = [j for m, j in self.api.pedidos if m == "sendMessage"][-1]
        self.assertEqual((enviado["text"], enviado["chat_id"]), ("RESULTADO", CHAT))
        tg._a_analisar.set()
        self.assertEqual(tg.processar(msg(mint)), "ocupado")
        tg._a_analisar.clear()
        self.api.updates = []
        tg.ciclo(timeout=0)
        pedido = [j for m, j in self.api.pedidos if m == "getUpdates"][-1]
        self.assertEqual(pedido["allowed_updates"], ["callback_query", "message"])

    def test_analise_traz_botao_acompanhar(self):
        pedidos = []
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                  post=self.api, analisar=lambda txt: ("ANALISE", mint), em_fundo=False,
                                  acompanhar=lambda m: (pedidos.append(m), (True, "Vou acompanhar"))[1])
        msg = {"update_id": 5, "message": {"text": mint, "from": {"id": int(CHAT)}, "chat": {"id": int(CHAT)}}}
        self.assertEqual(tg.processar(msg), "analise")
        enviado = [j for m, j in self.api.pedidos if m == "sendMessage"][-1]
        self.assertEqual(enviado["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "w:" + mint)
        self.assertEqual(tg.processar(clique("w:" + mint, update_id=6)), "acompanhar")
        self.assertEqual(pedidos, [mint])
        self.assertEqual(tg.processar(clique("w:" + mint, de="111", update_id=7)), "nao_autorizado")
        self.assertEqual(pedidos, [mint])
        sem = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                   post=self.api)
        self.assertEqual(sem.processar(clique("w:" + mint, update_id=8)), "invalido")

    def test_comando_acompanhados(self):
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                  post=self.api, analisar=lambda txt: "x", em_fundo=False,
                                  acompanhados=lambda: "LISTA AO VIVO")
        msg = lambda texto, de=CHAT: {"update_id": 5, "message": {"text": texto, "from": {"id": int(de)},
                                                                   "chat": {"id": int(de)}}}
        self.assertEqual(tg.processar(msg("/acompanhados", de="111")), "nao_autorizado")
        self.assertEqual(tg.processar(msg("/acompanhados")), "acompanhados")
        self.assertEqual([j for m, j in self.api.pedidos if m == "sendMessage"][-1]["text"], "LISTA AO VIVO")

    def test_comprar_e_recuperar_entrada(self):
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        compras, recuperar = [], []
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                  post=self.api, analisar=lambda txt: ("ANALISE", mint), em_fundo=False,
                                  vender=lambda m: (True, "ok"), posicao_aberta=lambda m: True,
                                  comprar=lambda m, v=None: (compras.append((m, v)), (True, "A preparar a proposta"))[1],
                                  recuperar=lambda m: (recuperar.append(m), (True, "pedido registado"))[1],
                                  pode_recuperar=lambda m: True)
        msg = {"update_id": 5, "message": {"text": mint, "from": {"id": int(CHAT)}, "chat": {"id": int(CHAT)}}}
        tg.processar(msg)
        teclado = [j for m, j in self.api.pedidos if m == "sendMessage"][-1]["reply_markup"]["inline_keyboard"]
        self.assertIn("c:" + mint, [b["callback_data"] for linha in teclado for b in linha])
        self.assertEqual(tg.processar(clique("c:" + mint, update_id=6)), "escolher_valor")   # primeiro o valor
        self.assertEqual(compras, [])
        self.assertEqual(tg.processar(clique("x:" + mint, update_id=61)), "valor_cancelado")
        self.assertEqual(tg.processar(clique("o:" + mint, update_id=62)), "outro_valor")
        self.assertEqual(tg.processar(clique("e3:" + mint, update_id=63)), "comprar")
        self.assertEqual(tg.processar(clique("e9:" + mint, update_id=64)), "invalido")
        cmd = lambda t, u: {"update_id": u, "message": {"text": t, "from": {"id": int(CHAT)}, "chat": {"id": int(CHAT)}}}
        self.assertEqual(tg.processar(cmd("/comprar " + mint + " 2,5", 65)), "comprar")
        self.assertEqual(tg.processar(cmd("/comprar " + mint, 66)), "escolher_valor")
        self.assertEqual(compras, [(mint, 3), (mint, "2,5")])
        self.assertEqual(tg.processar(clique("c:" + mint, de="111", update_id=7)), "nao_autorizado")
        self.assertEqual(tg.processar(clique("p:" + mint, update_id=8)), "confirmar_recuperar")
        self.assertEqual(recuperar, [])                                     # so depois do "Sim"
        self.assertEqual(tg.processar(clique("k:" + mint, update_id=9)), "venda_cancelada")
        self.assertEqual(tg.processar(clique("z:" + mint, update_id=10)), "recuperar")
        self.assertEqual(recuperar, [mint])
        self.assertEqual(len(bot.TelegramDecisoes.botoes_venda(mint, recuperar=True)), 4)
        self.assertEqual(len(bot.TelegramDecisoes.botoes_venda(mint)), 3)

    def test_vender_parte_e_mudar_stop(self):
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        vendas, stops = [], []
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                  post=self.api, analisar=lambda txt: ("ANALISE", mint), em_fundo=False,
                                  vender=lambda m, f=None: (vendas.append((m, f)), (True, "pedido registado"))[1],
                                  posicao_aberta=lambda m: True,
                                  stop=lambda m, v: (stops.append((m, v)), (True, "stop mudado"))[1])
        self.assertEqual(tg.processar(clique("h25:" + mint, update_id=20)), "confirmar_parte")
        self.assertEqual(vendas, [])                                       # so depois do "Sim"
        self.assertEqual(tg.processar(clique("g25:" + mint, update_id=21)), "vender_parte")
        self.assertEqual(tg.processar(clique("g30:" + mint, update_id=22)), "invalido")
        self.assertEqual(vendas, [(mint, 25)])
        self.assertEqual(tg.processar(clique("t:" + mint, update_id=23)), "escolher_stop")
        self.assertEqual(tg.processar(clique("u-15:" + mint, update_id=24)), "stop")
        self.assertEqual(tg.processar(clique("u10:" + mint, update_id=25)), "stop")
        self.assertEqual(tg.processar(clique("j:" + mint, update_id=26)), "stop_outro")
        cmd = lambda t, u: {"update_id": u, "message": {"text": t, "from": {"id": int(CHAT)}, "chat": {"id": int(CHAT)}}}
        self.assertEqual(tg.processar(cmd("/stop " + mint + " -18%", 27)), "stop")
        self.assertEqual(tg.processar(cmd("/stop " + mint, 28)), "stop_ajuda")
        self.assertEqual(stops, [(mint, -15), (mint, 10), (mint, "-18")])
        self.assertEqual(tg.processar(clique("u-15:" + mint, de="111", update_id=29)), "nao_autorizado")

    def test_parar_acompanhamento(self):
        mint = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
        parados = []
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (True, "ok"), self.manager.esta_pendente,
                                  post=self.api, analisar=lambda txt: "x", em_fundo=False,
                                  acompanhados=lambda: ("LISTA", [(mint, "VEXA")]),
                                  deixar=lambda m: (parados.append(m), (True, "Deixei de acompanhar $VEXA."))[1])
        msg = {"update_id": 5, "message": {"text": "/acompanhados", "from": {"id": int(CHAT)}, "chat": {"id": int(CHAT)}}}
        self.assertEqual(tg.processar(msg), "acompanhados")
        enviado = [j for m, j in self.api.pedidos if m == "sendMessage"][-1]
        self.assertEqual(enviado["text"], "LISTA")
        self.assertEqual(enviado["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "d:" + mint)
        self.assertEqual(tg.processar(clique("d:" + mint, de="111", update_id=6)), "nao_autorizado")
        self.assertEqual(tg.processar(clique("d:" + mint, update_id=7)), "parar")
        self.assertEqual(parados, [mint])

    def test_decisao_recusada_pelo_bot(self):
        tg = bot.TelegramDecisoes("123:abc", CHAT, lambda pid, d: (False, "paragem de emergencia ativa"),
                                  self.manager.esta_pendente, post=self.api)
        self.assertEqual(tg.processar(clique("s:" + self.p.proposal_id)), "recusado_pelo_bot")
        resposta = [j for m, j in self.api.pedidos if m == "answerCallbackQuery"][-1]
        self.assertIn("paragem de emergencia", resposta["text"])
        self.assertTrue(resposta["show_alert"])

    def test_ciclo_offset_e_cliques_antigos(self):
        self.api.updates = [{"update_id": 40}, {"update_id": 41}]
        self.assertTrue(self.tg.descartar_antigos())                        # ignora o que ficou para tras
        self.assertEqual(self.tg.offset, 42)
        self.api.updates = [clique("a:" + self.p.proposal_id, update_id=42)]
        self.assertEqual(self.tg.ciclo(timeout=0), 1)
        self.assertEqual(self.tg.offset, 43)
        pedido = [j for m, j in self.api.pedidos if m == "getUpdates"][-1]
        self.assertEqual((pedido["offset"], pedido["allowed_updates"]), (42, ["callback_query"]))

    def test_conflito_desliga_os_botoes_e_avisa(self):
        avisos = []
        self.tg.avisar = avisos.append
        self.api.codigo_updates = 409
        self.assertEqual(self.tg.ciclo(timeout=0), "http_409")
        self.assertFalse(self.tg.ativo)
        self.assertEqual(len(avisos), 1)
        self.assertIn("outro programa", avisos[0])

    def test_fechar_escreve_o_resultado_uma_vez(self):
        self.tg.fechar(self.p.proposal_id, "✅ Compra Paper feita.")
        fim = time.time() + 3
        while "editMessageText" not in self.api.metodos() and time.time() < fim:
            time.sleep(0.01)
        texto = [j for m, j in self.api.pedidos if m == "editMessageText"][-1]
        self.assertEqual(texto["text"], "🔔 [PAPER] NOVA PROPOSTA\n\n✅ Compra Paper feita.")
        self.assertNotIn("reply_markup", texto)                              # os botoes desaparecem
        antes = len(self.api.pedidos)
        self.tg.fechar(self.p.proposal_id, "outra vez")
        time.sleep(0.05)
        self.assertEqual(len(self.api.pedidos), antes)

    def test_vender_agora_pede_confirmacao(self):
        mint = "So11111111111111111111111111111111111111112"
        abertas, pedidos = {mint}, []

        def vender(m):
            pedidos.append(m)
            abertas.discard(m)
            return True, "pedido registado"

        tg = bot.TelegramDecisoes("123:abc", CHAT, None, None, post=self.api, vender=vender,
                                  posicao_aberta=lambda m: m in abertas)
        self.assertEqual(tg.processar(clique("v:" + mint)), "confirmar_venda")
        self.assertEqual(pedidos, [])                                       # carregar em Vender nao vende nada
        markup = [j for m, j in self.api.pedidos if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "y:" + mint)
        self.assertEqual(tg.processar(clique("k:" + mint, update_id=2)), "venda_cancelada")
        self.assertEqual(pedidos, [])
        self.assertEqual(tg.processar(clique("y:" + mint, de="111", update_id=3)), "nao_autorizado")
        self.assertEqual(tg.processar(clique("y:" + mint, update_id=4)), "vender")
        self.assertEqual(pedidos, [mint])
        self.assertEqual(tg.processar(clique("y:" + mint, update_id=5)), "venda_fechada")   # segundo clique: nada
        self.assertEqual(pedidos, [mint])
        sem = bot.TelegramDecisoes("123:abc", CHAT, None, None, post=self.api)              # venda manual desligada
        self.assertEqual(sem.processar(clique("y:" + mint)), "venda_fechada")

    def test_venda_recusada_pelo_bot_mantem_o_botao(self):
        mint = "So11111111111111111111111111111111111111112"
        tg = bot.TelegramDecisoes("123:abc", CHAT, None, None, post=self.api,
                                  vender=lambda m: (False, "modo Paper indisponivel"), posicao_aberta=lambda m: True)
        self.assertEqual(tg.processar(clique("y:" + mint)), "venda_recusada")
        markup = [j for m, j in self.api.pedidos if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "v:" + mint)

    def test_fechar_posicoes_tira_o_botao_de_venda(self):
        tg = bot.TelegramDecisoes("123:abc", CHAT, None, None, post=self.api)
        tg.registar_mensagem("pos:AAA", 90, "COMPRA AAA")
        tg.registar_mensagem("pos:BBB", 91, "COMPRA BBB")
        tg.fechar_posicoes({"BBB"})
        fim = time.time() + 3
        while "editMessageText" not in self.api.metodos() and time.time() < fim:
            time.sleep(0.01)
        editados = [j for m, j in self.api.pedidos if m == "editMessageText"]
        self.assertEqual([(j["message_id"], j["text"]) for j in editados], [(90, "COMPRA AAA\n\nPosição fechada.")])

    def test_token_nunca_vai_para_o_registo(self):
        def rebenta(url, json=None, timeout=None):
            raise ConnectionError("falhou " + url)
        tg = bot.TelegramDecisoes("123:SEGREDO", CHAT, None, None, post=rebenta)
        with self.assertLogs("bot", level="WARNING") as registo:
            bot.log.warning("marca")
            self.assertEqual(tg.ciclo(timeout=0), "rede_ConnectionError")
        self.assertNotIn("SEGREDO", "\n".join(registo.output))


if __name__ == "__main__":
    unittest.main()
