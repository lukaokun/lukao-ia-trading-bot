# -*- coding: utf-8 -*-
"""Verificacao (so leitura) antes de ligar os botoes Aprovar/Recusar no Telegram.

Confirma duas coisas, sem mostrar chaves nem ids:
  1. o telegram_chat_id do MemeBot e um chat privado (num grupo, outros membros poderiam aprovar);
  2. o DILAN AI nao usa o mesmo bot do Telegram (dois programas a ler o mesmo bot entram em conflito).
Uso (no servidor):  python3 verificar_telegram.py
"""
import json
import os
import sys

PASTA = os.path.dirname(os.path.abspath(__file__))
DILAN = sys.argv[1] if len(sys.argv) > 1 else "/home/bot/dilanbot"


def main():
    try:
        with open(os.path.join(PASTA, "chaves_locais.json"), encoding="utf-8-sig") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        print("ERRO: nao consegui ler o chaves_locais.json")
        return 1
    chat = str(dados.get("telegram_chat_id") or "").strip()
    numero = str(dados.get("telegram_token") or "").split(":")[0].strip()
    print("Chat do MemeBot:", "privado (OK)" if chat.isdigit() else "GRUPO ou vazio (os botoes ficam desligados)")
    if not numero.isdigit():
        print("ERRO: telegram_token sem o formato esperado")
        return 1
    mesmo = False
    for raiz, pastas, ficheiros in os.walk(DILAN):
        pastas[:] = [p for p in pastas if p not in (".venv", "venv", "__pycache__", ".git", "backups")]
        for nome in ficheiros:
            if nome.endswith((".log", ".jsonl")):
                continue
            caminho = os.path.join(raiz, nome)
            try:
                if os.path.getsize(caminho) > 5_000_000:
                    continue
                with open(caminho, encoding="utf-8", errors="ignore") as f:
                    if numero + ":" in f.read():
                        mesmo = True
            except OSError:
                continue
    print("DILAN AI usa o mesmo bot do Telegram:", "SIM (nao ligar os botoes)" if mesmo else "NAO (OK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
