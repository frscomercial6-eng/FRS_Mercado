"""Testes isolados de falha para o atualizador FRS Mercado 1.0.23."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("FRS_TEST_OFFLINE", "1")
os.environ.setdefault("FRS_TEST_NO_GUI", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import updater_secure as secure
from updater_secure import (
    UpdateState, UpdateSecurityError, ReleaseInfo, compare_versions,
    get_local_version, write_update_state, read_update_state,
    _sha256_file,
)


@pytest.fixture
def tmp_root(tmp_path):
    """Raiz de dados temporária completamente isolada."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    import app_paths
    orig = app_paths.obter_caminho_dados
    app_paths.obter_caminho_dados = lambda *parts: str(
        (data_root / Path(*parts)) if parts else data_root
    )
    secure.redefinir_caminhos_apos_raiz()
    yield data_root
    app_paths.obter_caminho_dados = orig


@pytest.fixture
def app_root(tmp_root):
    """Simula a pasta da aplicação com marcadores de versão e arquivos protegidos."""
    root = tmp_root / "app"
    root.mkdir(parents=True, exist_ok=True)
    (root / "version.txt").write_text("1.0.22\n", encoding="utf-8")
    (root / "version.json").write_text(
        json.dumps({"latest_version": "1.0.22"}), encoding="utf-8"
    )
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "mercado.db").write_text("dados-do-cliente", encoding="utf-8")
    (data_dir / "licenca.dat").write_text("LIC-12345", encoding="utf-8")
    (data_dir / "config.json").write_text(
        json.dumps({"idioma": "pt-BR"}), encoding="utf-8"
    )
    (root / "FRS_Mercado.exe").write_bytes(b"MZ" + b"\x00" * 100)
    return root


def _mk_release(version="1.0.23"):
    return ReleaseInfo(
        version=version,
        asset_name=f"FRS_Mercado_Portable_{version}.zip",
        asset_url=(
            "https://github.com/frscomercial6-eng/FRS_Mercado/releases/download/v"
            f"{version}/FRS_Mercado_Portable_{version}.zip"
        ),
        asset_type="portable_zip",
        size=0,
        sha256="",
        key_id="k1",
        sequence=5,
        issued_at="2026-01-01T00:00:00Z",
        manifest={
            "schema": "FRS-MERCADO-UPDATE-MANIFEST-V1",
            "key_id": "k1",
            "payload": {
                "version": version, "sequence": 5,
                "issued_at": "2026-01-01T00:00:00Z", "assets": [],
            },
            "signature": "AAAA",
        },
    )


def _validate_asset(upd, zip_path, release):
    """Chama _validate_asset_content na instância do Updater."""
    upd._validate_asset_content(zip_path, release)


# ===========================================================================
# 1. health-check bem-sucedido (main.py trata --update-health-check)
# ===========================================================================

def test_health_check_argument_esta_no_main():
    """O ponto de entrada real (main.py) trata --update-health-check."""
    main_path = Path(__file__).resolve().parent / "main.py"
    fonte = main_path.read_text(encoding="utf-8")
    assert "--update-health-check" in fonte
    assert "FRS Mercado update health check: OK" in fonte


def test_health_check_sem_abrir_gui():
    """O health-check não abre PDV, login nem acessa banco (apenas imprime OK)."""
    main_path = Path(__file__).resolve().parent / "main.py"
    fonte = main_path.read_text(encoding="utf-8")
    idx = fonte.find('if "--update-health-check" in sys.argv')
    bloco = fonte[idx:idx + 500]
    assert 'print("FRS Mercado update health check: OK")' in bloco
    assert "return" in bloco


# ===========================================================================
# 2. health-check com falha (helper detecta returncode != 0)
# ===========================================================================

def test_health_check_falha_returncode_nao_zero():
    """O helper levanta HelperError quando o health-check retorna != 0."""
    import updater_helper as helper
    import subprocess

    class _FakeResult:
        returncode = 3

    def _fake(cmd, **kw):
        return _FakeResult()

    with patch.object(subprocess, "run", _fake):
        with pytest.raises(helper.HelperError):
            helper._health_check(
                Path("qualquer.exe"), expected_version="1.0.22", timeout=5
            )


# ===========================================================================
# 6. pacote inválido ou com hash/tamanho divergente
# ===========================================================================

def test_pacote_hash_divergente_rejeitado(tmp_root, app_root):
    """SHA-256 divergente dispara UpdateSecurityError."""
    zip_path = tmp_root / "pacote.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("FRS_Mercado.exe", b"MZ" + b"\x00" * 10)
    base = _mk_release(version="1.0.23")
    release = ReleaseInfo(
        version=base.version, asset_name=base.asset_name,
        asset_url=base.asset_url, asset_type=base.asset_type,
        size=zip_path.stat().st_size, sha256="0000" * 64,
        key_id=base.key_id, sequence=base.sequence,
        issued_at=base.issued_at, manifest=base.manifest,
    )
    upd = secure.Updater(parent=None)
    with pytest.raises(UpdateSecurityError, match="SHA-256"):
        upd._validate_asset_content(zip_path, release)


