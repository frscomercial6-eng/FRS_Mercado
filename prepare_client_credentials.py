import argparse
import json
from pathlib import Path

from client_credentials_store import save_client_credentials


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera arquivo protegido de credenciais do cliente")
    parser.add_argument("--license-key", default="", help="Chave/licença do cliente")
    parser.add_argument("--client-key", default="", help="Chave interna do cliente")
    parser.add_argument("--firebase-admin-key-path", default="", help="Caminho do firebase-admin-key.json")
    parser.add_argument("--google-oauth-credentials-path", default="", help="Caminho do credentials.json")
    parser.add_argument("--google-services-path", default="", help="Caminho do google-services.json")
    parser.add_argument(
        "--from-json",
        default="",
        help="Arquivo JSON de entrada com os mesmos campos; sobrescreve argumentos individuais.",
    )
    parser.add_argument("--output", default="", help="Caminho de saída do arquivo protegido")
    args = parser.parse_args()

    payload = {
        "license_key": args.license_key,
        "client_key": args.client_key,
        "firebase_admin_key_path": args.firebase_admin_key_path,
        "google_oauth_credentials_path": args.google_oauth_credentials_path,
        "google_services_path": args.google_services_path,
    }

    if args.from_json:
        src = Path(args.from_json).expanduser().resolve()
        payload.update(json.loads(src.read_text(encoding="utf-8")))

    out_path = save_client_credentials(payload, path=(args.output or None))
    print(f"Arquivo protegido gerado em: {out_path}")


if __name__ == "__main__":
    main()
