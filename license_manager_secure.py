"""Status e ativação de licença do FRS Mercado.

A licença assinada em `licensing/` é a fonte de verdade. O banco legado
somente é consultado para exibir umasituação de migração; nunca cria Trial,
chave ou autorização.
"""

from __future__ import annotations

from datetime import datetime, timezone

from database_manager import get_db_connection
from licensing import LicenseService
from licensing.storage import LicenseStorage

RENOVACAO_URL = "https://www.frssolutions.com.br/planos"


def _status_active(message: str, days: int | None, source: str = "signed") -> dict:
    warning = days is not None and days <= 7
    return {
        "code": "active",
        "message": message,
        "days_left": days,
        "is_expired": False,
        "is_warning": warning,
        "color": "#f1c40f" if warning else "#2ecc71",
        "renewal_url": RENOVACAO_URL,
        "source": source,
    }


def _legacy_read_only_status() -> dict:
    """Exibe data legada sem transformá-la em licença verificável."""
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT data_expiracao FROM licenca ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception as exc:
        return {
            "code": "error",
            "message": f"Licença: erro ao consultar compatibilidade legada ({exc})",
            "days_left": None,
            "is_expired": True,
            "is_warning": True,
            "color": "#ff5555",
            "renewal_url": RENOVACAO_URL,
            "source": "legacy",
        }

    if not row or not row[0]:
        return {
            "code": "missing",
            "message": "Nenhuma licença assinada foi ativada.",
            "days_left": None,
            "is_expired": True,
            "is_warning": False,
            "color": "#ff5555",
            "renewal_url": RENOVACAO_URL,
            "source": "missing",
        }

    try:
        raw = str(row[0]).strip().replace("Z", "+00:00")
        expires = datetime.fromisoformat(raw)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = (expires - now).days
    except Exception:
        return {
            "code": "invalid_legacy",
            "message": "Registro legado possui data inválida; importe uma licença assinada.",
            "days_left": None,
            "is_expired": True,
            "is_warning": False,
            "color": "#ff5555",
            "renewal_url": RENOVACAO_URL,
            "source": "legacy",
        }

    expired = expires <= datetime.now(timezone.utc)
    warning = not expired and days <= 7
    return {
        "code": "legacy_expired" if expired else "legacy_unverified",
        "message": (
            f"Compatibilidade legada { 'expirada' if expired else 'encontrada' }: {expires.date()}. "
            "Importe uma licença assinada para usar o modo seguro."
        ),
        "days_left": days,
        "is_expired": True,
        "is_warning": False,
        "color": "#ff5555" if expired else "#f1c40f",
        "renewal_url": RENOVACAO_URL,
        "source": "legacy",
    }


class LicenseManager:
    """Facade local para o verificador fail-closed de licenças assinadas."""

    def __init__(self, storage: LicenseStorage | None = None):
        self.storage = storage or LicenseStorage()
        self.service = LicenseService(self.storage)

    def get_status(self) -> dict:
        status = self.service.get_status()
        if status.valid:
            days = None
            if status.payload:
                try:
                    expires = datetime.fromisoformat(str(status.payload["expires_at"]))
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    days = max(0, (expires - datetime.now(timezone.utc)).days)
                except Exception:
                    days = None
            return _status_active(status.message, days, "signed")

        # Arquivo assinado inválido/corrompido é fail-closed; não há fallback.
        if self.storage.license_path.is_file():
            return {
                "code": "invalid_signed_license",
                "message": f"Licença assinada inválida: {status.message}",
                "days_left": None,
                "is_expired": True,
                "is_warning": False,
                "color": "#ff5555",
                "renewal_url": RENOVACAO_URL,
                "source": "signed",
            }
        return _legacy_read_only_status()

    def create_challenge(self) -> dict:
        return self.service.create_challenge()

    def activate(self, license_text: str):
        return self.service.activate(license_text)
