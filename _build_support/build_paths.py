"""Configuração central de caminhos de build para homologação isolada.

Quando a variável de ambiente FRS_HOMELOG_ROOT está definida, todos os
destinos de escrita ficam dentro de um diretório dedicado de homologação.
Quando não está definida, os caminhos históricos da raiz são preservados.
Nenhum caminho isolado pode resolver para um diretório protegido da raiz.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

PROTECTED_DIRS = (
    "dist", "build", "_secure_obf", "portable_build", "installer",
    "_build_staging", "_build_support", "venv", "venv_relatorios",
    "build_venv", "build_venv311", ".git", ".pytest_cache",
    "FRS_Mercado_1.0.12_Teste", "release_final",
    "_backup_pre_build_20261010", "_audit_1_0_22_anterior",
)

PROTECTED_FILES = (
    "_build_support/version_info.txt",
    "_build_staging/mercado_1.0.19_distribuicao_limpa.db",
    "setup_frs.iss", "version.txt", "version.json",
    "release_info.py", "update-manifest.json",
    # 1.0.23 — oficiais alterados por prepare_release_artifacts(): nunca podem
    # ser destino de build/homologação nem sobrescritos com SKIP_VERSION_SYNC.
    "CHANGELOG.md", "EULA.txt", "EULA.template.txt",
    "Contrato_FRS_Atual.md", "contrato_template.md",
    "checklist_homologacao.md", "setup_flet_windows_bundle.iss",
)


def _is_protected(path: Path) -> bool:
    # 1.0.23 — normalização Windows: resolve ".."/".", normcase para
    # case-insensitividade (C:\X == c:\x) antes de comparar.
    # LIMITAÇÃO REGISTRADA: junctions/symlinks externos que apontam para
    # fora da homologação mas resolvem para dentro (ou vice-versa) dependem
    # de Path.resolve(); links que não podem ser resolvidos (alvo ausente,
    # permissão negada) não têm proteção comprovada dinamicamente.
    resolved = os.path.normcase(str(path.resolve()))
    for protected in PROTECTED_DIRS:
        protected_path = os.path.normcase(str((ROOT_DIR / protected).resolve()))
        try:
            Path(resolved).relative_to(protected_path)
            return True
        except ValueError:
            pass
    for protected_file in PROTECTED_FILES:
        protected_file_path = os.path.normcase(str((ROOT_DIR / protected_file).resolve()))
        if resolved == protected_file_path:
            return True
    return False


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    p = Path(value) if value else default
    if _is_protected(p):
        raise RuntimeError(
            f"Caminho de build protegido detectado: {p}. "
            f"Defina {name} para um diretório fora dos protegidos da raiz."
        )
    return p


@dataclass(frozen=True)
class BuildPaths:
    root: Path
    homol_root: Path | None
    dist_dir: Path
    build_dir: Path
    portable_dir: Path
    installer_dir: Path
    obf_dir: Path
    support_dir: Path
    payload_dir: Path
    staging_db: Path
    version_info: Path
    iss_temp: Path
    skip_clean: bool
    skip_version_sync: bool

    def is_protected(self, path: Path) -> bool:
        return _is_protected(path)


def load_build_paths() -> BuildPaths:
    """Carrega e valida todos os caminhos de build a partir do ambiente."""
    homol_root_env = os.getenv("FRS_HOMELOG_ROOT")
    homol_root = Path(homol_root_env) if homol_root_env else None

    if homol_root is not None:
        base = homol_root.resolve()
        dist_dir = base / "dist" / "FRS_Mercado"
        build_dir = base / "build"
        portable_dir = base / "portable_build"
        installer_dir = base / "installer"
        obf_dir = base / "_secure_obf"
        support_dir = base / "_build_support"
        payload_dir = base / "_build_support_payload"
        staging_db = base / "_build_staging" / "mercado_1.0.23_distribuicao_limpa.db"
        version_info = base / "_build_support" / "version_info.txt"
        iss_temp = base / "_iss_temp" / "setup_frs_homolog.iss"
    else:
        dist_dir = ROOT_DIR / "dist" / "FRS_Mercado"
        build_dir = ROOT_DIR / "build"
        portable_dir = ROOT_DIR / "portable_build"
        installer_dir = ROOT_DIR / "installer"
        obf_dir = ROOT_DIR / "_secure_obf"
        support_dir = ROOT_DIR / "_build_support"
        payload_dir = ROOT_DIR / "_build_support_payload"
        staging_db = ROOT_DIR / "_build_staging" / "mercado_1.0.19_distribuicao_limpa.db"
        version_info = ROOT_DIR / "_build_support" / "version_info.txt"
        iss_temp = ROOT_DIR / "_iss_temp" / "setup_frs_homolog.iss"

    for label, p in (
        ("dist_dir", dist_dir), ("build_dir", build_dir),
        ("portable_dir", portable_dir), ("installer_dir", installer_dir),
        ("obf_dir", obf_dir), ("support_dir", support_dir),
        ("payload_dir", payload_dir), ("staging_db", staging_db),
        ("version_info", version_info), ("iss_temp", iss_temp),
    ):
        if _is_protected(p):
            raise RuntimeError(
                f"Caminho protegido detectado para {label}: {p}. "
                "Reveja FRS_HOMELOG_ROOT ou as variáveis FRS_BUILD_*."
            )

    skip_clean = os.getenv("FRS_SKIP_CLEAN", "0").strip().lower() in {"1", "true", "yes", "sim"}
    skip_version_sync = (
        os.getenv("FRS_SKIP_VERSION_SYNC", "0").strip().lower() in {"1", "true", "yes", "sim"}
    )

    return BuildPaths(
        root=ROOT_DIR,
        homol_root=homol_root,
        dist_dir=dist_dir,
        build_dir=build_dir,
        portable_dir=portable_dir,
        installer_dir=installer_dir,
        obf_dir=obf_dir,
        support_dir=support_dir,
        payload_dir=payload_dir,
        staging_db=staging_db,
        version_info=version_info,
        iss_temp=iss_temp,
        skip_clean=skip_clean,
        skip_version_sync=skip_version_sync,
    )
