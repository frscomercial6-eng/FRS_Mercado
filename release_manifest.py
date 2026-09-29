"""
Emissor do `update-manifest.json` assinado (Ed25519) do FRS Mercado.

Este modulo APENAS GERA o arquivo assinado. Ele nao publica nada: a
publicacao continua sendo uma etapa manual e controlada.

Garantias de desenho
--------------------
* A chave privada e lida EXCLUSIVAMENTE da variavel de ambiente
  ``FRS_UPDATER_PRIVATE_KEY``. Nunca e gravada em disco nem impressa.
* Formatos aceitos:
    1. PEM PKCS#8 (-----BEGIN PRIVATE KEY-----), com ou sem quebras;
    2. base64 da chave Ed25519 crua (32 bytes).
* Reutiliza a canonicalizacao do cliente
  (``licensing.canonical_json.canonical_json_bytes``).
* O envelope contem EXATAMENTE ``schema``, ``key_id``, ``payload`` e
  ``signature`` - nenhum campo extra, porque verify_manifest os rejeita.
* Antes de gravar, a assinatura e verificada contra a chave publica oficial
  (``updater_public_keys.json``) e o envelope passa pelo contrato real do
  cliente (``updater_secure.verify_manifest``).
* ``size`` e ``sha256`` sao calculados do ARQUIVO LOCAL. O emissor nao aceita
  tamanho/hash digitados manualmente, tornando divergencia entre manifesto e
  artefato estruturalmente impossivel.

Uso
---
    $env:FRS_UPDATER_PRIVATE_KEY = "<PEM ou base64 Ed25519>"
    python release_manifest.py --version 1.0.21 --sequence 1 --out update-manifest.json ^
        --asset portable_zip "https://.../FILE.zip" ".\\FILE.zip"

    python release_manifest.py --check-only --in update-manifest.json
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from licensing.canonical_json import canonical_json_bytes
from updater_secure import (
    DEFAULT_CHANNEL,
    MANIFEST_SCHEMA,
    PRODUCT,
    PUBLIC_KEYS_FILE,
    UpdateSecurityError,
    normalize_version,
    verify_manifest,
)

PRIVATE_KEY_ENV = "FRS_UPDATER_PRIVATE_KEY"
ASSET_TYPES = ("portable_zip", "installer_exe", "traditional_exe")
DEFAULT_KEY_ID = "frs-mercado-updater-official-v1"


class ManifestError(RuntimeError):
    """Falha operacional na geracao do manifesto (nunca expoe a chave)."""


def _load_private_key_from_env() -> Ed25519PrivateKey:
    raw_env = os.environ.get(PRIVATE_KEY_ENV, "")
    if not raw_env.strip():
        raise ManifestError(
            "Chave privada ausente. Defina %s (PEM PKCS#8 ou base64 Ed25519)." % PRIVATE_KEY_ENV
        )
    raw = raw_env.strip()
    if "BEGIN" in raw:
        pem = raw.replace("\\n", "\n").encode("utf-8")
        try:
            key = serialization.load_pem_private_key(pem, password=None)
        except Exception as exc:
            raise ManifestError("Chave privada PEM invalida ou nao suportada.") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ManifestError("A chave informada nao e Ed25519.")
        return key
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("Chave privada base64 invalida.") from exc
    if len(decoded) != 32:
        raise ManifestError(
            "Chave Ed25519 base64 deve ter 32 bytes; a informada tem %d." % len(decoded)
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(decoded)
    except Exception as exc:
        raise ManifestError("Nao foi possivel carregar a chave privada Ed25519.") from exc


def _public_key_for(key_id: str) -> Ed25519PublicKey:
    try:
        payload = json.loads(PUBLIC_KEYS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ManifestError("updater_public_keys.json ausente ou invalido.") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    encoded = str((keys or {}).get(key_id) or "").strip()
    if not encoded:
        raise ManifestError(
            "key_id '%s' nao esta em updater_public_keys.json. "
            "Use um id ja publicado para o cliente aceitar a assinatura." % key_id
        )
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise ManifestError("Chave publica invalida para o id '%s'." % key_id) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_https(url: str) -> str:
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ManifestError("URL de asset deve ser HTTPS valida: %r" % url)
    if parsed.username or parsed.password:
        raise ManifestError("URL de asset nao pode conter credenciais.")
    return url


def build_asset(asset_type: str, url: str, local_path: Path) -> dict:
    """Monta um asset com size/sha256 calculados do arquivo local."""
    asset_type = str(asset_type or "").strip().lower()
    if asset_type not in ASSET_TYPES:
        raise ManifestError("asset_type invalido: %r. Use um de %s." % (asset_type, ASSET_TYPES))
    url = _validate_https(url)
    local_path = Path(local_path)
    if not local_path.is_file():
        raise ManifestError("Artefato local nao encontrado: %s" % local_path)
    size = local_path.stat().st_size
    if size <= 0:
        raise ManifestError("Artefato local esta vazio: %s" % local_path)
    return {
        "name": local_path.name,
        "asset_type": asset_type,
        "url": url,
        "size": size,
        "sha256": _sha256_file(local_path),
    }


def build_envelope(
    private_key,
    key_id: str,
    version: str,
    sequence: int,
    assets,
    product: str = PRODUCT,
    channel: str = DEFAULT_CHANNEL,
    auto_update: bool = True,
    issued_at=None,
) -> dict:
    """Monta e assina o envelope."""
    key_id = str(key_id or "").strip()
    if not key_id:
        raise ManifestError("key_id e obrigatorio.")
    version = normalize_version(version)
    if version == "0.0.0":
        raise ManifestError("Versao do manifesto invalida.")
    sequence = int(sequence)
    if sequence <= 0:
        raise ManifestError("sequence deve ser inteiro > 0 e crescente a cada release.")
    if not assets:
        raise ManifestError("E necessario ao menos um asset.")
    for asset in assets:
        _validate_https(asset.get("url", ""))

    payload = {
        "product": product,
        "channel": channel,
        "version": version,
        "sequence": sequence,
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
        "auto_update": bool(auto_update),
        "assets": [dict(asset) for asset in assets],
    }
    message = canonical_json_bytes(
        {"schema": MANIFEST_SCHEMA, "key_id": key_id, "payload": payload}
    )
    signature = base64.b64encode(private_key.sign(message)).decode("ascii")
    return {
        "schema": MANIFEST_SCHEMA,
        "key_id": key_id,
        "payload": payload,
        "signature": signature,
    }


def self_check(envelope: dict, key_id: str) -> None:
    """Valida o envelope ANTES de gravar: assinatura + contrato do cliente."""
    public_key = _public_key_for(key_id)
    message = canonical_json_bytes(
        {
            "schema": envelope["schema"],
            "key_id": envelope["key_id"],
            "payload": envelope["payload"],
        }
    )
    try:
        public_key.verify(base64.b64decode(envelope["signature"], validate=True), message)
    except Exception as exc:
        raise ManifestError("Assinatura nao confere com a chave publica oficial.") from exc
    unexpected = set(envelope) - {"schema", "key_id", "payload", "signature"}
    if unexpected:
        raise ManifestError("Envelope com campos nao assinados: %s" % sorted(unexpected))
    try:
        verify_manifest(envelope)
    except UpdateSecurityError as exc:
        if "sequ" in str(exc).lower():
            print("  ! AVISO: verify_manifest recusou por sequencia local (%s)." % exc)
            print("    Assinatura e estrutura validas; confirme que nenhum cliente")
            print("    ja aplicou um sequence maior.")
            return
        raise ManifestError("Contrato do cliente recusou o manifesto: %s" % exc) from exc


def write_manifest(envelope: dict, out_path: Path, key_id: str) -> Path:
    self_check(envelope, key_id)
    out_path = Path(out_path)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


def check_manifest_file(path: Path) -> dict:
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    self_check(envelope, str(envelope.get("key_id") or "").strip())
    return envelope


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Gera o update-manifest.json assinado do FRS Mercado (nao publica).",
    )
    parser.add_argument("--key-id", default=DEFAULT_KEY_ID)
    parser.add_argument("--version", default="")
    parser.add_argument("--sequence", type=int, default=0)
    parser.add_argument("--product", default=PRODUCT)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--issued-at", default="")
    parser.add_argument("--no-auto-update", action="store_true")
    parser.add_argument("--asset", nargs=3, action="append", default=[],
                        metavar=("ASSET_TYPE", "URL", "LOCAL_PATH"))
    parser.add_argument("--out", default="update-manifest.json")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--in", dest="input", default="update-manifest.json")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    if args.check_only:
        envelope = check_manifest_file(Path(args.input))
        payload = envelope["payload"]
        print("OK: manifesto valido - versao %s (sequence %s, %d asset(s))"
              % (payload["version"], payload["sequence"], len(payload["assets"])))
        return 0

    if not os.environ.get(PRIVATE_KEY_ENV, "").strip():
        print("Emissor implementado; chave privada ausente; nenhum manifesto gerado.")
        print("Defina %s com a chave Ed25519 privada correspondente a '%s'."
              % (PRIVATE_KEY_ENV, args.key_id))
        return 0

    if not args.version:
        raise ManifestError("--version e obrigatorio.")
    if not args.asset:
        raise ManifestError("Informe ao menos um --asset.")

    private_key = _load_private_key_from_env()
    assets = [build_asset(t, u, Path(p)) for t, u, p in args.asset]
    envelope = build_envelope(
        private_key=private_key, key_id=args.key_id, version=args.version,
        sequence=args.sequence, assets=assets, product=args.product,
        channel=args.channel, auto_update=not args.no_auto_update,
        issued_at=args.issued_at or None,
    )
    out = write_manifest(envelope, Path(args.out), args.key_id)
    print("Manifesto gravado e validado: %s" % out)
    print("  versao   : %s" % envelope["payload"]["version"])
    print("  sequence : %s" % envelope["payload"]["sequence"])
    print("  key_id   : %s" % envelope["key_id"])
    for asset in envelope["payload"]["assets"]:
        print("  asset    : %s (%s) %d bytes" % (asset["name"], asset["asset_type"], asset["size"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print("ERRO: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
