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
- **Pontos finos no laboratório (só gravação).** `percursos_finos` grava em `ptsj` o preço de venda Jupiter dos sinais durante `percurso_fino_minutos` (180, o percurso inteiro): 1 cotação de 2 em 2 s no total sem posição aberta (30/min) e de 5 em 5 s com posição (12/min, somados aos 20/min do preço rápido; o plano Free do Jupiter dá 60/min). Cada sinal tem no máximo 1 ponto a cada `percurso_fino_min_por_sinal_seg` (10 s). O token da posição aberta recebe os pontos do preço rápido. A secção `PONTOS FINOS` do laboratório usa-os; o resto do relatório continua a usar `pts`.
- **Fonte do preço na observabilidade.** As avaliações de risco feitas com preço Jupiter ficam registadas como `Jupiter+DexScreener` (antes diziam sempre `DexScreener`).
- **Página pública.** Ligações paradas são largadas ao fim de 15 s e há um teto de 48 ligações em simultâneo.
- **Laboratório: pontos finos e custos reais.** O relatório (`--analisar`) tem a secção `PONTOS FINOS`, que repete as mesmas regras nos mesmos percursos de três maneiras: pontos normais com custos simulados, pontos finos com custos simulados (o mais parecido com o que o bot Paper regista) e pontos finos com custos reais (cotação Jupiter de entrada, preço de venda Jupiter na saída e `lab_taxa_rede_usd` por transação, que é uma hipótese por medir). O resto do relatório continua igual e nada disto entra em decisões.
- **Laboratório: operações por dia.** A secção `OPERACOES PAPER FECHADAS, POR DIA` mostra o resultado real do bot dia a dia, para comparar períodos com as mesmas regras.
- **Holders sem a pool.** A observação extra guarda o dono das 12 maiores contas (`holders.top`) e cada percurso guarda o endereço do par (`par`). Quando a pool do par está entre as maiores contas, o laboratório mostra também o top 10 sem ela.
- **Carteiras seguidas (só observação).** Se existir `wallet_watchlist.json` (local, nunca vai para o Git) com `{"wallets": ["endereço", ...]}`, em cada sinal aprovado o bot vê quantas dessas carteiras têm o token naquele momento (1 pedido RPC por carteira, máximo 40; precisa da chave Helius) e guarda o resultado em `obs.carteiras`. A lista é lida a cada sinal, por isso mudá-la não obriga a reiniciar. Não decide compras. A lista nunca é enviada para o dashboard nem para a página pública. O laboratório ganha a secção `CARTEIRAS`, que compara os sinais com e sem carteiras seguidas dentro e mostra os donos que se repetem entre as maiores contas (candidatas a entrar na lista).
- **Escolher as carteiras a seguir.** `python verificar_carteiras.py` lê `carteiras_candidatas.json` (local), confirma por RPC que cada endereço existe e está ativo (1 pedido por carteira, só leitura) e mostra a tabela; com `--gravar` escreve o `wallet_watchlist.json` com as mais ativas (máximo 40), guardando o anterior em `.bak`. Não avalia lucros: as candidatas vêm de sites públicos de ranking e servem apenas para observação.
- **Aprovar e recusar no Telegram.** Com `telegram_botoes` ligado, cada proposta chega ao Telegram com os botões Aprovar e Recusar. Aprovar pede confirmação ("Sim, aprovar") antes de decidir. Só são aceites cliques do chat privado configurado em `telegram_chat_id` (num grupo os botões ficam desligados). A decisão segue pela mesma fila do dashboard, com as mesmas verificações (modo Paper, paragem de emergência, prazo de 90 s, revalidação do plano antes de executar). A mensagem é atualizada com o resultado. Se outro programa estiver a ler o mesmo bot do Telegram (erro 409), os botões desligam-se sozinhos e o bot avisa; as propostas continuam no dashboard.
- **Vender agora pelo Telegram.** Com `telegram_venda_manual` ligado, a mensagem de compra traz o botão "Vender agora", que pede confirmação ("Sim, vender"). O pedido é registado e a venda Paper é feita pelo ciclo principal, ao preço mais recente conhecido (preço rápido Jupiter se estiver fresco). Só aceita o chat privado configurado e só funciona com a posição aberta. Fica registada com o motivo `MANUAL (Telegram)`, separada das saídas automáticas. O laboratório não é afetado.
- **Laboratório: pontuação por critério.** A secção `PONTUACAO POR CRITERIO` mostra, para cada critério da pontuação (liquidez, volume, pressão compradora, etc.), quantos pontos deu em média aos sinais de 70–79 e aos de 80+, e como correram os sinais com mais e com menos pontos nesse critério. Os pontos vêm do percurso (campo `comp`, gravado a partir de 17/11/2026) ou, nos percursos antigos, das linhas `ANALISE` do `bot.log`. É só relatório.

## Alterações de 17/11/2026 (laboratório)

