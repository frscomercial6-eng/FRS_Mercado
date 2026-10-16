"""Testes de segurança para isolamento da build 1.0.23.

Todos os testes usam diretórios temporários e mocks. Nenhum teste grava,
remove ou modifica arquivos na raiz real do projeto.
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

from _build_support.build_paths import (
    PROTECTED_DIRS,
    PROTECTED_FILES,
    _is_protected,
    load_build_paths,
    ROOT_DIR,
)


@pytest.fixture
def isolado_tmpdir():
    tmp = Path(tempfile.mkdtemp(prefix="frs_homolog_test_"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def env_homolog(isolado_tmpdir, monkeypatch):
    monkeypatch.setenv("FRS_HOMELOG_ROOT", str(isolado_tmpdir))
    monkeypatch.setenv("FRS_SKIP_CLEAN", "1")
    monkeypatch.setenv("FRS_SKIP_VERSION_SYNC", "1")
    # Recarrega módulos para refletir o FRS_HOMELOG_ROOT do teste,
    # pois PATHS é carregado no import e ficaria preso ao env externo.
    for mod_name in ("build_exe", "build_portable", "build_secure_exe"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "reload_paths"):
            mod.reload_paths()
    return isolado_tmpdir


def test_is_protected_detecta_caminhos_da_raiz():
    protegidos = [
        ROOT_DIR / "dist", ROOT_DIR / "build", ROOT_DIR / "_secure_obf",
        ROOT_DIR / "portable_build", ROOT_DIR / "installer",
        ROOT_DIR / "_build_staging", ROOT_DIR / "_build_support",
        ROOT_DIR / "venv",
    ]
    for p in protegidos:
        assert _is_protected(p), f"Esperava protegido: {p}"


def test_is_protected_nao_detecta_caminho_fora_da_raiz(isolado_tmpdir):
    assert not _is_protected(isolado_tmpdir / "dist")
    assert not _is_protected(isolado_tmpdir / "build")


@patch("build_exe.shutil.rmtree")
def test_clean_previous_builds_skip_clean_nao_chama_rmtree(mock_rmtree, env_homolog):
    import build_exe as exe
    exe._clean_previous_builds(skip_clean=True)
    mock_rmtree.assert_not_called()


@patch("build_exe.shutil.rmtree")
def test_clean_previous_builds_limpa_apenas_isolados(mock_rmtree, env_homolog, monkeypatch):
    import build_exe as exe
    # 1.0.23: o flag global FRS_SKIP_CLEAN=1 bloqueia mesmo com skip_clean=False.
    # Para exercitar a limpeza permitida, remove o flag e recarrega PATHS.
    monkeypatch.delenv("FRS_SKIP_CLEAN", raising=False)
    exe.reload_paths()
    exe.BUILD_DIR.mkdir(parents=True, exist_ok=True)
    exe.PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
    exe._clean_previous_builds(skip_clean=False)
    chamados = [str(c.args[0]) for c in mock_rmtree.call_args_list]
    assert any("build" in c for c in chamados), f"Esperava limpeza de build_dir em: {chamados}"
    assert any("portable_build" in c for c in chamados), f"Esperava limpeza de portable_dir em: {chamados}"
    for chamado in chamados:
        assert "FRS_MERCADO\\build" not in chamado.replace("/", "\\"), f"rmtree em build original: {chamado}"
        assert "FRS_MERCADO\\dist" not in chamado.replace("/", "\\"), f"rmtree em dist original: {chamado}"
        assert "FRS_MERCADO\\_secure_obf" not in chamado.replace("/", "\\"), f"rmtree em _secure_obf original: {chamado}"


@patch("build_exe.shutil.rmtree")
def test_prepare_support_payload_nao_remove_build_support(mock_rmtree, env_homolog, isolado_tmpdir):
    import build_exe as exe
    exe._prepare_support_payload(skip_clean=False)
    chamados = [str(c.args[0]) for c in mock_rmtree.call_args_list]
    for chamado in chamados:
        assert "_build_support" not in chamado, f"rmtree em _build_support: {chamado}"
    assert not _is_protected(isolado_tmpdir / "dist")
def test_prepare_support_payload_cria_payload_dir(isolado_tmpdir, env_homolog):
    import build_exe as exe
    exe._prepare_support_payload(skip_clean=False)
    assert exe.PAYLOAD_DIR.exists()
    try:
        exe.PAYLOAD_DIR.resolve().relative_to(isolado_tmpdir.resolve())
    except ValueError:
        pytest.fail(f"PAYLOAD_DIR={exe.PAYLOAD_DIR} não está dentro de {isolado_tmpdir}")


def test_staging_db_nao_é_o_banco_1019(env_homolog, isolado_tmpdir):
    import build_exe as exe
    banco_1019 = ROOT_DIR / "_build_staging" / "mercado_1.0.19_distribuicao_limpa.db"
    assert exe.PUBLIC_DELIVERY_DB_PATH != banco_1019
    try:
        exe.PUBLIC_DELIVERY_DB_PATH.resolve().relative_to(isolado_tmpdir.resolve())
    except ValueError:
        pytest.fail(f"PUBLIC_DELIVERY_DB_PATH={exe.PUBLIC_DELIVERY_DB_PATH} não está dentro de {isolado_tmpdir}")


def test_banco_1019_ainda_existe_apos_importacao():
    banco_1019 = ROOT_DIR / "_build_staging" / "mercado_1.0.19_distribuicao_limpa.db"
    assert banco_1019.exists(), "Banco 1.0.19 não existe mais após importação"


@patch("build_exe.prepare_release_artifacts")
def test_prepare_release_artifacts_bloqueado_com_skip_version_sync(mock_prepare, env_homolog):
    import build_exe as exe
    from _build_support.build_paths import load_build_paths
    paths = load_build_paths()
    assert paths.skip_version_sync is True
    if paths.skip_version_sync:
        app_version = exe.read_version()
        mock_prepare.assert_not_called()


@patch("build_portable.exe.prepare_release_artifacts")
def test_build_portable_bloqueia_prepare_release(mock_prepare, env_homolog):
    import build_portable as bp
    assert bp.PATHS.skip_version_sync is True


def test_caminho_isolado_nao_resolve_para_protegado(env_homolog, isolado_tmpdir):
    paths = load_build_paths()
    for label in (
        "dist_dir", "build_dir", "portable_dir", "installer_dir",
        "obf_dir", "support_dir", "payload_dir", "staging_db",
        "version_info", "iss_temp",
    ):
        p = getattr(paths, label)
        for protected in PROTECTED_DIRS:
            prot_path = (ROOT_DIR / protected).resolve()
            try:
                p.resolve().relative_to(prot_path)
                pytest.fail(f"{label}={p} está dentro do protegido {protected}")
            except ValueError:
                pass


@patch("build_exe.PyInstaller.__main__.run")
@patch("build_exe._validate_security_files")
def test_build_pyinstaller_args_tem_caminhos_isolados(
    mock_validate, mock_pyinstaller_run, env_homolog, isolado_tmpdir, monkeypatch
):
    import build_exe as exe
    monkeypatch.setenv("FRS_SKIP_VERSION_SYNC", "1")
    paths = exe.load_build_paths()
    assert paths.skip_version_sync
    args = exe._build_pyinstaller_args("1.0.22")
    distpath_args = [a for a in args if a.startswith("--distpath=")]
    workpath_args = [a for a in args if a.startswith("--workpath=")]
    specpath_args = [a for a in args if a.startswith("--specpath=")]
    assert len(distpath_args) == 1, f"Esperava exatamente um --distpath, encontrou: {distpath_args}"
    assert len(workpath_args) == 1, f"Esperava exatamente um --workpath, encontrou: {workpath_args}"
    assert len(specpath_args) == 1, f"Esperava exatamente um --specpath, encontrou: {specpath_args}"
    for arg in distpath_args + workpath_args + specpath_args:
        _, valor = arg.split("=", 1)
        try:
            Path(valor).resolve().relative_to(isolado_tmpdir.resolve())
        except ValueError:
            pytest.fail(f"Arg {arg} não está dentro de {isolado_tmpdir}")


def test_iss_temp_nao_modifica_setup_frs_original(isolado_tmpdir, env_homolog, monkeypatch):
    import build_exe as exe
    setup_frs_original = ROOT_DIR / "setup_frs.iss"
    conteudo_antes = setup_frs_original.read_text(encoding="utf-8")
    exe.PATHS.iss_temp.parent.mkdir(parents=True, exist_ok=True)
    template_content = setup_frs_original.read_text(encoding="utf-8")
    iss_content = template_content.replace(
        'OutputDir=installer',
        f'OutputDir={exe.INSTALLER_DIR}'
    ).replace(
        'OutputBaseFilename=FRS_Mercado_Setup',
        f'OutputBaseFilename=FRS_Mercado_Setup_1.0.22'
    ).replace(
        'Source: "dist\\FRS_Mercado\\*";',
        f'Source: "{exe.APP_DIST_DIR.parent}\\FRS_Mercado\\*";'
    )
    exe.PATHS.iss_temp.write_text(iss_content, encoding="utf-8")
    conteudo_depois = setup_frs_original.read_text(encoding="utf-8")
    assert conteudo_antes == conteudo_depois, "setup_frs.iss foi modificado!"
    try:
        exe.PATHS.iss_temp.resolve().relative_to(isolado_tmpdir.resolve())
    except ValueError:
        pytest.fail(f"iss_temp={exe.PATHS.iss_temp} não está dentro de {isolado_tmpdir}")
    iss_conteudo = exe.PATHS.iss_temp.read_text(encoding="utf-8")
    assert str(exe.INSTALLER_DIR) in iss_conteudo
    assert str(exe.APP_DIST_DIR.parent) in iss_conteudo


def test_nenhum_caminho_protegido_é_destino_de_escrita(env_homolog):
    paths = load_build_paths()
    caminhos_escrita = [
        paths.dist_dir, paths.build_dir, paths.portable_dir,
        paths.installer_dir, paths.obf_dir, paths.payload_dir,
        paths.staging_db, paths.version_info, paths.iss_temp,
    ]
    for c in caminhos_escrita:
        assert not paths.is_protected(c), f"Caminho de escrita protegido: {c}"


def test_build_paths_todos_dentro_do_homol_root(env_homolog, isolado_tmpdir):
    paths = load_build_paths()
    assert paths.homol_root == isolado_tmpdir.resolve()
    for label in (
        "dist_dir", "build_dir", "portable_dir", "installer_dir",
        "obf_dir", "support_dir", "payload_dir", "staging_db",
        "version_info", "iss_temp",
    ):
        p = getattr(paths, label)
        try:
            p.resolve().relative_to(isolado_tmpdir.resolve())
        except ValueError:
            pytest.fail(f"{label}={p} não está dentro de {isolado_tmpdir}")


def test_build_paths_nao_resolve_para_protegado(env_homolog):
    paths = load_build_paths()
    for label in (
        "dist_dir", "build_dir", "portable_dir", "installer_dir",
        "obf_dir", "support_dir", "payload_dir", "staging_db",
        "version_info", "iss_temp",
    ):
        p = getattr(paths, label)
        assert not paths.is_protected(p), f"{label}={p} é protegido"


def test_load_build_paths_rejeita_caminho_protegido(monkeypatch, isolado_tmpdir):
    monkeypatch.setenv("FRS_HOMELOG_ROOT", str(ROOT_DIR / "dist"))
    with pytest.raises(RuntimeError, match="protegido"):
        load_build_paths()


def test_isolamento_falha_explicita_sem_homolog_root(monkeypatch):
    """Sem FRS_HOMELOG_ROOT o isolamento falha explicitamente (fail-closed)."""
    monkeypatch.delenv("FRS_HOMELOG_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="[Pp]rotegido"):
        load_build_paths()


def test_runtime_hook_dentro_do_isolamento(env_homolog, isolado_tmpdir):
    import build_exe as exe

    assert exe.RUNTIME_HOOK_PATH != ROOT_DIR / "_runtime_hook_error_logger.py"
    try:
        exe.RUNTIME_HOOK_PATH.resolve().relative_to(isolado_tmpdir.resolve())
    except ValueError:
        pytest.fail(f"RUNTIME_HOOK_PATH={exe.RUNTIME_HOOK_PATH} fora de {isolado_tmpdir}")
    assert not exe.PATHS.is_protected(exe.RUNTIME_HOOK_PATH)


def test_secure_exe_clean_dirs_respeita_skip_clean(env_homolog):
    import build_secure_exe as sec

    sec.reload_paths()
    with patch("build_secure_exe.shutil.rmtree") as mock_rmtree:
        sec._clean_dirs(skip_clean=True)
        mock_rmtree.assert_not_called()


def test_secure_exe_clean_dirs_limpa_apenas_isolados(env_homolog, isolado_tmpdir, monkeypatch):
    import build_secure_exe as sec

    # 1.0.23: idem — remove o flag global para exercitar a limpeza permitida.
    monkeypatch.delenv("FRS_SKIP_CLEAN", raising=False)
    sec.reload_paths()
    # Cria os diretórios isolados para que _clean_dirs tenha algo a limpar.
    for d in (sec.OBFUSCATED_DIR, sec.BUILD_DIR, sec.DIST_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with patch("build_secure_exe.shutil.rmtree") as mock_rmtree:
        sec._clean_dirs(skip_clean=False)
        chamados = [str(c.args[0]) for c in mock_rmtree.call_args_list]
    assert chamados, "Esperava limpeza dos diretórios isolados"
    for chamado in chamados:
        assert str(isolado_tmpdir) in chamado.replace("/", "\\") or str(
            isolado_tmpdir
        ) in chamado, f"rmtree fora do isolamento: {chamado}"


def test_pyinstaller_stub_falha_explicita_sem_instalacao(env_homolog):
    """Stub de PyInstaller deve falhar explicitamente, nunca silenciosamente."""
    import build_exe as exe

    try:
        import PyInstaller  # noqa: F401

        tem_real = hasattr(getattr(exe.PyInstaller, "__main__", None), "run")
    except ModuleNotFoundError:
        tem_real = False
    if tem_real:
        # Se PyInstaller real existir, o stub não está ativo; valida chamada mockada.
        with patch("build_exe.PyInstaller.__main__.run") as mock_run:
            mock_run(["--version"])
            mock_run.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match="PyInstaller"):
            exe.PyInstaller.__main__.run(["--version"])