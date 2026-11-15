# -*- coding: utf-8 -*-
"""Testes offline do preco rapido (cotacao de venda Jupiter) - sem rede."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse

import preco_rapido as PR

PUMP = "45yf6UuhLUfkDKrNDUq4WSiLxb1e1NiggpyWjiRKpump"
OUTRO = "So11111111111111111111111111111111111111112"
CHAVE = "jup_abcdefghijklmnop1234"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(fn, pedidos):
    def abrir(pedido, timeout=None):
        pedidos.append(pedido)
        r = fn(pedido)
        if isinstance(r, Exception):
            raise r
        return _Resp(json.dumps(r).encode("utf-8"))
    return abrir


def quantidade(pedido):
    return int(urllib.parse.parse_qs(urllib.parse.urlparse(pedido.full_url).query)["amount"][0])


def erro_http(codigo, corpo):
    return urllib.error.HTTPError("https://x", codigo, "erro", {}, io.BytesIO(json.dumps(corpo).encode("utf-8")))


class PrecoRapidoTests(unittest.TestCase):
    def test_token_pump_usa_6_decimais_e_calcula_preco_executavel(self):
        pedidos = []
        # vender 1000 tokens (6 decimais) devolve 0.093 USDC => 0.000093 USD por token
        f = lambda p: {"outAmount": "93000", "priceImpactPct": "0.0004",
                       "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}}]}
        pr = PR.PrecoRapido(opener=opener(f, pedidos))
        r = pr.cotar(PUMP, 1000.0)
        self.assertEqual(r["estado"], "ok")
        self.assertAlmostEqual(r["preco"], 0.000093)
        self.assertEqual(r["decimais"], 6)
        self.assertEqual(r["impacto_pct"], 0.04)
        self.assertEqual(r["rota"], "Pump.fun Amm")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pedidos[0].full_url).query)
        self.assertEqual(q["inputMint"], [PUMP])
        self.assertEqual(q["outputMint"], [PR.USDC_MINT])
        self.assertEqual(quantidade(pedidos[0]), 1_000_000_000)
        self.assertTrue(pedidos[0].full_url.startswith(PR.URL_SEM_CHAVE))
        self.assertNotIn("x-api-key", {k.lower() for k in pedidos[0].headers})

    def test_infere_decimais_com_preco_de_referencia(self):
        pedidos = []
        preco_real = 2.0  # USD por token, token com 9 casas decimais

        def f(p):
            tokens_reais = quantidade(p) / 10 ** 9
            return {"outAmount": str(int(tokens_reais * preco_real * 10 ** 6)), "priceImpactPct": "0"}
        pr = PR.PrecoRapido(opener=opener(f, pedidos))
        r = pr.cotar(OUTRO, 10.0, preco_ref=2.05)
        self.assertEqual(r["estado"], "ok")
        self.assertEqual(r["decimais"], 9)
        self.assertAlmostEqual(r["preco"], 2.0)
        self.assertEqual(len(pedidos), 2)          # 1 palpite + 1 cotacao correta
        r = pr.cotar(OUTRO, 10.0, preco_ref=2.05)  # decimais ja conhecidos: 1 pedido
        self.assertEqual(len(pedidos), 3)
        self.assertEqual(quantidade(pedidos[-1]), 10 * 10 ** 9)

    def test_sem_decimais_nem_referencia_nao_pede(self):
        pedidos = []
        pr = PR.PrecoRapido(opener=opener(lambda p: {}, pedidos))
        r = pr.cotar(OUTRO, 10.0)
        self.assertEqual(r["erro"], "decimais_desconhecidos")
        self.assertEqual(pedidos, [])

    def test_preco_incoerente_e_rejeitado(self):
        pr = PR.PrecoRapido(opener=opener(lambda p: {"outAmount": "93000"}, []))
        self.assertEqual(pr.cotar(PUMP, 1000.0, preco_ref=0.0005)["erro"], "preco_incoerente")

    def test_limite_de_pedidos_pausa_e_retoma(self):
        relogio = {"t": 100.0}
        pedidos = []
        respostas = [erro_http(429, {"error": "Too many requests"}), {"outAmount": "93000"}]
        pr = PR.PrecoRapido(opener=opener(lambda p: respostas.pop(0), pedidos), relogio=lambda: relogio["t"])
        self.assertEqual(pr.cotar(PUMP, 1000.0)["erro"], "limite_pedidos")
        self.assertEqual(pr.cotar(PUMP, 1000.0)["erro"], "em_pausa_limite_pedidos")
        self.assertEqual(len(pedidos), 1)
        relogio["t"] += 31
        self.assertEqual(pr.cotar(PUMP, 1000.0)["estado"], "ok")

    def test_sem_rota_conta_falhas_seguidas_e_sucesso_limpa(self):
        respostas = [erro_http(400, {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"}),
                     erro_http(400, {"errorCode": "TOKEN_NOT_TRADABLE"}), {"outAmount": "93000"}]
        pr = PR.PrecoRapido(opener=opener(lambda p: respostas.pop(0), []))
        self.assertEqual(pr.cotar(PUMP, 1000.0)["sem_rota_seguidas"], 1)
        r = pr.cotar(PUMP, 1000.0)
        self.assertEqual((r["erro"], r["sem_rota_seguidas"]), ("sem_rota", 2))
        self.assertEqual(pr.cotar(PUMP, 1000.0)["estado"], "ok")
        self.assertEqual(pr.sem_rota[PUMP], 0)

    def test_chave_api_vai_no_cabecalho_e_nunca_no_url_nem_nos_erros(self):
        pedidos = []
        pr = PR.PrecoRapido(api_key=CHAVE, opener=opener(lambda p: erro_http(401, {"error": "bad key"}), pedidos))
        r = pr.cotar(PUMP, 1000.0)
        self.assertTrue(pedidos[0].full_url.startswith(PR.URL_COM_CHAVE))
        self.assertEqual(pedidos[0].get_header("X-api-key"), CHAVE)
        self.assertNotIn(CHAVE, pedidos[0].full_url)
        self.assertNotIn(CHAVE, json.dumps(r))
        self.assertEqual(pr.origem, "jupiter_chave")
        self.assertEqual(PR.PrecoRapido(api_key="curta").origem, "jupiter_publico")

    def test_nunca_lanca_excecoes(self):
        pr = PR.PrecoRapido(opener=opener(lambda p: OSError("rede"), []))
        self.assertEqual(pr.cotar(PUMP, 1000.0)["estado"], "erro")
        self.assertEqual(pr.cotar("invalido", 1000.0)["erro"], "mint_invalido")
        self.assertEqual(pr.cotar(PUMP, 0)["erro"], "quantidade_invalida")
        self.assertEqual(pr.cotar(PUMP, float("nan"))["erro"], "quantidade_invalida")


    def test_cotar_compra(self):
        pedidos = []
        respostas = [{"outAmount": "1000", "priceImpactPct": "0.021", "routePlan": [{"swapInfo": {"label": "Raydium"}}]},
                     erro_http(400, {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"}), erro_http(429, {})]
        pr = PR.PrecoRapido(opener=opener(lambda p: respostas.pop(0), pedidos))
        r = pr.cotar_compra(PUMP, 5.5)
        self.assertEqual((r["estado"], r["impacto_pct"], r["rota"]), ("ok", 2.1, "Raydium"))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pedidos[0].full_url).query)
        self.assertEqual((q["inputMint"], q["outputMint"], q["amount"]), ([PR.USDC_MINT], [PUMP], ["5500000"]))
        self.assertEqual(pr.cotar_compra(PUMP, 5.5)["erro"], "sem_rota")
        self.assertEqual(pr.cotar_compra(PUMP, 5.5)["erro"], "limite_pedidos")
        self.assertEqual(pr.cotar_compra(PUMP, 5.5)["erro"], "em_pausa_limite_pedidos")
        self.assertEqual(pr.cotar_compra(PUMP, 0)["erro"], "valor_invalido")

class ChavesLocaisTests(unittest.TestCase):
    def test_le_e_valida_chaves(self):
        with tempfile.TemporaryDirectory() as pasta:
            with open(os.path.join(pasta, PR.FICHEIRO_CHAVES), "w", encoding="utf-8") as f:
                json.dump({"jupiter_api_key": CHAVE, "telegram_token": "123456789:" + "A" * 35,
                           "telegram_chat_id": "-100123456", "helius_api_key": "x"}, f)
            c = PR.ler_chaves_locais(pasta, ambiente={})
            self.assertEqual(c["jupiter_api_key"], CHAVE)
            self.assertEqual(c["telegram_chat_id"], "-100123456")
            self.assertTrue(c["telegram_token"].startswith("123456789:"))
            # o ambiente tem prioridade; valores invalidos sao ignorados
            c = PR.ler_chaves_locais(pasta, ambiente={"TELEGRAM_TOKEN": "lixo"})
            self.assertEqual(c["telegram_token"], "")

    def test_sem_ficheiro_ou_ficheiro_invalido(self):
        with tempfile.TemporaryDirectory() as pasta:
            self.assertEqual(PR.ler_chaves_locais(pasta, ambiente={}),
                             {"jupiter_api_key": "", "telegram_token": "", "telegram_chat_id": ""})
            with open(os.path.join(pasta, PR.FICHEIRO_CHAVES), "w", encoding="utf-8") as f:
                f.write("{nao e json")
            self.assertEqual(PR.ler_chaves_locais(pasta, ambiente={})["jupiter_api_key"], "")



class RedeTests(unittest.TestCase):
    def test_limpar_erro_esconde_urls_e_chaves(self):
        import rede
        txt = rede.limpar_erro("HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: "
                               "/?api-key=SEGREDO123 https://mainnet.helius-rpc.com/?api-key=SEGREDO123", 300)
        self.assertNotIn("SEGREDO123", txt)

    def test_pedir_json_com_requests_simulado(self):
        import rede

        class Resp:
            def __init__(self, codigo, dados):
                self.status_code, self._d = codigo, dados

            def json(self):
                if self._d is None:
                    raise ValueError
                return self._d

        class Sessao:
            def __init__(self, respostas):
                self.respostas, self.pedidos = respostas, []

            def get(self, url, headers=None, timeout=None):
                self.pedidos.append(("GET", url, headers))
                r = self.respostas.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            post = get

        s = Sessao([Resp(200, {"ok": 1}), Resp(429, {}), Resp(400, {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"}),
                    rede.requests.exceptions.ConnectionError("falhou url: /?api-key=SEGREDO"),
                    rede.requests.exceptions.ReadTimeout("lento"), Resp(200, None)])
        rede._local.sessao = s
        try:
            self.assertEqual(rede.pedir_json("https://x"), ({"ok": 1}, None, None))
            self.assertEqual(rede.pedir_json("https://x")[1], "limite_pedidos")
            self.assertEqual(rede.pedir_json("https://x")[2], {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
            erro = rede.pedir_json("https://x")[1]
            self.assertTrue(erro.startswith("sem_ligacao") and "SEGREDO" not in erro)
            self.assertEqual(rede.pedir_json("https://x")[1], "timeout")
            self.assertEqual(rede.pedir_json("https://x")[1], "resposta_invalida")
            # o preco rapido usa esta camada quando nao ha opener de teste
            s.respostas = [Resp(400, {"errorCode": "TOKEN_NOT_TRADABLE"})]
            self.assertEqual(PR.PrecoRapido().cotar(PUMP, 1000.0)["erro"], "sem_rota")
        finally:
            rede._local.sessao = None

if __name__ == "__main__":
    unittest.main()
