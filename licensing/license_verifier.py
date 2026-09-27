"""Verificação fail-closed de licenças FRS Mercado assinadas por Ed25519."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .challenge import create_challenge
from .clock_guard import ClockGuard
from .errors import (
    ExpiredLicenseError,
    InvalidLicenseError,
    MachineMismatchError,
)
from .formats import LICENSE_SCHEMA, PRODUCT, parse_document, verify_envelope
from .keys import get_public_key
from .machine_identity import MachineIdentity, collect_machine_identity
from .revocation import RevocationStore
from .storage import LicenseStorage


@dataclass(frozen=True)
class LicenseValidation:
    valid: bool
    payload: dict
    checked_at: datetime


class LicenseVerifier:
    def __init__(self, storage: LicenseStorage | None = None):
        self.storage = storage or LicenseStorage()

    def verify(
        self,
        license_text: str,
        now: datetime | None = None,
        machine_identity: MachineIdentity | None = None,
        trusted_keys: dict[str, str] | None = None,
        clock_guard: ClockGuard | None = None,
    ) -> LicenseValidation:
        envelope = parse_document(license_text)
        key_id = str(envelope.get("key_id") or "")
        payload = verify_envelope(envelope, LICENSE_SCHEMA, get_public_key(key_id, trusted_keys))
        if str(payload.get("product") or "") != PRODUCT:
            raise InvalidLicenseError("Licença emitida para outro produto.")
        required = {
            "license_id", "product", "customer_id", "installation_id",
            "machine_binding", "edition", "features", "issued_at",
            "not_before", "expires_at", "issuer",
        }
        if not required.issubset(payload):
            raise InvalidLicenseError("Payload da licença incompleto.")

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            not_before = datetime.fromisoformat(str(payload["not_before"])).astimezone(timezone.utc)
            expires_at = datetime.fromisoformat(str(payload["expires_at"])).astimezone(timezone.utc)
        except Exception as exc:
            raise InvalidLicenseError("Validade da licença inválida.") from exc
        if current < not_before:
            raise ExpiredLicenseError("Licença ainda não está válida.")
        if current > expires_at:
            raise ExpiredLicenseError("Licença expirada.")

        expected_installation = str(payload.get("installation_id") or "")
        actual_installation = self.storage.get_or_create_installation_id()
        if expected_installation != actual_installation:
            raise MachineMismatchError("Licença pertence a outra instalação.")

        binding = payload.get("machine_binding")
        if not isinstance(binding, dict):
            raise MachineMismatchError("Vínculo de máquina ausente.")
        identity = machine_identity or collect_machine_identity()
        if str(binding.get("binding_algorithm") or "") != identity.binding_algorithm:
            raise MachineMismatchError("Algoritmo de vínculo incompatível.")
        expected_hashes = binding.get("component_hashes") or {}
        if not isinstance(expected_hashes, dict):
            raise MachineMismatchError("Componentes de máquina inválidos.")
        matches = sum(1 for name, value in expected_hashes.items() if identity.component_hashes.get(name) == value)
        minimum = max(1, int(binding.get("minimum_matches") or 1))
        if matches < minimum:
            raise MachineMismatchError("Licença não pertence a esta máquina.")

        if RevocationStore(self.storage).is_revoked(str(payload.get("license_id") or "")):
            from .errors import RevokedLicenseError
            raise RevokedLicenseError("Licença revogada.")

        checked_at = (clock_guard or ClockGuard(self.storage)).check_and_update(current)
        return LicenseValidation(True, dict(payload), checked_at)