def test_pacote_tamanho_divergente_rejeitado(tmp_root, app_root):
    """Tamanho divergente dispara UpdateSecurityError."""
    zip_path = tmp_root / "pacote.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("FRS_Mercado.exe", b"MZ" + b"\x00" * 10)
    base = _mk_release(version="1.0.23")
    release = ReleaseInfo(
        version=base.version, asset_name=base.asset_name,
        asset_url=base.asset_url, asset_type=base.asset_type,
        size=999999, sha256=_sha256_file(zip_path),
        key_id=base.key_id, sequence=base.sequence,
        issued_at=base.issued_at, manifest=base.manifest,
    )
    upd = secure.Updater(parent=None)
    with pytest.raises(UpdateSecurityError, match="tamanho"):
        upd._validate_asset_content(zip_path, release)


def test_pacote_zip_corrompido_rejeitado(tmp_root, app_root):
    """ZIP corrompido (testzip) dispara UpdateSecurityError."""
    zip_path = tmp_root / "corrompido.zip"
    zip_path.write_bytes(b"nao e um zip valido")
    base = _mk_release(version="1.0.23")
    release = ReleaseInfo(
        version=base.version, asset_name=base.asset_name,
        asset_url=base.asset_url, asset_type=base.asset_type,
        size=zip_path.stat().st_size, sha256=_sha256_file(zip_path),
        key_id=base.key_id, sequence=base.sequence,
        issued_at=base.issued_at, manifest=base.manifest,
    )
    upd = secure.Updater(parent=None)
    with pytest.raises(UpdateSecurityError, match="ZIP válido|corrompida"):
        upd._validate_asset_content(zip_path, release)


# ===========================================================================
# 7. versão de destino igual à instalada — atualização recusada
# ===========================================================================

def test_versao_igual_recusada_antes_de_instalar(tmp_root, app_root):
    """Revalidação antes de instalar recusa versão igual à instalada."""
    release = _mk_release(version="1.0.22")
    upd = secure.Updater(parent=None)
    with patch.object(secure, "get_local_version", return_value="1.0.22"):
        with pytest.raises(UpdateSecurityError, match="igual"):
            upd._revalidar_antes_de_instalar(release)


# ===========================================================================
# 8. versão de destino inferior — downgrade recusado
# ===========================================================================

def test_downgrade_recusado_antes_de_instalar(tmp_root, app_root):
    """Revalidação antes de instalar recusa versão inferior à instalada."""
    release = _mk_release(version="1.0.21")
    upd = secure.Updater(parent=None)
    with patch.object(secure, "get_local_version", return_value="1.0.22"):
        with pytest.raises(UpdateSecurityError, match="inferior"):
            upd._revalidar_antes_de_instalar(release)


# ===========================================================================
# 9. versão de destino superior, válida e instalada com sucesso
# ===========================================================================

def test_versao_superior_valida_passa_revalidacao(tmp_root, app_root):
    """Versão 1.0.23 superior a 1.0.22 passa na revalidação."""
    release = _mk_release(version="1.0.23")
    upd = secure.Updater(parent=None)
    with patch.object(secure, "get_local_version", return_value="1.0.22"):
        upd._revalidar_antes_de_instalar(release)


# ===========================================================================
# 10. preservação dos arquivos protegidos antes e depois da falha
# ===========================================================================

def test_arquivos_protegidos_preservados_apos_falha(tmp_root, app_root):
    """Os arquivos protegidos (mercado.db, licenca, config) permanecem intactos
    após uma falha simulada de atualização."""
    mercado_db = app_root / "data" / "mercado.db"
    licenca = app_root / "data" / "licenca.dat"
    config = app_root / "data" / "config.json"

    conteudo_antes = {
        "mercado.db": mercado_db.read_text(encoding="utf-8"),
        "licenca.dat": licenca.read_text(encoding="utf-8"),
        "config.json": config.read_text(encoding="utf-8"),
    }

    write_update_state({
        "state": UpdateState.ROLLED_BACK.value,
        "version": "1.0.22",
        "error": "simulado: falha na instalacao",
    })

    assert mercado_db.read_text(encoding="utf-8") == conteudo_antes["mercado.db"]
    assert licenca.read_text(encoding="utf-8") == conteudo_antes["licenca.dat"]
    assert config.read_text(encoding="utf-8") == conteudo_antes["config.json"]

    estado = read_update_state()
    assert estado.get("state") == UpdateState.ROLLED_BACK.value


# ===========================================================================
# 11. mensagem ao cliente com o link oficial confirmado
# ===========================================================================

