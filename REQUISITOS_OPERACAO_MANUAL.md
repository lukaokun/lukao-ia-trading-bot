# Requisitos para futura operação com dinheiro real — opção B

## Decisão do utilizador
- Capital nominal máximo inicial: **5 EUR por operação**.
- **Confirmação manual obrigatória** antes de cada compra.
- O bot pode pesquisar, analisar e preparar uma proposta; não pode decidir comprar sozinho.

## Comportamento obrigatório da proposta
Antes de pedir confirmação, a interface deve apresentar:
- token, mint e rede;
- preço observado e respetiva hora;
- montante nominal em EUR e conversão estimada para o ativo de cotação;
- taxas de rede e de plataforma estimadas;
- slippage estimado e impacto de preço, se disponível;
- quantidade estimada recebida;
- liquidez e riscos detetados;
- regras de saída/gestão de risco;
- validade temporal da cotação.

Se algum custo essencial, mínimo de ordem, cotação ou condição de segurança não puder ser verificado, a operação deve ser **bloqueada**, não presumida como executável.

## Confirmação
- A ação de aprovação deve ser explícita, específica e vinculada à proposta apresentada.
- Qualquer alteração material de preço, montante, mint, rota ou custos invalida a aprovação e exige nova confirmação.
- Rejeitar ou deixar expirar a proposta não envia qualquer transação.
- Impedir cliques repetidos, ordens duplicadas e reutilização de aprovações antigas.

## Limites e proteção
- Nunca exceder 5 EUR de capital nominal numa entrada, até o utilizador alterar expressamente o limite.
- O valor de 5 EUR não é uma perda máxima garantida: o preço pode cair, a liquidez pode desaparecer e a execução pode diferir.
- Definir limites separados para exposição total, perda diária e drawdown antes de qualquer operação real.
- Ter paragem de emergência que impeça novas ordens e permita gerir posições abertas de forma segura.
- Não armazenar seed phrase nem chave privada em ficheiros do projeto, logs ou interface.
- Usar carteira dedicada e permissões mínimas numa futura integração; nunca pedir a seed phrase ao utilizador.

## Estado atual do projeto
**Paper trading apenas.** `config_operacoes.json` mantém `mode=paper` e `live_trading_enabled=false`.
Os módulos Smart Money e monitorização de carteiras são ferramentas separadas e ainda não estão integrados no decisor principal do bot. A aprovação manual e a execução real descritas acima são requisitos para desenvolvimento futuro, não funcionalidades já implementadas.

## Critérios antes de considerar a fase real
1. Integrar os sinais e filtros no motor de análise sem contornar a gestão de risco.
2. Implementar proposta de ordem e confirmação manual vinculada aos parâmetros exatos.
3. Implementar verificação de saldo, mínimo de ordem, taxas, slippage e validade da cotação.
4. Implementar proteção contra duplicados, expiração e alterações de cotação.
5. Implementar limites de exposição/perda e paragem de emergência.
6. Testar com simulações, testes de integração e ambiente de teste apropriado.
7. Fazer auditoria de segurança e revisão do fluxo de assinatura.
8. Só depois considerar uma ativação gradual, mantendo confirmação manual.

Nenhum backtest ou teste técnico garante rentabilidade. A aprovação deve depender de resultados líquidos de custos, amostras suficientes e testes fora da amostra.
