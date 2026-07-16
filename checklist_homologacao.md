# Checklist de Homologacao (5 minutos)

Use este checklist antes de entregar qualquer nova versao do executavel para o cliente.

## Checklist rapido (1 minuto)
- [ ] Executavel abre sem erro: `dist/FRS_Mercado.exe`
- [ ] PDV abre e encontra produto por codigo e por nome
- [ ] Estoque exibe badge e legenda corretamente (Azul = Interno, Verde = Real)
- [ ] Cadastro sem codigo gera codigo interno automaticamente
- [ ] Dados persistem apos fechar e reabrir o sistema
- [ ] Sem alerta critico de dependencia faltando no ultimo build

## 1. Pre-build e artefatos (30s)
- [ ] Executar build oficial: `python build_exe.py`
- [ ] Confirmar geracao do executavel: `dist/FRS_Mercado.exe`
- [ ] Confirmar geracao do pacote portatil: `installer/FRS_Mercado_Portable_1.0.8.zip`
- [ ] Confirmar geracao do instalador: `installer/FRS_Mercado_Setup.exe`

## 2. Validacao de dados e padronizacao numerica (45s)
- [ ] Testar campos numericos principais (PDV, financeiro, estoque, usuarios) com virgula e ponto
- [ ] Confirmar que entradas invalidas sao bloqueadas com mensagem amigavel
- [ ] Confirmar que valores sao formatados corretamente ao sair do campo
- [ ] Confirmar que nao houve regressao de calculos (preco, troco, taxas, etc.)

## 3. Verificacao visual (badge e legenda) (30s)
- [ ] Abrir tela de Estoque e conferir badge de tipo de codigo por produto
- [ ] Confirmar legenda visivel no topo: "Azul = Interno, Verde = Real"
- [ ] Confirmar que a legenda nao atrapalha a leitura da listagem

## 4. Fluxo PDV (2 min)
- [ ] Abrir PDV sem erro
- [ ] Buscar produto por codigo de barras (scanner/digitacao numerica)
- [ ] Buscar produto por nome (digitacao texto)
- [ ] Quando houver varios resultados por nome, selecionar item no modal/lista
- [ ] Adicionar item ao carrinho com quantidade valida
- [ ] Finalizar uma venda de teste (dinheiro) e validar total/troco

## 5. Cadastro de produtos e codigo interno (45s)
- [ ] Cadastrar produto sem codigo de barras e confirmar geracao automatica de codigo interno sequencial
- [ ] Confirmar que o codigo interno gerado nao conflita com codigos existentes
- [ ] Editar produto e confirmar persistencia de codigo, preco e estoque

## 6. Persistencia e estabilidade (45s)
- [ ] Fechar e reabrir o sistema
- [ ] Confirmar que produtos, configuracoes e movimentacoes permanecem salvos
- [ ] Confirmar que banco esta sendo utilizado no caminho de dados esperado
- [ ] Confirmar que nao houve crash/traceback no uso basico

## 7. Conexao e backup (Google Cloud/Drive) (45s)
- [ ] Em Configuracoes, validar reconhecimento de `credentials.json` e `google-services.json`
- [ ] Executar teste de conexao da integracao fiscal/backup
- [ ] Confirmar ausencia de erro de token ausente/corrompido no fluxo local

## 8. Dependencias e executavel limpo (30s)
- [ ] Revisar `build/FRS_Mercado/warn-FRS_Mercado.txt`
- [ ] Validar que nao ha modulo critico faltando para os fluxos usados (PDV, relatorios, Google, bcrypt, sqlite)
- [ ] Executar abertura completa do app + fluxo rapido de venda sem erro

## 9. Criterio final de liberacao
- [ ] Todos os itens acima marcados
- [ ] Sem erro bloqueante no PDV, persistencia ou integracoes
- [ ] Versao aprovada para entrega ao cliente

---

## Registro rapido da release
- Data:
- Versao:
- Build executado por:
- Resultado geral: Aprovado / Reprovado
- Observacoes:
