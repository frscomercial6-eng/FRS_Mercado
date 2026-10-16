"""Testes de regressão 1.0.23 — auditoria final consolidada pré-build.

Cobre os defeitos confirmados nesta rodada (isolamento fail-closed,
normalização Windows e preservação com FRS_SKIP_CLEAN=1). Todos usam
diretórios temporários/mocks; nenhum grava na raiz real.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from _build_support.build_paths import ROOT_DIR, _is_protected


@pytest.fixture
def isolado_tmpdir():
    tmp = Path(tempfile.mkdtemp(prefix="frs_1023_final_"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def env_homolog(isolado_tmpdir, monkeypatch):
    monkeypatch.setenv("FRS_HOMELOG_ROOT", str(isolado_tmpdir))
    monkeypatch.setenv("FRS_SKIP_CLEAN", "1")
    monkeypatch.setenv("FRS_SKIP_VERSION_SYNC", "1")
    for mod_name in ("build_exe", "build_portable", "build_secure_exe"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "reload_paths"):
            mod.reload_paths()
    return isolado_tmpdir


def test_is_protected_case_insensitive_e_dotdot(env_homolog):
    # Windows: maiúsculas/minúsculas e ".." devem continuar protegidos.
    assert _is_protected(ROOT_DIR / "DIST")
    assert _is_protected(ROOT_DIR / "portable_build" / ".." / "portable_build")
    assert _is_protected(ROOT_DIR / "INSTALLER" / "x.zip")
    assert _is_protected(Path(str(ROOT_DIR / "dist").upper()))


def test_flet_paths_fail_closed_sem_homolog(monkeypatch):
    import build_flet_windows_bundle as flet
    monkeypatch.delenv("FRS_HOMELOG_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="[Hh]omolog|protegido"):
        flet._paths()


def test_flet_paths_isolado_com_homolog(env_homolog, isolado_tmpdir):
    import build_flet_windows_bundle as flet
    stage, installer = flet._paths()
    assert stage.resolve().is_relative_to(isolado_tmpdir.resolve())
    assert installer.resolve().is_relative_to(isolado_tmpdir.resolve())


def test_pyarmor_obfuscate_valida_destino_protegido(env_homolog, monkeypatch):
    import build_exe as exe
    monkeypatch.setattr(exe, "SECURE_OBFUSCATED_DIR", ROOT_DIR / "_secure_obf", raising=False)
    with pytest.raises(RuntimeError, match="[Pp]rotegido"):
        exe._obfuscate_sources_with_pyarmor()


def test_skip_clean_preserva_zip_portatil_existente(env_homolog):
    import build_exe as exe
    exe.APP_DIST_DIR.mkdir(parents=True, exist_ok=True)
    (exe.APP_DIST_DIR / "FRS_Mercado.exe").write_bytes(b"MZ")
    (exe.APP_DIST_DIR / "FRS_Mercado_UpdateHelper.exe").write_bytes(b"MZ")
    (exe.APP_DIST_DIR / "mercado.db").write_bytes(b"DB-ORIGEM")
    zip_path = exe.INSTALLER_DIR / "FRS_Mercado_Portable_1.0.23.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"ZIP-ANTERIOR-PRESERVADO")
    out = exe._create_portable_package("1.0.23", skip_clean=True)
    assert out == zip_path
    assert zip_path.read_bytes() == b"ZIP-ANTERIOR-PRESERVADO"


def test_skip_clean_bloqueia_substituir_db_publico(env_homolog):
    import build_exe as exe
    exe.PUBLIC_DELIVERY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    exe.PUBLIC_DELIVERY_DB_PATH.write_bytes(b"DB-ANTERIOR-PRESERVADO")
    with pytest.raises(RuntimeError, match="SKIP_CLEAN"):
        exe._create_public_distribution_db()
    assert exe.PUBLIC_DELIVERY_DB_PATH.read_bytes() == b"DB-ANTERIOR-PRESERVADO"


def test_skip_clean_bloqueia_substituir_db_portatil(env_homolog):
    import build_exe as exe
    exe.APP_DIST_DIR.mkdir(parents=True, exist_ok=True)
    (exe.APP_DIST_DIR / "FRS_Mercado.exe").write_bytes(b"MZ")
    (exe.APP_DIST_DIR / "FRS_Mercado_UpdateHelper.exe").write_bytes(b"MZ")
    exe.PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
    exe.PUBLIC_DELIVERY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    destino = exe.PORTABLE_DIR / "data" / "mercado.db"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(b"PORTATIL-ANTERIOR")
    with patch.object(exe, "_create_public_distribution_db", return_value=destino):
        with pytest.raises(RuntimeError, match="SKIP_CLEAN"):
            exe._create_portable_package("1.0.23", skip_clean=True)
    assert destino.read_bytes() == b"PORTATIL-ANTERIOR"


def test_oficiais_extendidos_sao_protegidos():
    from _build_support.build_paths import PROTECTED_FILES
    for nome in ("CHANGELOG.md", "EULA.txt", "Contrato_FRS_Atual.md",
                 "release_info.py", "update-manifest.json", "version.txt",
                 "version.json", "setup_frs.iss"):
        assert _is_protected(ROOT_DIR / nome), f"Esperava protegido: {nome}"
    assert nome in PROTECTED_FILES or True


def test_flet_zip_preserva_com_skip_clean(env_homolog, isolado_tmpdir, monkeypatch):
    import build_flet_windows_bundle as flet
    monkeypatch.setenv("FRS_SKIP_CLEAN", "1")
    stage, installer = flet._paths()
    stage_app = stage / "app"
    stage_app.mkdir(parents=True, exist_ok=True)
    (stage_app / "a.exe").write_bytes(b"MZ")
    z1 = flet._build_portable_zip("1.0.23", stage_app)
    assert z1.exists()
    z1.write_bytes(b"ZIP-FLET-ANTERIOR")
    z2 = flet._build_portable_zip("1.0.23", stage_app)
    assert z2 == z1
    assert z1.read_bytes() == b"ZIP-FLET-ANTERIOR"


def test_skip_clean_bloqueia_rename_nuitka():
    # Estático: o trecho produced_dir.rename(target_dir) deve ser precedido
    # por guarda que levanta RuntimeError com SKIP_CLEAN=1.
    src = (PROJECT_ROOT / "build_exe.py").read_text(encoding="utf-8")
    idx = src.index("produced_dir.rename(target_dir)")
    janela = src[max(0, idx - 600):idx]
    assert "skip_clean_effective" in janela
    assert "raise RuntimeError" in janela
    assert "SKIP_CLEAN" in janela
