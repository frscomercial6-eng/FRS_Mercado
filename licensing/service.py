"""Serviço único de ativação/consulta da licença assinada."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .challenge import create_challenge
from .errors import LicenseError
from .license_verifier import LicenseVerifier
from .storage import LicenseStorage


@dataclass(frozen=True)
class LicenseStatus:
    valid: bool
    legacy: bool
    message: str
    payload: dict | None = None


class LicenseService:
    """Fonte de verdade para licenças novas; sem fallback automático para Trial."""

    def __init__(self, storage: LicenseStorage | None = None):
        self.storage = storage or LicenseStorage()
        self.verifier = LicenseVerifier(self.storage)

    def create_challenge(self) -> dict:
        return create_challenge(self.storage)

    def activate(self, license_text: str) -> LicenseStatus:
        # Valida antes de persistir; arquivo inválido nunca substitui o válido.
        validation = self.verifier.verify(license_text)
        self.storage.save_license(license_text)
        return LicenseStatus(True, False, "Licença assinada ativada com sucesso.", validation.payload)

    def get_status(self) -> LicenseStatus:
        try:
            text = self.storage.load_license()
        except LicenseError as exc:
            return LicenseStatus(False, False, str(exc))
        if not text:
            return LicenseStatus(False, False, "Nenhuma licença assinada foi ativada.")
        try:
            validation = self.verifier.verify(text)
            payload = validation.payload
            expires = datetime.fromisoformat(str(payload["expires_at"]))
            days = max(0, (expires - datetime.now(timezone.utc)).days)
            message = f"Licença assinada ativa ({days} dia(s))"
            if days <= 7:
                message = f"Licença assinada vence em {days} dia(s)"
            return LicenseStatus(True, False, message, payload)
        except LicenseError as exc:
            # Erro criptográfico, clock ou incompatibilidade é fail-closed.
            return LicenseStatus(False, False, str(exc))
