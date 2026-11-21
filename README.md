# SpiderBot (MemeBot v12) — Paper Trading de meme coins na Solana

> **Só simulação.** Este bot não usa dinheiro real, não tem carteiras reais, não assina nem envia
> transações e não guarda chaves privadas ou seed phrases. Os resultados são simulados com preços reais
> do mercado e custos estimados, e não garantem resultados futuros. Não é aconselhamento financeiro.

## O que faz

1. **Scanner** — procura tokens novos na Solana (DexScreener e GeckoTerminal).
2. **Analista** — dá uma pontuação de 0 a 100 (liquidez, market cap/liquidez, idade, volume, pressão
   compradora, momento, acumulação e RugCheck) e aprova ou recusa.
3. **Executor** — com a tua **confirmação manual** (Telegram ou dashboard privado, 90 s), simula a compra
   com slippage, impacto e taxas. A cotação Jupiter é usada como referência.
4. **Risco** — vigia as posições e vende em Paper no stop-loss, alvo, trailing stop, tempo máximo ou
   sinal de rug.
5. **Laboratório** (`python bot.py --analisar`) — testa regras nos percursos reais gravados, com grupo
   de controlo, prova dos nove e hipóteses congeladas, antes de qualquer mudança de estratégia.

Também permite analisar, acompanhar, comprar e vender à mão tokens escolhidos por ti (sempre em Paper):
valor de 1 a 5 € por compra, stop-loss editável enquanto a posição está aberta e venda de 10/25/50/75%.
As decisões manuais ficam separadas das estatísticas da estratégia.

## O que NÃO faz

- Não negoceia com dinheiro real (`mode=paper` e `live_trading_enabled=false`, verificados no arranque
  e antes de cada compra).
- Não compra sem confirmação manual.
- Não faz compras parciais: o saldo tem de cobrir a ordem completa.

## Limites de segurança

| Limite | Valor |
|---|---|
| Valor por operação | máximo 5 EUR (taxa BCE bloqueada na sessão) |
| Posições abertas | configurável de 1 a 5 (`config_operacoes.json`) |
| Slippage | máximo aprovado em `config_operacoes.json` |
| Paragem de emergência | bloqueia novas propostas e compras |

## Instalar e correr (Paper)

Requer Python 3.10 ou superior.

```bash
pip install -r requirements.txt
cp config_operacoes.example.json config_operacoes.json   # rever os valores antes de arrancar
python bot.py              # arranca em Paper
python bot.py --analisar   # relatório do laboratório (só leitura)
```

- Dashboard privado: `http://127.0.0.1:8000`. Abre só na própria máquina; para o ver de outro
  computador usa um túnel SSH. **Nunca** o abras à internet.
- Página pública (só leitura, sem botões): `pagina_publica.py`.
- Telegram (opcional): as chaves ficam em variáveis de ambiente ou ficheiros locais ignorados pelo Git.
  Nunca as coloques no código nem no repositório.
- Corre **uma só instância** de cada vez com o mesmo `estado.json`.

## Testes

```bash
python teste_offline.py
python -m unittest discover -p "teste_*.py"
```

## Ficheiros principais

| Ficheiro | Para quê |
|---|---|
| `bot.py` | bot (scanner, analista, executor, risco, Telegram, dashboard) |
| `laboratorio.py` | laboratório (`--analisar`): só leitura, separado do `bot.py` a 21/11/2026 |
| `paper_trading_limits.py` | política Paper: limites, taxa BCE, validações |
| `paper_operations.py`, `paper_audit_log.py` | diário de operações e auditoria Paper |
| `pagina_publica.py` | página pública só de leitura |
| `assets/dashboard_neural.html` | dashboard privado |
| `observacao_extra.py`, `early_accumulation.py`, `preco_rapido.py` | dados extra (holders, carteiras, Jupiter) |
| `smart_*.py`, `wallet_*.py` | módulos de observação de carteiras (consultivos; não decidem compras) |

## Mais informação

- `LEIA-ME.md` — registo detalhado de alterações e funcionalidades.
- `REQUISITOS_OPERACAO_MANUAL.md` — requisitos de segurança da confirmação manual.
- `AUDITORIA_FASE1.md`, `AUDITORIA_INTEGRACAO_V7.md` — auditorias.

## Acrescentar um tema ao dashboard

Tudo fica em `assets/dashboard_neural.html`. Um tema novo (ex.: `meutema`) precisa de 4 coisas:

1. **Bloco de cores** (no `<style>`, junto dos outros `html[data-tema="..."]`): copia o bloco de um tema existente
   (ex.: `carmesim`) e troca as cores. Sem este bloco, os painéis ficam com as cores do Neural.
2. **Nome no menu**: em `TEMAS_NOMES` acrescenta `"meutema": "Nome que aparece"`.
3. **Cores dos gráficos**: em `TEMAS_MAPA` acrescenta `"meutema": { ... }` com as mesmas chaves dos outros temas
   (as cores do Neural à esquerda, as do teu tema à direita).
4. **Fundo ao abrir a página**: no primeiro `<script>`, em `bgs`, acrescenta `meutema:"#corDeFundo"`.

Opcional: CSS só deste tema com `html[data-tema="meutema"] ...` (fundo, brilhos, animações).
Para instalar no servidor basta copiar o ficheiro para `assets/` (não é preciso reiniciar o bot).
