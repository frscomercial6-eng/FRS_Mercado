"""Testes de regressão 1.0.23 — lacunas pré-build (parte 2: skip flags + DDL).

- FRS_SKIP_CLEAN=1 bloqueia TODA limpeza (helper + Nuitka), sem bloquear
  escrita normal de novos artefatos no diretório isolado.
- FRS_SKIP_VERSION_SYNC=1 preserva versão oficial e releases oficiais.
- read_version() é leitura pura (nunca cria version.txt).
- DDL da distribuição confere com o schema esperado, em banco temporário
  descartável criado por database.init_db(). Nunca usa banco de cliente.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def isolado_tmpdir():
    tmp = Path(tempfile.mkdtemp(prefix="frs_1023_gap2_"))
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


def test_skip_clean_bloqueia_toda_limpeza(env_homolog):
    import build_exe as exe
    assert exe.SKIP_CLEAN is True
    with patch("build_exe.shutil.rmtree") as rm:
        exe._clean_previous_builds(skip_clean=False)
        rm.assert_not_called()
    with patch("build_exe.shutil.rmtree") as rm:
        exe._prepare_support_payload(skip_clean=False)
        rm.assert_not_called()
    import build_secure_exe as sec
    sec.reload_paths()
    with patch("build_secure_exe.shutil.rmtree") as rm:
        sec._clean_dirs(skip_clean=False)
        rm.assert_not_called()
    with patch("build_exe.shutil.rmtree") as rm, \
         patch("build_exe.subprocess.run"):
        try:
            exe._build_with_nuitka_secure_fallback("1.0.23", skip_clean=True)
        except (FileNotFoundError, RuntimeError):
            pass
        rm.assert_not_called()


def test_skip_version_sync_preserva_oficiais(env_homolog):
    import build_exe as exe
    assert exe.SKIP_VERSION_SYNC is True
    oficiais = ["version.txt", "version.json", "release_info.py",
                "update-manifest.json", "EULA.txt", "Contrato_FRS_Atual.md",
                "CHANGELOG.md", "setup_frs.iss"]
    conteudos = {n: (PROJECT_ROOT / n).read_bytes()
                 for n in oficiais if (PROJECT_ROOT / n).exists()}
    mtimes = {n: (PROJECT_ROOT / n).stat().st_mtime for n in conteudos}
    v = exe.read_version()
    assert v == (PROJECT_ROOT / "version.txt").read_text(encoding="utf-8").strip()
    for n, b in conteudos.items():
        assert (PROJECT_ROOT / n).read_bytes() == b, f"{n} alterado!"
        assert (PROJECT_ROOT / n).stat().st_mtime == mtimes[n], f"{n} tocado!"


def test_read_version_nao_cria_arquivo(tmp_path, monkeypatch):
    import release_manager as rm
    monkeypatch.setattr(rm, "VERSION_FILE", tmp_path / "version.txt")
    assert not (tmp_path / "version.txt").exists()
    with pytest.raises(FileNotFoundError):
        rm.read_version()
    assert not (tmp_path / "version.txt").exists(), "read_version criou arquivo!"


def test_ddl_distribuicao_confere_com_schema(tmp_path):
    tmp_appdata = tmp_path / "appdata"
    tmp_appdata.mkdir()
    env = os.environ.copy()
    env["APPDATA"] = str(tmp_appdata)
    subprocess.run([sys.executable, "-c", "import database; database.init_db()"],
                   cwd=str(PROJECT_ROOT), env=env, check=True)
    dbs = list(tmp_appdata.rglob("mercado.db"))
    assert dbs, "init_db nao criou mercado.db descartavel"
    conn = sqlite3.connect(str(dbs[0]))
    try:
        tabelas = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        esperadas = {
            "produtos", "vendas", "vendas_dia", "itens_venda", "clientes",
            "vales", "vale_itens", "orcamentos", "orcamento_itens",
            "financeiro", "caixa_operacao", "caixa_conferencia", "sangrias",
            "logs_auditoria", "logs_mentoria", "entradas", "usuarios",
            "licenca", "fornecedores", "fornecedor_produtos",
            "produto_lotes", "config_sistema", "config_fiscal",
            "config_taxas", "config_aliquotas_ncm",
            "config_aliquotas_fiscais_ncm",
        }
        faltando = {t for t in esperadas if t not in tabelas}
        assert not faltando, f"Tabelas esperadas ausentes: {sorted(faltando)}"
        for t in ("produtos", "vendas", "clientes", "usuarios", "licenca"):
            if t in tabelas:
                assert conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == 0, \
                    f"banco limpo com dados em {t}"
    finally:
        conn.close()


def test_auditoria_chamadas_escrita_tem_trava_protegida():
    """Audita shutil.rmtree/unlink/remove/replace/rename/copy/copytree/
    write_text/write_bytes/ZipFile-w/sqlite3.connect/mkdir nos scripts de
    build e exige que cada função de escrita/limpeza contenha trava
    is_protected ou SKIP_CLEAN antes da chamada."""
    import ast
    alvos = ["build_exe.py", "build_secure_exe.py", "build_portable.py",
             "build_flet_windows_bundle.py"]
    # Chamadas reais de escrita/exclusão (atributo ou nome da chamada).
    ops = {"rmtree", "unlink", "remove", "replace", "rename", "copy2",
           "copytree", "write_text", "write_bytes", "ZipFile", "connect",
           "mkdir"}
    # Helpers genéricos sem trava própria (proteção é do chamador) +
    # validadores somente-leitura (abrem ZIP/DB em modo leitura).
    helpers_sem_trava = {"_copy_if_exists", "_read_current_file", "_confirm",
                         "_parse_args", "_find_iscc", "_find_tool", "_find_pyarmor_cli",
                         "_resolve_entrypoint", "_resolve_customtkinter_assets_dir",
                         "_list_app_sources", "_list_sources_for_obfuscation",
                         "_locate_flet_release_dir", "_run", "_run_flet_build_windows",
                         "_validate_public_portable", "_assert_no_py_sources"}
    for nome in alvos:
        src = (PROJECT_ROOT / nome).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in helpers_sem_trava:
                    continue
                chamadas = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        f = sub.func
                        if isinstance(f, ast.Attribute):
                            chamadas.add(f.attr)
                        elif isinstance(f, ast.Name):
                            chamadas.add(f.id)
                tem_op = bool(chamadas & ops)
                if tem_op:
                    corpo = ast.dump(node)
                    tem_trava = ("is_protected" in corpo or "SKIP_CLEAN" in corpo
                                 or "_require_not_protected" in corpo
                                 or "_paths" in corpo)
                    assert tem_trava, (
                        f"{nome}::{node.name} chama {sorted(chamadas & ops)} "
                        "sem trava is_protected/SKIP_CLEAN"
                    )
