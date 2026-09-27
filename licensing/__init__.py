"""Licenciamento fail-closed do FRS Mercado.

O cliente possui somente chaves públicas de verificação. Nenhuma chave
privada de emissão deve existir ou ser importada por este pacote.
"""

from .errors import (
    ClockRollbackError,
    ExpiredLicenseError,
    KeyConfigurationError,
    LicenseError,
    MachineMismatchError,
    RevokedLicenseError,
)
from .license_verifier import LicenseValidation, LicenseVerifier
from .service import LicenseService, LicenseStatus

__all__ = [
    "ClockRollbackError",
    "ExpiredLicenseError",
    "KeyConfigurationError",
    "LicenseError",
    "LicenseService",
    "LicenseStatus",
    "LicenseValidation",
    "LicenseVerifier",
    "MachineMismatchError",
    "RevokedLicenseError",
]
