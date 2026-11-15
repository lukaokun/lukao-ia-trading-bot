# -*- coding: utf-8 -*-
"""Testes offline da observacao extra (sem rede)."""
import copy
import io
import json
import time
import unittest
import urllib.parse

import observacao_extra as OE

MINT = "So11111111111111111111111111111111111111112"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_falso(respostas):
    """respostas: funcao(pedido) -> dict ou Exception."""
    def abrir(pedido, timeout=None):
        r = respostas(pedido)
        if isinstance(r, Exception):
            raise r
        return _Resp(json.dumps(r).encode("utf-8"))
    return abrir


def respostas_ok(pedido):
    if pedido.full_url.startswith(OE.JUPITER_QUOTE_URL):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pedido.full_url).query)
        assert q["inputMint"] == [OE.USDC_MINT] and q["outputMint"] == [MINT] and q["amount"] == ["5420000"]
        # 5.42 USD -> 1000 tokens (6 decimais) => preco efetivo 0.00542; referencia 0.005 => +8.4%
        return {"outAmount": "1000000000", "priceImpactPct": "0.031", "routePlan": [{"swapInfo": {"label": "Raydium"}}]}
    corpo = json.loads(pedido.data.decode("utf-8"))
    if corpo["method"] == "getTokenSupply":
        return {"result": {"value": {"amount": "1000"}}}
    if corpo["method"] == "getTokenLargestAccounts":
        return {"result": {"value": [{"amount": str(a)} for a in (300, 100, 50, 50, 20, 10, 10, 10, 10, 10, 5)]}}
    raise AssertionError(corpo["method"])


class AuditorFalso:
    def audit_mint(self, mint):
        return {"quality_status": "valid", "decimals": 6, "mint_authority_state": "revoked",
                "freeze_authority_state": "active", "token_program": "spl_token"}


