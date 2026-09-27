"""Verificador de revogação local; nenhuma consulta de rede é realizada."""

from __future__ import annotations

from .errors import RevokedLicenseError
from .formats import REVOCATION_SCHEMA, parse_document, verify_envelope
from .keys import get_public_key
from .storage import LicenseStorage


class RevocationStore:
    def __init__(self, storage: LicenseStorage | None = None):
        self.storage = storage or LicenseStorage()

    def is_revoked(self, license_id: str, envelope_text: str | None = None) -> bool:
        if not envelope_text:
            try:
                envelope_text = self.storage.read_json(self.storage.root / "revocations.json").get("envelope")
            except Exception:
                return False
            if isinstance(envelope_text, dict):
                envelope_text = envelope_text.get("text")
        if not envelope_text:
            return False
        try:
            envelope = parse_document(str(envelope_text))
            key_id = str(envelope.get("key_id") or "")
            payload = verify_envelope(envelope, REVOCATION_SCHEMA, get_public_key(key_id))
            return str(license_id) in set(payload.get("revoked_license_ids") or [])
        except RevokedLicenseError:
            raise
        except Exception as exc:
            # Uma revogação local corrompida não pode ser silenciosamente ignorada.
            from .errors import StorageError
            raise StorageError("Lista de revogação local inválida.") from exc
