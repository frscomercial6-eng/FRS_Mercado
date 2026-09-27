"""Compatibilidade segura do atualizador do FRS Mercado.

A implementação de downloads, manifestos, gate e helper está em
`updater_secure.py`. Este arquivo não mantém o caminho legado baseado em
`version.json` não assinado.
"""

from updater_secure import (
    ReleaseInfo,
    UpdateSecurityError,
    UpdateState,
    Updater,
    compare_versions,
    current_ts,
    fetch_manifest_payload,
    fetch_update_manifest,
    get_local_version,
    normalize_version,
    read_update_state,
    verify_manifest,
    write_update_state,
)


def fetch_latest_release(repo: str):
    return fetch_update_manifest(repo)


def check_and_apply_startup_update(repo: str) -> bool:
    """O bootstrap não consulta a rede; a checagem ocorre após o login."""
    return False


__all__ = [
    "ReleaseInfo",
    "UpdateSecurityError",
    "UpdateState",
    "Updater",
    "check_and_apply_startup_update",
    "compare_versions",
    "current_ts",
    "fetch_latest_release",
    "fetch_manifest_payload",
    "fetch_update_manifest",
    "get_local_version",
    "normalize_version",
    "read_update_state",
    "verify_manifest",
    "write_update_state",
]