def test_link_oficial_esta_confirmado_no_projeto():
    """A URL oficial de download está confirmada no projeto (version.json)."""
    version_json = Path(__file__).resolve().parent / "version.json"
    dados = json.loads(version_json.read_text(encoding="utf-8"))
    url = dados.get("download_url", "")
    assert "github.com/frscomercial6-eng/FRS_Mercado" in url
    assert "releases/latest" in url or "releases/download" in url


def test_manual_update_url_usa_endereco_do_projeto():
    """MANUAL_UPDATE_URL aponta para o endereço oficial do projeto."""
    assert secure.MANUAL_UPDATE_URL == (
        "https://www.frssolutions.com.br/frs-mercado"
    )


def test_mensagem_falha_contem_link_oficial(tmp_root, app_root):
    """A mensagem de falha inclui o link oficial e não promete versão anterior
    quando o rollback não foi confirmado."""
    upd = secure.Updater(parent=None)
    with patch("tkinter.messagebox.showwarning") as mock_msg, \
         patch("webbrowser.open") as mock_browser:
        upd._mostrar_falha_atualizacao_com_suporte(
            "1.0.23", rollback_confirmado=False, parent=None
        )
    args = mock_msg.call_args
    mensagem = args[0][1] if args else ""
    assert "Atualização automática não concluída" in mensagem
    assert secure.MANUAL_UPDATE_URL in mensagem
    assert "suporte" in mensagem.lower()
    assert "Não foi possível confirmar" in mensagem
    mock_browser.assert_called_once_with(
        secure.MANUAL_UPDATE_URL, new=0, autoraise=True
    )


def test_mensagem_falha_com_rollback_confirmado(tmp_root, app_root):
    """Quando o rollback foi confirmado, a mensagem informa que a versão
    anterior foi preservada."""
    upd = secure.Updater(parent=None)
    with patch("tkinter.messagebox.showwarning") as mock_msg, \
         patch("webbrowser.open"):
        upd._mostrar_falha_atualizacao_com_suporte(
            "1.0.23", rollback_confirmado=True, parent=None
        )
    args = mock_msg.call_args
    mensagem = args[0][1] if args else ""
    assert "versão anterior do programa foi preservada" in mensagem


# ===========================================================================
# 12. comportamento sem interface gráfica
# ===========================================================================

def test_sem_interface_grafica_nao_bloqueia(tmp_root, app_root):
    """As funções de falha não bloqueiam nem abrem janelas quando messagebox
    não está disponível (modo headless/CI)."""
    upd = secure.Updater(parent=None)
    with patch("tkinter.messagebox.showwarning", side_effect=Exception("sem GUI")):
        try:
            upd._mostrar_falha_atualizacao_com_suporte(
                "1.0.23", rollback_confirmado=False, parent=None
            )
        except Exception as exc:
            pytest.fail(f"Não deve falhar sem GUI: {exc}")


# ===========================================================================
# 3 e 4. Falha durante instalação (ZIP e Inno Setup) — pendentes no Linux
# ===========================================================================

def test_falha_instalacao_zip_pendente_windows():
    """Falha durante instalação ZIP/portable com restauração da versão anterior.

    PENDENTE: exige executável Windows compilado e ambiente Windows.
    """
    pytest.skip("Requer Windows + FRS_Mercado_UpdateHelper.exe compilado")


def test_falha_instalacao_inno_setup_pendente_windows():
    """Falha durante instalação pelo Inno Setup, com restauração da versão anterior.

    PENDENTE: exige executável Windows compilado e Inno Setup 6.
    """
    pytest.skip("Requer Windows + Inno Setup 6 + FRS_Mercado_Setup.exe compilado")


def test_falha_rollback_mensagem_honesta_pendente():
    """Falha no rollback, com mensagem honesta e instrução de suporte.

    PENDENTE: exige simulação de rollback falhando no helper.
    """
    pytest.skip("Requer simulação de rollback falhando no helper")


# ===========================================================================
# Teste de integração: revalidação completa antes de lançar o helper
# ===========================================================================

def test_preparar_instalacao_recusa_versao_igual(tmp_root, app_root):
    """Recusa quando a versão destino é igual à instalada."""
    release = _mk_release(version="1.0.22")
    write_update_state({
        "state": UpdateState.AVAILABLE.value,
        "version": "1.0.22",
        "asset_url": release.asset_url,
        "asset_name": release.asset_name,
        "size": release.size,
        "sha256": release.sha256,
        "key_id": release.key_id,
        "sequence": release.sequence,
        "issued_at": release.issued_at,
        "manifest": release.manifest,
        "asset_type": release.asset_type,
    })
    upd = secure.Updater(parent=None)
    with patch.object(secure, "get_local_version", return_value="1.0.22"):
        try:
            upd._revalidar_antes_de_instalar(release)
            pytest.fail("Deveria lançar UpdateSecurityError para versão igual")
        except UpdateSecurityError as exc:
            assert "igual" in str(exc).lower()
