# -*- coding: utf-8 -*-
"""Testes da pagina publica so de leitura (pagina_publica.py). Sem internet: usa 127.0.0.1."""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest

import pagina_publica as P


class PaginaPublicaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pasta = self.tmp.name
        with open(os.path.join(pasta, "estado.json"), "w", encoding="utf-8") as f:
            json.dump({"saldo_inicial": 100.0, "saldo": 90.0, "posicoes": {}, "historico": [],
                       "ultimo_ciclo": time.time(), "inicio": time.time() - 3600,
                       "operational_session": {"id": "segredo-interno"}}, f)
        with open(os.path.join(pasta, "trades.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("data_hora,tipo,simbolo,mint,pnl_usd,pnl_pct,motivo,duracao_min,pontuacao\n"
                    "2026-11-15 10:00:00,COMPRA,ABC,MintABC,,,pontuacao,,80\n"
                    "2026-11-15 10:05:00,VENDA,ABC,MintABC,-10.0,-10.0,STOP_LOSS -12.0%,5.0,80\n")
        P._cache.update(t=0.0, dados=None)

        class _H(P.Handler):
            pass

        _H.pasta = pasta
        self.handler = _H
        self.srv = P.Servidor(("127.0.0.1", 0), _H)
        self.porta = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _pedido(self, metodo, caminho, corpo=None):
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        try:
            c.request(metodo, caminho, body=corpo)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_saude_e_cabecalhos_de_seguranca(self):
        estado, cab, corpo = self._pedido("GET", "/saude")
        self.assertEqual((estado, corpo), (200, b"ok"))
        self.assertEqual(cab.get("X-Frame-Options"), "DENY")
        self.assertIn("default-src 'none'", cab.get("Content-Security-Policy", ""))

    def test_so_leitura_recusa_escrita(self):
        for metodo in ("POST", "PUT", "DELETE", "PATCH"):
            estado, _, _ = self._pedido(metodo, "/api/proposta/decisao", corpo=b"{}")
            self.assertEqual(estado, 405, metodo)

    def test_dados_publicos_sem_identificadores_internos(self):
        estado, _, corpo = self._pedido("GET", "/dados.json")
        self.assertEqual(estado, 200)
        dados = json.loads(corpo.decode("utf-8"))
        self.assertEqual((dados["fechados"], dados["perdas"], dados["total"]), (1, 1, 90.0))
        self.assertNotIn("segredo-interno", corpo.decode("utf-8"))

    def test_lista_de_carteiras_seguidas_nunca_e_publicada(self):
        estado = {"recentes": [{"mint": "M", "observacao_extra": {"carteiras": {"dentro": 1, "quem": [["W", 1.0]]},
                                                                  "holders": {"top10_pct": 40.0}}}], "quem": "x"}
        self.assertEqual(P._limpar(estado),
                         {"recentes": [{"mint": "M", "observacao_extra": {"holders": {"top10_pct": 40.0}}}]})

    def test_motivo_manual_na_pagina_publica(self):
        self.assertEqual(P._motivo_curto("MANUAL (Telegram)"), "Manual")
        self.assertEqual(P._motivo_curto("STOP_LOSS -12.0%"), "Stop-loss")

    def test_imagem_do_tema(self):
        import shutil
        os.makedirs(os.path.join(self.tmp.name, "assets"), exist_ok=True)
        origem = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "mascote_chifres.png")
        shutil.copy(origem, os.path.join(self.tmp.name, "assets", "mascote_chifres.png"))
        estado, cab, corpo = self._pedido("GET", "/assets/mascote_chifres.png")
        self.assertEqual((estado, cab.get("Content-Type"), corpo[:4]), (200, "image/png", b"\x89PNG"))
        self.assertEqual(self._pedido("GET", "/assets/outra.png")[0], 404)

    def test_caminho_desconhecido_da_404(self):
        self.assertEqual(self._pedido("GET", "/chaves_locais.json")[0], 404)
        self.assertEqual(self._pedido("GET", "/../estado.json")[0], 404)

    def test_ligacao_parada_e_largada_pelo_timeout(self):
        self.handler.timeout = 0.5
        s = socket.create_connection(("127.0.0.1", self.porta), timeout=5)
        try:
            s.sendall(b"GET /saude HTTP/1.1\r\n")  # pedido incompleto, fica calado
            s.settimeout(5)
            self.assertEqual(s.recv(1024), b"")    # o servidor fecha a ligacao sozinho
        finally:
            s.close()

    def test_teto_de_ligacoes_em_simultaneo(self):
        self.handler.timeout = 3
        paradas = []
        try:
            for _ in range(P.Servidor.max_ligacoes):
                s = socket.create_connection(("127.0.0.1", self.porta), timeout=5)
                s.sendall(b"GET /saude HTTP/1.1\r\n")
                paradas.append(s)
            fim = time.time() + 3
            while self.srv._vagas._value > 0 and time.time() < fim:
                time.sleep(0.02)
            self.assertEqual(self.srv._vagas._value, 0)
            extra = socket.create_connection(("127.0.0.1", self.porta), timeout=5)
            try:
                extra.settimeout(2)
                try:
                    extra.sendall(b"GET /saude HTTP/1.1\r\nHost: x\r\n\r\n")
                    resposta = extra.recv(1024)
                except OSError:
                    resposta = b""
                self.assertEqual(resposta, b"")    # acima do teto: fechada sem resposta
            finally:
                extra.close()
        finally:
            for s in paradas:
                s.close()
        fim = time.time() + 5
        while self.srv._vagas._value < P.Servidor.max_ligacoes and time.time() < fim:
            time.sleep(0.05)
        self.assertEqual(self.srv._vagas._value, P.Servidor.max_ligacoes)  # as vagas voltam todas
        self.assertEqual(self._pedido("GET", "/saude")[0], 200)


if __name__ == "__main__":
    unittest.main()