class ObservacaoExtraTests(unittest.TestCase):
    def test_concentracao_holders(self):
        r = OE.concentracao_holders(MINT, opener=opener_falso(respostas_ok))
        self.assertEqual(r["estado"], "ok")
        self.assertEqual(r["top1_pct"], 30.0)
        self.assertEqual(r["top10_pct"], 57.0)

    def test_donos_das_maiores_contas(self):
        quantidades = (300, 100, 50, 50, 20, 10, 10, 10, 10, 10, 5, 4, 3)
        contas = ["1" * 31 + letra for letra in "ABCDEFGHJKLMN"]
        donos = {conta: "2" * 31 + conta[-1] for conta in contas}
        pedidos = []

        def respostas(pedido, falhar=False):
            corpo = json.loads(pedido.data.decode("utf-8"))
            pedidos.append(corpo["method"])
            if corpo["method"] == "getTokenSupply":
                return {"result": {"value": {"amount": "1000", "decimals": 6}}}
            if corpo["method"] == "getTokenLargestAccounts":
                return {"result": {"value": [{"address": c, "amount": str(q)} for c, q in zip(contas, quantidades)]}}
            if corpo["method"] == "getMultipleAccounts":
                if falhar:
                    return {"error": {"code": -32005, "message": "limite"}}
                pedidas, opcoes = corpo["params"]
                assert opcoes["encoding"] == "jsonParsed" and pedidas == contas[:12]
                return {"result": {"value": [None if c == contas[2] else
                                             {"data": {"parsed": {"info": {"owner": donos[c]}}}} for c in pedidas]}}
            raise AssertionError(corpo["method"])

        r = OE.concentracao_holders(MINT, opener=opener_falso(respostas))
        self.assertEqual((r["estado"], r["top1_pct"], r["top10_pct"]), ("ok", 30.0, 57.0))
        self.assertEqual(len(r["top"]), 12)
        self.assertEqual(r["top"][0], [donos[contas[0]], 30.0])
        self.assertEqual(r["top"][2], [None, 5.0])               # conta sem dados: dono desconhecido
        self.assertEqual(pedidos.count("getMultipleAccounts"), 1)
        falha = OE.concentracao_holders(MINT, opener=opener_falso(lambda p: respostas(p, falhar=True)))
        self.assertEqual((falha["estado"], falha["top10_pct"], falha["top"]), ("ok", 57.0, None))
        antigo = OE.concentracao_holders(MINT, opener=opener_falso(respostas_ok))  # sem enderecos: nem pede
        self.assertEqual((antigo["estado"], antigo["top"]), ("ok", None))

    def test_cotacao_jupiter_impacto_e_slippage_real(self):
        r = OE.cotacao_jupiter(MINT, 5.42, 0.005, 6, opener=opener_falso(respostas_ok))
        self.assertEqual(r["estado"], "ok")
        self.assertEqual(r["impacto_pct"], 3.1)
        self.assertAlmostEqual(r["slippage_real_pct"], 8.4, places=2)
        self.assertEqual(r["rota"], "Raydium")

    def test_observar_completo_e_sem_excecoes_quando_rede_falha(self):
        r = OE.observar(MINT, 5.42, 0.005, 2.07, opener=opener_falso(respostas_ok), auditor=AuditorFalso())
        self.assertEqual(r["modo"], "observacao")
        self.assertEqual(r["mint"]["freeze_authority"], "active")
        self.assertEqual(r["holders"]["top10_pct"], 57.0)
        self.assertEqual(r["jupiter"]["impacto_pct"], 3.1)
        sem_rede = opener_falso(lambda p: OSError("sem rede"))
        r2 = OE.observar(MINT, 5.42, 0.005, opener=sem_rede, auditor=AuditorFalso())
        self.assertEqual(r2["holders"]["estado"], "indisponivel")
        self.assertEqual(r2["jupiter"]["estado"], "indisponivel")
        self.assertEqual(OE.observar("invalido", 5, 1)["erro"], "mint_invalido")

    def test_observador_em_segundo_plano_uma_vez_por_mint_e_inativo_por_defeito(self):
        chamadas = []

        def fn(mint, valor, preco, sim):
            chamadas.append(mint)
            return {"modo": "observacao", "holders": {"top10_pct": 10}}
        inativo = OE.ObservadorExtra(ativo=False, observar_fn=fn)
        self.assertFalse(inativo.pedir(MINT, 5, 1))
        obs = OE.ObservadorExtra(ativo=True, observar_fn=fn)
        self.assertTrue(obs.pedir(MINT, 5, 1))
        for _ in range(100):
            if obs.obter(MINT):
                break
            time.sleep(0.01)
        self.assertEqual(obs.obter(MINT)["holders"]["top10_pct"], 10)
        self.assertFalse(obs.pedir(MINT, 5, 1))
        self.assertEqual(chamadas, [MINT])

    def test_bot_observacao_inativa_por_defeito_e_laboratorio_le_obs(self):
        import bot as B
        self.assertTrue(B.CONFIG["observacao_extra"])
        cfg = copy.deepcopy(B.CONFIG)
        regra = (cfg["stop_loss_pct"], cfg["take_profit_pct"], cfg["trailing_ativar_pct"],
                 cfg["trailing_distancia_pct"], cfg["tempo_maximo_min"])
        pcs = [{"pontuacao": 75, "ts": 1, "preco0": 1.0, "liq0": 20000, "fim": "rug", "liq_fim": 100,
                "pts": [[0, 1.0], [60, 1.3], [120, 0.2]],
                "obs": {"holders": {"top10_pct": 80}, "mint": {"mint_authority": "revoked", "freeze_authority": "revoked"},
                        "jupiter": {"slippage_real_pct": 6.5}, "slippage_simulado_pct": 2.05}},
               {"pontuacao": 75, "ts": 2, "preco0": 1.0, "liq0": 20000, "fim": "tempo", "liq_fim": 20000,
                "pts": [[0, 1.0], [60, 0.9]]}]
        texto = "\n".join(B._secao_observacao_extra(pcs, regra, cfg))
        self.assertIn("1 de 2 percursos", texto)
        self.assertIn("top10 holders > 70%", texto)
        self.assertIn("real Jupiter medio 6.50%", texto)
        self.assertIn("ainda nao filtram compras", texto)
        self.assertIn("Ainda sem dados", "\n".join(B._secao_observacao_extra(pcs[1:], regra, cfg)))


