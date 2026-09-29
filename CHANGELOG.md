# Changelog

## 1.0.0 - 23/06/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.3 - 23/06/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.4 - 05/07/2026
- Correção crítica de integração entre PDV e BI: vendas agora persistem em `vendas` e `itens_venda`, com fallback legado no dashboard/relatórios.
- Restauração do fluxo obrigatório de abertura de caixa diária (com fechamento automático de caixa antigo ainda aberto).
- Inclusão dos novos módulos de cadastro de Clientes e Fornecedores no menu principal.
- Implementado vínculo Fornecedor x Produto (`fornecedor_produtos`) com atualização de `entradas.fornecedor_id` quando aplicável.
- Cadastro de produtos atualizado com campo manual de Código NCM (`produtos.ncm`).
- Migrações SQLite adicionadas para tabelas `clientes`, `fornecedores`, `fornecedor_produtos` e colunas novas (`produtos.ncm`, `entradas.fornecedor_id`).
- Motor de vendas com função `calcular_impostos_liquidos(valor_venda, ncm)` e retenção automática por NCM no fechamento da venda.
- Configuração de alíquotas tributárias parametrizada em `config_aliquotas_ncm` (sem necessidade de alterar código para atualizar legislação).
- Fluxo de Caixa e Dashboard atualizados para exibir valores de `Bruto`, `Impostos Retidos` e `Líquido`.
- Esboço de relatório fiscal para SPED adicionado no módulo de relatórios (`Esboço SPED (CSV)`).
- Novo módulo de Orçamentos (Propostas Comerciais) com vínculo obrigatório a cliente, status e persistência separada de vendas.
- PDV com ações de `Salvar Orçamento` e `Abrir Orçamentos`, sem impacto em dashboard/caixa até conversão.
- Conversão de orçamento em venda com aplicação de impostos por NCM, baixa de estoque e integração com fluxo fiscal.
- Exportação de orçamento em PDF com itens, quantidades, valores, NCM e total.
- Delivery via webhook: reconhecimento de `pagamento_aprovado` com processamento automático de venda (status `APROVADO`/`PAGO`) e baixa imediata de estoque.
- Vendas passam a registrar `origem`, `status_pedido` e `status_pagamento` para rastreio por canal (Loja Física, iFood, App Próprio).
- Dashboard atualizado com visão de origem de lucro por canal no dia.
- Menu principal com ação `Verificar Atualizações` para consulta manual imediata ao GitHub e feedback quando já está na versão mais recente.

## 1.0.4 - 16/07/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.5 - 16/07/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.6 - 16/07/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.7 - 16/07/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.8 - 16/07/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.9 - 16/07/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.10 - 10/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.11 - 10/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.11 - 12/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.11 - 13/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.11 - 25/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.11 - 26/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.12 - 26/08/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.12 - 05/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.12 - 06/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.12 - 12/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.12 - 13/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.12 - 17/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.13 - 18/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.14 - 18/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.14 - 19/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.15 - 19/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.15 - 22/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.16 - 22/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.16 - 23/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.17 - 23/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.18 - 23/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.19 - 24/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.19 - 25/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.20 - 26/09/2026
- Release automatizada gerada pelo Mestre de Release.
- PDV: removido o botão visual "MÚLTIPLO PAGTO (F8)" do painel lateral de operações.
- PDV: o atalho F8 foi liberado e passou a acionar a função existente de SALVAR VALE (`salvar_vale_atual`).
- PDV: o item de menu "SALVAR VALE (F8)" foi rotulado com o novo atalho; o comando permanece inalterado.
- PDV: o método `abrir_modal_pagamento_multiplo()` e todo o fluxo interno de múltiplos pagamentos (`pagamentos_parciais`, `valor_pago_acumulado`, divisão, restante/troco e registro "MISTO") permanecem intactos e inalterados — apenas o componente de interface foi retirado.
- PDV: demais atalhos (F1–F7, F9, F10, F11, F12) preservados sem qualquer alteração.

## 1.0.20 - 27/09/2026
- Release automatizada gerada pelo Mestre de Release.

## 1.0.21 - 28/09/2026
- Cadastro de produtos: correção do cálculo e do recálculo de preço de venda e de margem, permitindo informar o preço de venda manualmente sem bloqueio e sem recálculo forçado pelo custo ou pela margem.
- PDV: correção da identificação de produto por EAN, código de barras ou nome, inclusive nos casos em que o código digitado era confundido com a quantidade.
- PDV: melhoria da navegação e do foco — campo de QTD ocultado visualmente, mantido funcional internamente, removido do TAB e ciclo principal Campo de Produto → Valor Informado → Campo de Produto.
- Atualização: preservação dos dados operacionais existentes durante o processo de atualização.

