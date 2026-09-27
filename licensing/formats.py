"""Formato canônico de envelopes assinados do FRS Mercado."""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical_json import canonical_json_bytes
from .errors import InvalidLicenseError

LICENSE_SCHEMA = "FRS-MERCADO-LICENSE"
CHALLENGE_SCHEMA = "FRS-MERCADO-CHALLENGE"
REVOCATION_SCHEMA = "FRS-MERCADO-REVOCATIONS"
PRODUCT = "FRS-Mercado"


def parse_document(text_or_bytes: str | bytes) -> dict[str, Any]:
    try:
        raw = text_or_bytes.decode("utf-8") if isinstance(text_or_bytes, bytes) else text_or_bytes
        payload = json.loads(str(raw).strip())
    except Exception as exc:
        raise InvalidLicenseError("Documento de licença não é um JSON válido.") from exc
    if not isinstance(payload, dict):
        raise InvalidLicenseError("Documento de licença deve ser um objeto JSON.")
    return payload


def signature_message(schema: str, key_id: str, payload: dict[str, Any]) -> bytes:
    """Mensagem exatamente assinada; `signature` nunca entra no material."""
    return canonical_json_bytes({"schema": schema, "key_id": key_id, "payload": payload})


def verify_envelope(
    envelope: dict[str, Any],
    expected_schema: str,
    public_key: Ed25519PublicKey,
) -> dict[str, Any]:
    schema = str(envelope.get("schema") or "").strip()
    key_id = str(envelope.get("key_id") or "").strip()
    payload = envelope.get("payload")
    signature = str(envelope.get("signature") or "").strip()
    if schema != expected_schema:
        raise InvalidLicenseError(f"Schema inválido: {schema or 'ausente'}.")
    if not key_id or not isinstance(payload, dict) or not signature:
        raise InvalidLicenseError("Envelope de licença incompleto.")
    try:
        raw_signature = base64.b64decode(signature, validate=True)
    except Exception as exc:
        raise InvalidLicenseError("Assinatura não está em Base64 válido.") from exc
    try:
        public_key.verify(raw_signature, signature_message(schema, key_id, payload))
    except InvalidSignature as exc:
        raise InvalidLicenseError("Assinatura da licença inválida.") from exc
    except Exception as exc:
        raise InvalidLicenseError("Não foi possível verificar a assinatura.") from exc
    return dict(payload)


def encode_envelope(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