- **Prova dos nove com as regras atuais ao lado:** além da melhor regra da 1.ª metade testada na 2.ª, o relatório mostra as regras atuais nessa mesma 2.ª metade e a diferença entre as duas.
- **Hipóteses congeladas:** secção nova no `--analisar`. As hipóteses escritas a 17/11/2026 com os primeiros 36 sinais (A: saída −20% / +50% / sem trailing / 30 min; B: recusar pontuação 80+) são medidas só nos sinais seguintes (do 37.º em diante), com pontos normais e com pontos finos e custos reais. Estão fixas no código (`HIPOTESES_CONGELADAS`) e não se mudam; decisão só com 30 ou mais sinais novos. Nada disto muda o que o bot compra ou vende.

## Alterações de 18/11/2026 (análise manual)

- **Analisar um token qualquer:** no Telegram (chat privado), cola o endereço do token (ou `/analisar <endereço>`, ou um link do pump.fun/DexScreener); no dashboard privado, botão **🔎 Analisar token** ao lado da caixa Ticker. Mostra a pontuação que o Analista daria, os pontos de cada critério, liquidez, market cap, idade, variações e o motivo. **Só mostra:** não cria propostas, não compra e não mexe no estado. Uma análise de cada vez, mínimo de 5 s entre análises e cache de 60 s (`analise_manual`, `analise_manual_intervalo_seg`, `analise_manual_cache_seg`). O endpoint `GET /api/analisar` só responde em 127.0.0.1 com a chave da sessão.
- **Histórico na análise manual:** junto à pontuação aparece o que aconteceu, com as regras atuais, aos percursos gravados com pontuação parecida (sinais 70–79, 80–89 ou 90–100; abaixo de 70, os recusados do grupo de controlo): acerto, média, % que chegou a +50% e % que acabou em rug.
- **Acompanhar (só laboratório):** botão no Telegram e no painel do dashboard depois de uma análise. O bot segue o preço desse token durante `percurso_minutos` (3 h) e no fim avisa no Telegram como teria corrido com as regras atuais (pico, mínimo, rug, resultado). Nunca compra. Máximo `percursos_manuais_max` (5) ao mesmo tempo; não usam pontos finos do Jupiter. Ficam gravados com `grupo: manual` e **não entram em nenhuma estatística** do laboratório; aparecem só na secção `TOKENS ACOMPANHADOS A MAO`. No dashboard o pedido é `POST /api/acompanhar` (endereço local, origem local e chave da sessão).
- **Acompanhados ao vivo:** botão **👁 Acompanhados** no dashboard privado (atualiza de 5 em 5 s: gráfico desde o pedido, variação atual, pico, mínimo, liquidez, compras, tempo que falta e o que as regras do bot fariam agora) e comando `/acompanhados` no Telegram. Também lista os terminados há pouco. Só leitura (`GET /api/acompanhados`, endereço local e chave da sessão); a lista é preparada no ciclo principal.
- **Stop alternativo nos acompanhados (só comparação):** cada token acompanhado mostra as regras do bot e as mesmas regras com stop `acompanhar_stop_alternativo_pct` (−25%) lado a lado: no painel (linha laranja no gráfico), no `/acompanhados`, no aviso de fim e na secção `TOKENS ACOMPANHADOS A MAO` do laboratório (média das duas e em quantos o stop alternativo foi melhor/pior). O bot continua a usar só o stop atual.
- **Comprar / vender os tokens que analisaste (Paper):** botão 🟢 Comprar na análise (Telegram e dashboard) e nos acompanhados. Não compra direto: cria uma **proposta** igual às do bot ("PROPOSTA MANUAL"), que confirmas, e o Executor revalida tudo (5 EUR, 1 posição, slippage, rota, paragem de emergência). Vender: botão 🔴 no Telegram (já existia) e no painel Acompanhados (`POST /api/manual`, com confirmação). As compras manuais ficam com `origem: manual`; no `trades.csv` a compra tem motivo `manual` e a venda acaba em `[manual]`; o laboratório mostra-as à parte (`COMPRAS MANUAIS`).
- **Impacto máximo nas compras manuais:** antes de propor, o bot confirma no Jupiter o impacto da ordem. Nas propostas do bot o máximo é `impacto_compra_jupiter_max_pct` (1,5%); nas tuas compras manuais é `impacto_compra_manual_max_pct` (3%), nunca acima do slippage máximo da política Paper.
- **Recuperar entrada (só posições manuais):** vende só a parte dos tokens que devolve a entrada (com a posição pelo menos `recuperar_entrada_lucro_min_pct` = 10% acima); o resto continua com o stop, o trailing e o tempo. Fica registada como venda `PARCIAL` (sem P&L; não conta como operação fechada) e o resultado final soma a parte recebida. Os acompanhados mostram também a simulação "recuperar entrada a 2×".

## Alterações de 18/11/2026 (laboratório: hipóteses de entrada)

- As hipóteses A (saída −20/+50/30 min) e B (recusar 80+) falharam com 41 sinais novos (perderam para as regras atuais com pontos normais e com pontos finos/custos reais) e ficam postas de parte. Nenhuma das 320 combinações de saída deu média positiva: o problema está nas entradas.
- Nova secção `HIPOTESES DE ENTRADA CONGELADAS` (`HIPOTESES_ENTRADA`), escrita com os primeiros 77 sinais e medida só do 78.º em diante: **C)** só entrar em tokens com 30 min ou mais; **D)** depois do sinal, esperar um recuo de 15–35% e só então entrar. Decisão com 30+ sinais novos. Só o relatório muda; o bot compra e vende como antes.

