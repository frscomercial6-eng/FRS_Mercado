"""Testes de regressão 1.0.23 — lacunas pré-build (parte 1: portable/iss_temp).

Cada teste prova DUAS coisas: (1) a operação é INTERROMPIDA com
RuntimeError fail-closed; (2) NENHUMA escrita/exclusão ocorreu no destino
protegido (mkdir/copy/ZIP/sqlite/rmtree/unlink não chamados).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from _build_support.build_paths import ROOT_DIR


@pytest.fixture
def isolado_tmpdir():
    tmp = Path(tempfile.mkdtemp(prefix="frs_1023_gap_"))
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


def test_create_portable_package_aborta_antes_de_escrever(env_homolog, monkeypatch):
    import build_exe as exe
    monkeypatch.setattr(exe, "PORTABLE_DIR", ROOT_DIR / "portable_build", raising=False)
    monkeypatch.setattr(exe, "INSTALLER_DIR", ROOT_DIR / "installer", raising=False)
    with patch("pathlib.Path.mkdir") as mk, \
         patch("shutil.copy2") as cp, \
         patch("shutil.copytree") as cpt, \
         patch("shutil.rmtree") as rm, \
         patch("zipfile.ZipFile") as zf, \
         patch("sqlite3.connect") as lite:
        with pytest.raises(RuntimeError, match="[Pp]rotegido"):
            exe._create_portable_package("1.0.23", skip_clean=False)
        mk.assert_not_called()
        cp.assert_not_called()
        cpt.assert_not_called()
        rm.assert_not_called()
        zf.assert_not_called()
        lite.assert_not_called()


def test_iss_temp_build_exe_aborta_antes_de_escrever(env_homolog):
    import build_exe as exe
    orig = exe.PATHS.iss_temp
    try:
        object.__setattr__(exe.PATHS, "iss_temp", ROOT_DIR / "_iss_temp" / "z.iss")
        with patch("pathlib.Path.mkdir") as mk, \
             patch("pathlib.Path.write_text") as wt:
            with pytest.raises(RuntimeError, match="[Pp]rotegido|homologa"):
                exe._build_installer("1.0.23")
            mk.assert_not_called()
            wt.assert_not_called()
    finally:
        object.__setattr__(exe.PATHS, "iss_temp", orig)


def test_secure_exe_zip_e_setup_abortam_antes_de_escrever(env_homolog):
    import build_secure_exe as sec
    orig_installer = sec.INSTALLER_DIR
    try:
        sec.INSTALLER_DIR = ROOT_DIR / "installer"
        with patch("pathlib.Path.mkdir") as mk, \
             patch("pathlib.Path.write_text") as wt, \
             patch("pathlib.Path.unlink") as ul, \
             patch("zipfile.ZipFile") as zf:
            with pytest.raises(RuntimeError, match="[Pp]rotegido|homologa"):
                sec._create_portable_zip("1.0.23", sec.DIST_DIR)
            mk.assert_not_called()
            wt.assert_not_called()
            ul.assert_not_called()
            zf.assert_not_called()
            with pytest.raises(RuntimeError, match="[Pp]rotegido|homologa"):
                sec._build_setup_if_available()
            mk.assert_not_called()
            wt.assert_not_called()
    finally:
        sec.INSTALLER_DIR = orig_installer


def test_iss_temp_isolado_nos_tres_scripts(env_homolog, isolado_tmpdir):
    import build_exe as exe
    import build_secure_exe as sec
    import build_flet_windows_bundle as flet
    assert exe.PATHS.iss_temp.resolve().is_relative_to(isolado_tmpdir.resolve())
    assert sec.PATHS.iss_temp.resolve().is_relative_to(isolado_tmpdir.resolve())
    stage, installer = flet._paths()
    assert stage.resolve().is_relative_to(isolado_tmpdir.resolve())
    assert installer.resolve().is_relative_to(isolado_tmpdir.resolve())
