# Credenciais protegidas compartilhadas (Desktop e APK)

O projeto agora suporta um arquivo protegido local com credenciais do cliente:

- Caminho padrão: `%APPDATA%/FRS_Mercado/data/client_credentials.sec.json`
- Override por ambiente: `FRS_CLIENT_CREDENTIALS_FILE`

## Campos suportados

```json
{
  "license_key": "LICENCA_DO_CLIENTE",
  "client_key": "CHAVE_INTERNA_CLIENTE",
  "firebase_admin_key_path": "C:/seguranca/firebase-admin-key.json",
  "google_oauth_credentials_path": "C:/seguranca/credentials.json",
  "google_services_path": "C:/seguranca/google-services.json"
}
```

## Como gerar o arquivo protegido

```bash
python prepare_client_credentials.py --from-json caminho/credenciais_cliente.json
```

Ou informando parâmetros diretos:

```bash
python prepare_client_credentials.py \
  --license-key "LIC-123" \
  --client-key "CLI-ABC" \
  --firebase-admin-key-path "C:/seguranca/firebase-admin-key.json"
```

## Compartilhar entre Desktop e APK

Para que os dois aplicativos leiam o mesmo arquivo protegido em ambientes diferentes, use a mesma senha via variável de ambiente:

- `FRS_CLIENT_CREDENTIALS_SECRET`

Sem essa variável, a proteção usa derivação local da máquina (funciona para proteção local, mas não é ideal para compartilhar entre dispositivos diferentes).