class RpcConfiguravelTests(unittest.TestCase):
    CHAVE = "abcd1234-ef56-7890-abcd-1234567890ab"

    def test_ordem_ambiente_ficheiro_publico(self):
        import os, tempfile
        url, origem = OE.configurar_rpc(ambiente={"HELIUS_API_KEY": self.CHAVE})
        self.assertEqual(origem, "helius")
        self.assertTrue(url.startswith("https://mainnet.helius-rpc.com/?api-key="))
        with tempfile.TemporaryDirectory() as pasta:
            self.assertEqual(OE.configurar_rpc(pasta, ambiente={})[1], "publico")
            with open(os.path.join(pasta, OE.FICHEIRO_CHAVES), "w", encoding="utf-8") as f:
                json.dump({"helius_api_key": self.CHAVE}, f)
            self.assertEqual(OE.configurar_rpc(pasta, ambiente={})[1], "helius")
        self.assertEqual(OE.configurar_rpc(ambiente={"HELIUS_API_KEY": "chave com espacos!"})[1], "publico")
        self.assertEqual(OE.configurar_rpc(ambiente={"SOLANA_RPC_URL": "http://inseguro"})[1], "publico")

    def test_chave_nunca_aparece_no_resultado_e_decimais_vem_do_supply(self):
        url, origem = OE.configurar_rpc(ambiente={"HELIUS_API_KEY": self.CHAVE})
        vistos = []

        def respostas(pedido):
            vistos.append(pedido.full_url)
            if pedido.full_url.startswith(OE.JUPITER_QUOTE_URL):
                return {"outAmount": "1000000000", "priceImpactPct": "0.01", "routePlan": []}
            corpo = json.loads(pedido.data.decode("utf-8"))
            if corpo["method"] == "getAccountInfo":
                return {"error": {"code": -1, "message": "x"}}
            if corpo["method"] == "getTokenSupply":
                return {"result": {"value": {"amount": "1000", "decimals": 6}}}
            return {"result": {"value": [{"amount": "100"}]}}
        r = OE.observar(MINT, 5.42, 0.005, 2.0, opener=opener_falso(respostas), rpc_url=url, origem_rpc=origem)
        self.assertEqual(r["rpc"], "helius")
        self.assertEqual(r["holders"]["decimais"], 6)
        self.assertAlmostEqual(r["jupiter"]["slippage_real_pct"], 8.4, places=2)  # decimais vieram do supply
        self.assertNotIn(self.CHAVE, json.dumps(r))
        self.assertTrue(any(self.CHAVE in u for u in vistos))  # a chave so vai no pedido ao RPC

    def test_erro_de_ligacao_nao_expoe_url(self):
        import urllib.error
        falha = opener_falso(lambda p: urllib.error.URLError("falhou em https://mainnet.helius-rpc.com/?api-key=" + self.CHAVE))
        r = OE.concentracao_holders(MINT, opener=falha)
        self.assertTrue(r["erro"].startswith("sem_ligacao"))
        self.assertNotIn(self.CHAVE, r["erro"])


class GeckoTerminalTests(unittest.TestCase):
    MINT_G = "GeCKo1111111111111111111111111111111111pump"
    MINT_D = "DexS11111111111111111111111111111111111pump"

    def _http(self):
        teste = self

        class Http:
            pedidos = []

            def get_json(self, url, grupo="pares", tentativas=None):
                self.pedidos.append(url)
                import bot as B
                if url == B.URL_PERFIS:
                    return [{"chainId": "solana", "tokenAddress": teste.MINT_D}]
                if url == B.URL_BOOSTS:
                    return []
                if url == B.URL_GECKO_NOVAS_POOLS:
                    return {"data": [
                        {"attributes": {"reserve_in_usd": "25000"}, "relationships": {"base_token": {"data": {"id": "solana_" + teste.MINT_G}}}},
                        {"attributes": {"reserve_in_usd": "900"}, "relationships": {"base_token": {"data": {"id": "solana_Pouca1111111111111111111111111111111111"}}}},
                        {"attributes": {"reserve_in_usd": "50000"}, "relationships": {"base_token": {"data": {"id": "solana_" + teste.MINT_D}}}},
                        {"attributes": {"reserve_in_usd": "50000"}, "relationships": {"base_token": {"data": {"id": "eth_0xabc"}}}},
                    ]}
                raise AssertionError(url)
        return Http()

    def test_inativo_por_defeito_nao_consulta_gecko(self):
        import bot as B
        http = self._http()
        sc = B.Scanner(copy.deepcopy(B.CONFIG), http)
        self.assertEqual(sc.tokens_recentes(), [self.MINT_D])
        self.assertNotIn(B.URL_GECKO_NOVAS_POOLS, http.pedidos)
        self.assertEqual(sc.origem_gecko, set())

    def test_ativo_filtra_liquidez_e_marca_so_tokens_exclusivos(self):
        import bot as B
        http = self._http()
        sc = B.Scanner(copy.deepcopy(B.CONFIG), http)
        sc.gecko_ativo = True
        mints = sc.tokens_recentes()
        self.assertEqual(mints, [self.MINT_D, self.MINT_G])
        self.assertEqual(sc.origem_gecko, {self.MINT_G})
        self.assertEqual(sc.last_gecko_count, 2)
        # segunda chamada dentro do intervalo nao volta a pedir ao GeckoTerminal
        sc.tokens_recentes()
        self.assertEqual(http.pedidos.count(B.URL_GECKO_NOVAS_POOLS), 1)

    def test_tokens_gecko_nunca_chegam_a_proposta(self):
        import inspect
        import bot as B
        fonte = inspect.getsource(B.Bot)
        guarda = fonte.index('if mint in getattr(self.scanner, "origem_gecko", ())')
        plano = fonte.index("self.executor.preparar_execucao(mint, par, valor)")
        self.assertLess(guarda, plano)
        self.assertIn("[GeckoTerminal - observacao] - sem proposta", fonte)


if __name__ == "__main__":
    unittest.main()
