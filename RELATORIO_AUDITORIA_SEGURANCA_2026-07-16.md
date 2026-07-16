# Relatorio Pronto para Auditoria - Incidente de Credenciais

Data: 2026-07-16  
Projeto: FRS_Mercado

## Escopo
- Revisao de exposicao de credenciais (Google Cloud/Firebase).
- Verificacao de higienizacao de historico Git.
- Validacao de blindagem para impedir novo vazamento.
- Confirmacao operacional da conexao Firebase apos mitigacao.

## Achados, Acoes, Evidencias e Status

| ID | Achado | Acao Executada | Evidencia | Status Final |
|---|---|---|---|---|
| A-01 | Arquivo de chave antiga presente historicamente no Git | Historico reescrito com `git-filter-repo` em clone espelho e publicado via push forcado | Execucao concluida com repack completo; verificacoes de padrao antigo sem ocorrencias no espelho limpo | Concluido |
| A-02 | Arquivos com credenciais no workspace (`mobile_app/assets/firebase-admin-key.json`, `_build_support/credentials.json`, `_build_support/google-services.json`) | Arquivos removidos do projeto local | Remocoes aplicadas e validadas em varredura subsequente | Concluido |
| A-03 | Chave ativa em arquivo JSON na raiz do repositorio | Chave movida para `%APPDATA%/FRS_Mercado/data/firebase-admin-key.json`; projeto reconfigurado para ler via cofre local protegido | Caminho persistido no `client_credentials.sec.json` em APPDATA; sem chave no workspace | Concluido |
| A-04 | Risco de reenvio de JSON/credenciais ao GitHub | Endurecimento de `.gitignore` para bloquear `*.json`, `.env*`, artefatos de build e padroes de chaves | `git check-ignore -v` confirma bloqueio de arquivos sensiveis | Concluido |
| A-05 | Risco de quebra de integracao apos remocao da chave local | Ajuste de resolucao de credencial no `firebase_manager.py` para ler tambem do cofre local protegido | `firebase_ok=True` e mensagem de conexao OK no teste de runtime | Concluido |

## Evidencias Tecnicas (Trechos Objetivos)
- Blindagem por ignore confirmada:
  - `.gitignore:54:*firebase-admin-key*.json mobile_app/assets/firebase-admin-key.json`
  - `.gitignore:50:credentials.json _build_support/credentials.json`
  - `.gitignore:51:google-services.json _build_support/google-services.json`
  - `.gitignore:46:*.json frsmercado-f817f-7b290b1a675a.json`
- Integracao Firebase validada:
  - `firebase_ok=True`
  - `Conexao Firebase OK (colecao 'teste' acessada).`

## Risco Residual
- Qualquer clone antigo local de colaboradores pode reintroduzir artefatos se houver push indevido.
- Forks externos podem manter historico anterior caso nao sejam tratados separadamente.

## Recomendacoes de Continuidade
1. Exigir novo clone limpo para todos os colaboradores ativos.
2. Manter varredura de segredos em pre-commit e CI.
3. Rotacionar periodicamente credenciais sensiveis (ja iniciado no incidente atual).

## 3 Tarefas Pendentes Processadas Nesta Rodada
1. Rodar varredura final de segredos apenas nos arquivos modificados.
2. Preparar staging seguro sem incluir `.json`/credenciais.
3. Criar commit final com evidencias e hardening de seguranca sem arquivos sensiveis.
