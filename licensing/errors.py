class LicenseError(Exception):
    """Base fail-closed para falhas de licenciamento."""


class KeyConfigurationError(LicenseError):
    """Chave pública confiável ausente ou inválida."""


class InvalidLicenseError(LicenseError):
    """Envelope, schema, produto ou assinatura inválidos."""


class MachineMismatchError(InvalidLicenseError):
    """Licença não pertence à instalação/máquina atual."""


class ExpiredLicenseError(InvalidLicenseError):
    """Licença ainda não válida ou expirada."""


class ClockRollbackError(InvalidLicenseError):
    """Relógio do sistema retrocedeu além da tolerância."""


class RevokedLicenseError(InvalidLicenseError):
    """Licença consta em lista local de revogação assinada."""


class StorageError(LicenseError):
    """Falha ao persistir Challenge, licença ou estado de relógio."""
