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

## Alterações de 15/11/2026 (scan)

Nenhuma destas alterações muda a estratégia (filtros, stop, alvo, trailing, tempo) nem os limites Paper.

- **Stops com prioridade sobre o scanner.** Com uma posição aberta, o scanner cede a vez ao preço rápido entre os seus pedidos e entre análises, e os pedidos HTTP passam a 1 tentativa com `timeout_com_posicao_seg` (4 s). Depois de um 429 o bot não fica parado à espera: salta esse grupo de pedidos durante a pausa.
- **Avisos Telegram do preço rápido.** Se a posição aberta ficar sem cotação Jupiter (ou sem rota de venda), o bot avisa no Telegram, no máximo 1 vez por minuto por token e por tipo. É só um aviso: não vende por causa disso.
- **Pontos finos no laboratório (só gravação).** `percursos_finos` grava em `ptsj` o preço de venda Jupiter dos sinais nos primeiros `percurso_fino_minutos` (30), 1 cotação de `percurso_fino_intervalo_seg` em `percurso_fino_intervalo_seg` (5 s) no total, e fica parado enquanto houver posição aberta. O laboratório (`--analisar`) continua a usar apenas `pts`; os pontos finos ficam guardados para uma decisão posterior.
- **Fonte do preço na observabilidade.** As avaliações de risco feitas com preço Jupiter ficam registadas como `Jupiter+DexScreener` (antes diziam sempre `DexScreener`).
- **Página pública.** Ligações paradas são largadas ao fim de 15 s e há um teto de 48 ligações em simultâneo.
- **Laboratório: pontos finos e custos reais.** O relatório (`--analisar`) tem a secção `PONTOS FINOS`, que repete as mesmas regras nos mesmos percursos de três maneiras: pontos normais com custos simulados, pontos finos com custos simulados (o mais parecido com o que o bot Paper regista) e pontos finos com custos reais (cotação Jupiter de entrada, preço de venda Jupiter na saída e `lab_taxa_rede_usd` por transação, que é uma hipótese por medir). O resto do relatório continua igual e nada disto entra em decisões.
- **Laboratório: operações por dia.** A secção `OPERACOES PAPER FECHADAS, POR DIA` mostra o resultado real do bot dia a dia, para comparar períodos com as mesmas regras.
- **Holders sem a pool.** A observação extra guarda o dono das 12 maiores contas (`holders.top`) e cada percurso guarda o endereço do par (`par`). Quando a pool do par está entre as maiores contas, o laboratório mostra também o top 10 sem ela.
- **Carteiras seguidas (só observação).** Se existir `wallet_watchlist.json` (local, nunca vai para o Git) com `{"wallets": ["endereço", ...]}`, em cada sinal aprovado o bot vê quantas dessas carteiras têm o token naquele momento (1 pedido RPC por carteira, máximo 40; precisa da chave Helius) e guarda o resultado em `obs.carteiras`. A lista é lida a cada sinal, por isso mudá-la não obriga a reiniciar. Não decide compras. A lista nunca é enviada para o dashboard nem para a página pública. O laboratório ganha a secção `CARTEIRAS`, que compara os sinais com e sem carteiras seguidas dentro e mostra os donos que se repetem entre as maiores contas (candidatas a entrar na lista).

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
