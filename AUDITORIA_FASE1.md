> **Relatorio historico da Fase 1.** Este documento regista uma auditoria anterior; nao e uma descricao autoritativa da implementacao atual. Consulte o codigo e a documentacao atual.
>
# Fase 1 — correções da auditoria técnica

## Salvaguardas

- Cópia integral do estado anterior às alterações: `backup_original_fase1_20261113/`.
- O bot principal e o modo Paper Trading foram preservados. Não foi adicionada execução de transações, ligação a carteiras de execução ou integração paga.
- Native SOL is deliberately excluded from swap decoding because balance deltas alone cannot separate swap value from transfers, rent, account lifecycle movements, and network fees. WSOL remains supported only with an explicit price.

## Alterações

- **E1 — FIFO por mint:** `SmartWalletTracker` mantém filas FIFO independentes para cada mint. Custos e P&L continuam agregados no relatório, sem cruzar inventários.
- **E2 - SOL/WSOL:** native SOL is not emitted from lamport deltas; WSOL continues to use token balance deltas and requires an explicit price. This avoids inferring a swap amount from ambiguous SOL transfers, rent, account creation/closure, or fees.
- **E3 — paginação:** o monitor continua com `before` até cobrir a janela delimitada pelo cursor anterior (ou o histórico inicial). Processa cronologicamente, ignora o próprio cursor se o RPC o incluir e só persiste um cursor depois de cobrir as páginas e obter todas as transações necessárias. Erros de transação declarados pelo RPC continuam a ser considerados processados e ignorados.
- **E5 - RugCheck:** while the filter is active, unavailable, malformed, out-of-range, or non-numeric responses are treated as unavailable; a candidate requiring the check is rejected temporarily and queued for re-evaluation.
- **E8 - timestamps:** only events with a present, finite transaction timestamp marked verified enter confirmed swap counts, token breadth, FIFO metrics, or wallet rankings. Existing rows migrated from older schemas are marked unverified and excluded until reimported with a timestamp.
- **E9 — fragmentos:** cada venda que fecha inventário conta uma operação, mesmo quando consome vários lotes. Os campos anteriores de relatório são mantidos; são adicionados aliases com nomes de operação e o ranking prefere estes aliases. A deduplicação de fluxo distingue assinatura, carteira e mint.
- **E10 — validade e falhas:** watchlists e proprietário/mint no decoder são validados como chaves públicas Solana base58 de 32 bytes. Transações com `meta.err` não produzem eventos.

## Testes adicionados ou atualizados

- `teste_regressoes_fase1.py`: regression coverage for E1, E2, E3, E5, E8, E9, and E10, including ambiguous SOL movements, malformed RugCheck payloads, and legacy ledger migration.
- `teste_solana_swap_decoder.py`, `teste_smart_money_pipeline.py` e `teste_wallet_monitor.py`: fixtures ajustadas para endereços públicos válidos.
- `teste_smart_money_flow.py`: caso de eventos distintos na mesma assinatura.
- `teste_offline.py`: o mercado simulado agora devolve relatório RugCheck seguro válido por omissão; as falhas permanecem cobertas pela regressão própria.

## Execução

Com Python 3 disponível, a verificação integral do projeto é:

```powershell
python -m py_compile *.py
python -m unittest discover -p "teste_*.py" -v
python teste_offline.py
```

Round 2 validation completed: all 24 project Python files compiled in memory with zero errors; unittest discovery ran 48 tests and all passed; `python teste_offline.py` passed 22/22 tests.

## Limitações conhecidas

- Native SOL is deliberately excluded from swap decoding because raw lamport balances cannot prove which movements represent a swap. WSOL remains supported from token balances with an explicit price.
- Native SOL is not decoded. WSOL USD conversion is not fetched or estimated; the caller must provide an explicit price.
- Swaps sem `blockTime` ficam armazenados com data desconhecida e são excluídos das métricas, exigindo reimportação/correção da fonte para que contem no histórico.
- Endereços são validados estruturalmente (base58 e 32 bytes); a validação não consulta a cadeia nem prova que a conta existe.
