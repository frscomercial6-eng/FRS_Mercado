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

