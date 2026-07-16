# Build do APK (WebView)

Este módulo Android gera um APK container (WebView) que renderiza a interface web da Oficina.

## Pré-requisitos

- JDK 17
- Android SDK (API 34)
- Gradle 8.x (ou `gradlew` quando disponível)

## Variáveis de ambiente

Defina as variáveis do arquivo `.env.example` no ambiente antes do build.

No PowerShell:

```powershell
$env:OFP_WEB_APP_URL = "https://seu-endereco/app"
$env:OFP_DRIVE_TOKEN_FOLDER_ID = "opcional-id-da-pasta-de-token"
$env:OFP_TOKEN_SECRET = "segredo-igual-ao-usado-no-desktop"
$env:OFP_TOKEN_FILE_NAME = "acesso.token"
$env:OFP_FIREBASE_API_KEY = "..."
$env:OFP_FIREBASE_AUTH_DOMAIN = "..."
$env:OFP_FIREBASE_DATABASE_URL = "https://oficinapescasystem-default-rtdb.firebaseio.com/"
$env:OFP_FIREBASE_PROJECT_ID = "..."
$env:OFP_FIREBASE_STORAGE_BUCKET = "..."
$env:OFP_FIREBASE_MESSAGING_SENDER_ID = "..."
$env:OFP_FIREBASE_APP_ID = "..."
$env:OFP_FIREBASE_SYNC_CHANNEL = "global"
```

## Build

```powershell
Set-Location android_apk
gradle assembleDebug
```

Saída esperada:

- `android_apk/app/build/outputs/apk/debug/app-debug.apk`

## Validações incluídas

- Permissão de internet e estado de rede no Manifest.
- `Dark Mode` habilitado por padrão no container.
- Injeção de configuração Firebase no runtime do WebView.
- Canal de sincronização `sync_nodes/<canal>` compartilhado com Desktop.
