# MemeBot v12 — Paper Trading

O MemeBot v12 analisa tokens Solana e simula operações. Esta versão é exclusivamente de Paper Trading.

## Segurança e limites operacionais

- `mode` tem de permanecer `paper`.
- `live_trading_enabled` tem de permanecer `false`.
- Não existem assinaturas nem envio de transações reais. Não introduza chaves privadas, seeds ou credenciais no projeto.
- Cada compra exige confirmação manual explícita, por operação: no terminal (identificador completo + código da proposta) ou no Dashboard local (botão Aprovar seguido de "Sim, aprovar"). O Dashboard só pode aprovar ou recusar propostas Paper já criadas pelo bot; não cria compras, não vende e não altera limites. Autorizado pelo operador em 14/11/2026 **apenas para Paper Trading**; tem de ser revisto antes de qualquer execução real.
- O limite nominal é 5 EUR por operação, convertido para USD pela taxa BCE da sessão. Se a estratégia calcular menos, mantém-se o valor inferior. Uma ordem que não caiba integralmente no saldo é bloqueada; não há compra parcial.
- O bot não aceita novas compras enquanto houver uma posição aberta. Posições legadas não são liquidadas à força para impor o limite.
- Uma taxa BCE ausente, inválida, expirada ou não persistida bloqueia novas compras.

A configuração de exemplo está em `config_operacoes.example.json`. A configuração local `config_operacoes.json` é ignorada pelo Git. Confirme os valores de segurança antes de qualquer execução.

## Dashboard

Com o bot já em execução, o Dashboard Neural está disponível apenas na máquina local em <http://127.0.0.1:8000> (o anterior continua em `/classico`). Não exponha a porta, não crie encaminhamento de portas e não permita acesso pela rede local.

O único pedido de escrita é `POST /api/proposta/decisao` (aprovar ou recusar uma proposta Paper pendente). O servidor só o aceita com o endereço e a origem locais, uma chave secreta gerada em cada arranque e servida apenas à própria página, modo Paper com confirmação manual e, para aprovar, sem paragem de emergência ativa. Cada decisão pede confirmação ("Sim") na página e o Executor revalida tudo antes de executar.

## Alterações de 14/11/2026

- Limite máximo de slippage simulado aprovado: 3% (`APPROVED_MAX_SLIPPAGE_PCT` e `max_slippage_pct`).
- `emergency_stop_enabled` passou a ser carregado da configuração (os comandos `PARAR_PAPER`/`REARMAR_PAPER` ficam funcionais).
- Observação extra (só leitura, não decide compras): permissões do mint, concentração de holders (RPC público Solana) e cotação Jupiter para tokens aprovados — `observacao_extra.py`.
- GeckoTerminal como fonte extra de descoberta em modo observação: tokens encontrados só por esta fonte são analisados e gravados, mas nunca geram propostas.
- Decisão de propostas no Dashboard (ver acima).
- Preço rápido da posição aberta (`preco_rapido.py`, só leitura): cotação de VENDA no Jupiter de 3 em 3 segundos; stops, alvo e trailing passam a usar esse preço. A liquidez (regras de rug) continua a vir da DexScreener, lida de 5 em 5 segundos com posição aberta. Se o Jupiter falhar, o bot volta automaticamente à DexScreener.
- Antes de cada proposta, confirma no Jupiter que existe rota de compra e que o impacto real não passa de 1,5% (`impacto_compra_jupiter_max_pct`). Se o Jupiter não responder, não bloqueia.
- Avisos de proposta: botão "Ativar avisos" no Dashboard (som, notificação do sistema e título a piscar) e mensagem por Telegram, se configurado.
- Laboratório: grupo de controlo (tokens recusados com pontuação, seguidos como se fossem sinais) e características de entrada (subida 5 min/1 h, compras/vendas, idade) em cada percurso; `python bot.py --analisar` mostra aprovados vs. recusados e resultados por característica.
- O log deixa de repetir a mesma análise do mesmo token a cada ciclo (só regista quando o motivo muda).

### Ficheiro local `chaves_locais.json` (opcional, nunca vai para o Git)

```json
{
  "helius_api_key": "...",
  "jupiter_api_key": "...",
  "telegram_token": "...",
  "telegram_chat_id": "..."
}
```

Todas as chaves são opcionais. Nunca são registadas no log nem mostradas no Dashboard. Não partilhe este ficheiro.

## Componentes

- `bot.py` contém o Scanner, o Analista, o Executor Paper, o Risk e a aplicação web.
- `paper_trading_limits.py` trata os limites operacionais, a taxa BCE e a exclusividade da instância.
- `paper_operations.py` mantém o diário idempotente das operações Paper.
- `smart_filters_market.py` recolhe snapshots observacionais de mercado; `smart_protection_observability.py` regista avaliações de risco para observabilidade.
- `smart_filters_security.py`, os módulos Smart Money, o monitor de carteiras e o decoder são componentes separados; não os interprete como critérios integrados no decisor principal sem verificar o ponto de ligação no código.

As fontes públicas consultadas podem falhar ou impor limites. Dados indisponíveis não são garantia de segurança nem sinal para comprar.

## Preparação local

1. Use Python 3.10 ou superior e instale as dependências declaradas em `requirements.txt`.
2. Para uma primeira configuração local, copie `config_operacoes.example.json` para `config_operacoes.json`; não substitua uma configuração existente sem a rever.
3. Confirme `mode=paper`, `live_trading_enabled=false`, confirmação manual, máximo de 5 EUR e uma posição aberta.
4. Antes de iniciar, confirme que não existe outra instância do bot a usar o mesmo estado.
5. Só então, se decidir iniciar o Paper Trading, execute `python bot.py`. Para encerramento normal, use Ctrl+C. Não use reset como procedimento de arranque.

Não abra o Dashboard noutros dispositivos nem altere as configurações de rede.

## Testes

Execute testes apenas numa cópia isolada, nunca sobre os dados operacionais. Os testes podem criar ficheiros temporários ou caches. Os testes de decoder/pipeline que dependem de fixtures on-chain não fazem parte da seleção inicial do repositório.

```text
python -m py_compile bot.py paper_operations.py paper_trading_limits.py
python -m unittest discover -p "teste_*.py" -v
python teste_offline.py
```

## Dados locais e publicação

Estado, histórico de operações, logs, bases SQLite, fixtures, watchlists reais, credenciais e backups são dados locais e estão excluídos pelo `.gitignore`. Não confie apenas no `.gitignore`: reveja o conteúdo e os ficheiros staged antes de cada commit. A configuração de exemplo e `wallet_watchlist.example.json` contêm apenas parâmetros sanitizados/placeholders.

`AUDITORIA_FASE1.md` e `AUDITORIA_INTEGRACAO_V7.md` são relatórios históricos, não especificações do estado atual. `REQUISITOS_OPERACAO_MANUAL.md` descreve requisitos futuros, não autoriza nem ativa execução real.
