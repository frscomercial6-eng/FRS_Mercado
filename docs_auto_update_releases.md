# Estrutura recomendada de Releases para auto-update

O auto-update consulta `version.json` na raiz do branch principal do repositório.

## Arquivos mínimos no repositório

- `version.txt`: versão local atual (ex.: `1.0.5`)
- `version.json`: manifesto remoto consumido pelo app
- `release_info.py`: metadados de versão usados no runtime

## Formato do version.json

```json
{
  "latest_version": "1.0.5",
  "download_url": "https://api.github.com/repos/OWNER/REPO/releases/assets/ID_DO_ASSET"
}
```

## Fluxo de release no GitHub

1. Criar tag da versão (`v1.0.5`).
2. Publicar Release com os binários:
   - instalador desktop (`FRS_Mercado_Setup_1.0.5.exe`) ou bundle Flet (`FRS_Mercado_FletBundle_1.0.5.exe`)
   - opcional zip portátil (`FRS_Mercado_Portable_1.0.5.zip` ou `FRS_Mercado_FletPortable_1.0.5.zip`)
3. Copiar o ID do asset principal do instalador.
4. Atualizar `version.json` apontando `latest_version` e `download_url` do asset.
5. Commitar `version.json` no branch principal.

## Organização sugerida no Release

- Nome da Release: `FRS Mercado 1.0.5`
- Tag: `v1.0.5`
- Assets:
  - `FRS_Mercado_Setup_1.0.5.exe` (principal para auto-update)
  - `FRS_Mercado_Portable_1.0.5.zip` (opcional)
  - `FRS_Mercado_FletBundle_1.0.5.exe` (quando build Flet for o canal principal)

## Observações

- O auto-update compara apenas versões semânticas no formato `X.Y.Z`.
- O app baixa o asset definido em `download_url` e inicia a instalação automaticamente.
- Se o repositório for privado, configure `GITHUB_TOKEN` ou `GH_TOKEN` no ambiente do cliente para permitir download via API.