## Alterações de 19/11/2026 (até 5 posições)

- Limite de posições abertas ao mesmo tempo passou de 1 para **5** (autorizado pelo utilizador). O teto absoluto está em `paper_trading_limits.MAX_OPEN_POSITIONS = 5`; `config_operacoes.json` escolhe entre 1 e 5 (acima disso as compras ficam bloqueadas). Cada operação continua com máximo 5 EUR e confirmação manual. Com várias posições, o preço rápido do Jupiter abranda (1,5 s por posição) para não passar ~60 pedidos/min. O painel "Posição aberta" mostra separadores quando há mais de uma.

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

## Valor escolhido nas compras à mão e tolerância de preço (19/11/2026)

- Compras à mão: escolhes o valor de 1 € a 5 € (máximo por operação continua 5 €). Dashboard: botões 1–5 € ou valor livre. Telegram: botões 1–5 € depois de "Comprar", ou `/comprar <endereço> <valor>`.
- O valor escolhido é uma ordem completa desse tamanho (sem compras parciais); nunca acima do máximo permitido.
- Ao aprovares uma proposta, se o preço mudou: aceita se desceu ou subiu até `tolerancia_preco_aprovacao_pct` (3%) e a liquidez não caiu mais de `tolerancia_liquidez_aprovacao_pct` (20%). O limite de slippage continua a ser verificado. Senão é anulada como antes. A variação aparece no registo e no Telegram.

## Stop-loss escolhido por ti e vender parte da posição (19/11/2026)

- Em qualquer posição aberta podes mudar o stop-loss quando quiseres, de -50% a +50% face à entrada (acima de 0 garante lucro). Dashboard: janela "Ver ao vivo" e painel Acompanhados. Telegram: botão "🛑 Mudar stop-loss" nas mensagens de compra, ou `/stop <endereço> <valor>` (sem sinal conta como negativo).
- Se o preço já estiver abaixo do novo stop, a posição vende no ciclo seguinte (o dashboard pede confirmação).
- Vender parte: 10%, 25%, 50% ou 75% dos tokens, com confirmação. O resto continua aberto com as mesmas regras.
- Uma posição do bot onde mudes o stop ou vendas uma parte passa a contar como decisão tua ([manual]): sai das estatísticas da estratégia no laboratório. Os percursos do laboratório não mudam.
- A venda pelo teu stop aparece como `STOP_MANUAL`.

## Entrada na queda (hipótese D) — 21/11/2026

- Com 52 sinais novos, a hipótese D (entrar só depois de o preço recuar 15–35% desde o pico após o sinal) deu +3,5% por trade contra −5,3% das regras atuais, sem deixar sinais de fora. A C (só tokens com 30 min ou mais) não passou (deixa 73% de fora e perdeu força).
- O laboratório mede agora também a D com pontos finos (`D com pontos finos (custos simulados)`).
- Com `entrada_queda_ativa` (ligado), um sinal aprovado é registado e o percurso começa logo, mas a proposta só é enviada quando o preço recuar 15–35% desde o pico; se recuar mais de 35%, espera que volte à zona. Desiste ao fim de 180 min ou se a liquidez cair abaixo do mínimo de rug. Recebes no Telegram "⏳ aprovado, à espera da queda". A confirmação manual (90 s), os 5 EUR e o resto das verificações são as mesmas. As saídas não mudam.
- A lista de espera fica em memória: num reinício, os sinais à espera perdem-se.

## 21/11/2026 — espera persistida, hipóteses de saída para a D, laboratório separado

- Os sinais à espera da queda ficam no `estado.json` (`espera_queda`) e continuam depois de um reinício.
- Dashboard: no painel "Posição aberta" aparece a caixa "⏳ À espera da queda" com cada token, o recuo atual desde o pico e os minutos que faltam.
- Laboratório: secção nova `HIPOTESES DE SAIDA PARA A ENTRADA D`, congelada a 21/11/2026 com os primeiros 129 sinais e medida só a partir do 130.º: E) stop −30% / alvo +25% / 120 min; F) stop −20% / alvo +50% / 120 min; referência = D com as saídas atuais. Só passam se ganharem com pontos normais E com pontos finos, com 30+ sinais novos.
- O laboratório saiu do `bot.py` para `laboratorio.py`, sem mudar nenhum cálculo (o relatório sai igual; os nomes continuam disponíveis em `bot.*`).

## Correção do reset (21/11/2026)

- O reset passou a arquivar também o diário de auditoria (`paper_audit.jsonl`) junto com o estado, os trades e o diário Paper. Antes ficava no sítio, e os eventos antigos de compra deixavam de bater certo com o diário Paper novo: o bot bloqueava as compras logo a seguir ao reset ("Reconciliação B13 falhou").
- `python bot.py --reset --confirmar-reset` já não arranca o bot depois do reset: no servidor é o serviço que o arranca.
