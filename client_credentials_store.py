import base64
import hashlib
import json
import os
import socket
from pathlib import Path

from app_paths import obter_caminho_dados


CREDENTIALS_FILE_ENV = "FRS_CLIENT_CREDENTIALS_FILE"
SHARED_SECRET_ENV = "FRS_CLIENT_CREDENTIALS_SECRET"
DEFAULT_CREDENTIALS_FILE = "client_credentials.sec.json"


def resolve_credentials_path(path: str | None = None) -> Path:
    custom = str(path or "").strip() or str(os.getenv(CREDENTIALS_FILE_ENV, "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return Path(obter_caminho_dados(DEFAULT_CREDENTIALS_FILE)).resolve()


def _machine_key() -> bytes:
    shared_secret = str(os.getenv(SHARED_SECRET_ENV, "") or "").strip()
    if shared_secret:
        return hashlib.sha256(shared_secret.encode("utf-8")).digest()

    seed = f"{os.getenv('USERNAME', '')}|{socket.gethostname()}|FRS_MERCADO_CLIENT_SECRET_V1"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    out = bytearray(len(data))
    key_len = len(key)
    for i, value in enumerate(data):
        out[i] = value ^ key[i % key_len]
    return bytes(out)


def _encrypt_payload(payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cipher = _xor_bytes(raw, _machine_key())
    return {
        "version": 1,
        "scheme": "xor-sha256-v1",
        "payload": base64.b64encode(cipher).decode("ascii"),
    }


def _decrypt_payload(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Documento de credenciais inválido.")

    scheme = str(document.get("scheme") or "").strip().lower()
    if scheme == "plain":
        data = document.get("payload")
        if isinstance(data, dict):
            return data
        raise ValueError("Payload plain inválido.")

    if scheme != "xor-sha256-v1":
        raise ValueError(f"Scheme de credenciais não suportado: {scheme}")

    blob = str(document.get("payload") or "").strip()
    if not blob:
        raise ValueError("Payload de credenciais vazio.")

    encrypted = base64.b64decode(blob)
    raw = _xor_bytes(encrypted, _machine_key())
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Credenciais decodificadas inválidas.")
    return decoded


def load_client_credentials(path: str | None = None) -> dict:
    cred_path = resolve_credentials_path(path)
    if not cred_path.exists() or not cred_path.is_file():
        return {}

    content = json.loads(cred_path.read_text(encoding="utf-8"))
    payload = _decrypt_payload(content)
    return payload if isinstance(payload, dict) else {}


def save_client_credentials(data: dict, path: str | None = None) -> Path:
    if not isinstance(data, dict):
        raise ValueError("As credenciais devem ser um dicionário.")

    cred_path = resolve_credentials_path(path)
    cred_path.parent.mkdir(parents=True, exist_ok=True)

    encrypted_doc = _encrypt_payload(data)
    cred_path.write_text(json.dumps(encrypted_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        os.chmod(cred_path, 0o600)
    except Exception:
        pass

    return cred_path
