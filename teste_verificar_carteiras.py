# -*- coding: utf-8 -*-
"""Testes offline do verificar_carteiras.py (sem rede)."""
import io
import json
import os
import tempfile
import unittest

import observacao_extra as OE
import verificar_carteiras as V

A, B, C, D = ("So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
              "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(tabela):
    def abrir(pedido, timeout=None):
        corpo = json.loads(pedido.data.decode("utf-8"))
        assert corpo["method"] == "getSignaturesForAddress" and corpo["params"][1] == {"limit": V.LIMITE_ASSINATURAS}
        r = tabela[corpo["params"][0]]
        if isinstance(r, Exception):
            raise r
        return _Resp(json.dumps(r).encode("utf-8"))
    return abrir


class VerificarCarteirasTests(unittest.TestCase):
    def test_ler_candidatas_filtra_invalidas_e_repetidas(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = os.path.join(pasta, "c.json")
            with open(caminho, "w", encoding="utf-8") as f:
                json.dump({"candidatas": [{"nome": "um", "endereco": A, "fonte": "x"}, {"endereco": A}, B,
                                          {"endereco": "SUBSTITUIR"}, {"endereco": "1" * 31 + "0"}, 7]}, f)
            self.assertEqual(V.ler_candidatas(caminho), [("um", A, "x"), ("?", B, "?")])

    def test_atividade_e_escolha(self):
        agora = 1_800_000_000
        tabela = {A: {"result": [{"blockTime": agora - 3600}, {"blockTime": agora - 90000}, {"blockTime": None}]},
                  B: {"result": []},
                  C: {"result": [{"blockTime": agora - 30 * 86400}]},
                  D: {"error": {"code": -32005}}}
        medir = lambda e: dict(V.atividade(e, "https://rpc.invalid", agora=agora, opener=opener(tabela)), endereco=e, nome=e[:3])
        a, b, c, d = medir(A), medir(B), medir(C), medir(D)
        self.assertEqual((a["estado"], round(a["ultima_h"], 2), a["tx_24h"]), ("ok", 1.0, 1))
        self.assertEqual((b["estado"], b["tx_24h"]), ("vazia", 0))
        self.assertEqual((c["estado"], round(c["ultima_h"])), ("ok", 720))
        self.assertEqual(d["estado"], "erro")
        self.assertEqual([r["endereco"] for r in V.escolher([c, b, d, a], dias=7)], [A])   # so a ativa
        self.assertEqual([r["endereco"] for r in V.escolher([c, a], dias=60)], [A, C])       # mais ativa primeiro
        self.assertEqual(len(V.escolher([a, c], dias=60, maximo=1)), 1)
        self.assertEqual(len(V.escolher([a] * 100, dias=7, maximo=999)), OE.MAX_CARTEIRAS)

    def test_gravar_lista_e_lida_pelo_bot(self):
        with tempfile.TemporaryDirectory() as pasta:
            escolhidas = [{"endereco": A, "nome": "um"}, {"endereco": B, "nome": "dois"}]
            destino = V.gravar_lista(pasta, escolhidas)
            self.assertEqual(OE.ler_carteiras_seguidas(pasta), [A, B])
            self.assertEqual(os.stat(destino).st_mode & 0o777, 0o600)
            V.gravar_lista(pasta, escolhidas[:1])
            self.assertEqual(OE.ler_carteiras_seguidas(pasta), [A])
            with open(destino + ".bak", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["wallets"], [A, B])                            # anterior preservado

    def test_sem_ficheiro_de_candidatas_nao_altera_nada(self):
        with tempfile.TemporaryDirectory() as pasta:
            self.assertEqual(V.main(["--pasta", pasta, "--gravar"]), 1)
            self.assertFalse(os.path.exists(os.path.join(pasta, OE.FICHEIRO_CARTEIRAS)))


if __name__ == "__main__":
    unittest.main()
