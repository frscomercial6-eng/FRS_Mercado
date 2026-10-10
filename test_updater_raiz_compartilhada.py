"""Regressão da causa raiz 1.0.22: divergência de raiz de dados app/helper.

Reproduz o cenário reportado: helper em ``<D>/updates/bin``, job em
``<D>/updates/<versão>``, raiz real ``<D>``. O helper deve gravar estado, lock
e diagnóstico no MESMO ``<D>``; nada pode surgir em ``<D>/updates/bin/data``.
Roda em diretórios temporários com APPDATA isolado; nenhum banco real é tocado.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_paths
import updater_helper
import updater_secure as secure


def _reset_raiz() -> None:
    app_paths.limpar_raiz_dados_explícita()


class BaseRaiz(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="frs-rootregress-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self._old_appdata = os.environ.get("APPDATA")
        self._old_envroot = os.environ.get("FRS_MERCADO_DATA_ROOT")
        appdata = self.base / "profile" / "AppData" / "Roaming"
        appdata.mkdir(parents=True, exist_ok=True)
        os.environ["APPDATA"] = str(appdata)
        os.environ.pop("FRS_MERCADO_DATA_ROOT", None)
        self._old_state = secure.UPDATE_STATE_FILE
        self._state_file = self.base / "state" / "update_state.json"
        secure.UPDATE_STATE_FILE = self._state_file
        _reset_raiz()
        self.addCleanup(self._restaurar)

    def _restaurar(self):
        secure.UPDATE_STATE_FILE = self._old_state
        os.environ["APPDATA"] = self._old_appdata
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        if self._old_envroot is None:
            os.environ.pop("FRS_MERCADO_DATA_ROOT", None)
        else:
            os.environ["FRS_MERCADO_DATA_ROOT"] = self._old_envroot
        _reset_raiz()

    def _criar_layout_portable(self) -> Path:
        # Convenção real do Portable: FRS_Mercado.exe em <D>, dados em <D>/data,
        # helper copiado para <D>/data/updates/bin pelo _stage_release.
        raiz = self.base / "D"
        (raiz).mkdir(parents=True, exist_ok=True)
        (raiz / "data").mkdir(parents=True, exist_ok=True)
        (raiz / "data" / "updates" / "bin").mkdir(parents=True, exist_ok=True)
        (raiz / "data" / "updates" / "bin" / "FRS_Mercado_UpdateHelper.exe").write_bytes(b"MZ")
        return raiz


class TestRaizCompartilhada(BaseRaiz):
    def test_helper_em_updates_bin_usa_mesma_raiz_do_app(self):
        raiz = self._criar_layout_portable()
        with patch.object(app_paths, "e_compilado", return_value=True), \
             patch.object(app_paths, "_base_executavel", return_value=str(raiz / "updates" / "bin")), \
             patch.object(app_paths, "_versioned_portable_data_base", return_value=str(raiz / "data")):
            _reset_raiz()
            self.assertEqual(Path(app_paths.obter_caminho_dados()).resolve(),
                             (raiz / "data").resolve())
            app_paths.definir_raiz_dados_explícita(raiz / "data")
            self.assertEqual(Path(app_paths.obter_caminho_dados("update_state.json")).resolve(),
                             (raiz / "data" / "update_state.json").resolve())
            self.assertFalse((raiz / "updates" / "bin" / "data").exists())

    def test_updates_root_do_helper_confere_com_job(self):
        raiz = self._criar_layout_portable()
        app_paths.definir_raiz_dados_explícita(raiz / "data")
        secure.redefinir_caminhos_apos_raiz()
        updates_root = Path(app_paths.obter_caminho_dados("updates")).resolve()
        self.assertEqual(updates_root, (raiz / "data" / "updates").resolve())
        job = raiz / "data" / "updates" / "1.0.22" / "update-job.json"
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_text("{}", encoding="utf-8")
        self.assertEqual(job.parent.parent.resolve(), updates_root)

    def test_portable_root_prefere_raiz_explicita(self):
        raiz = self._criar_layout_portable()
        app_paths.definir_raiz_dados_explícita(raiz / "data")
        self.assertEqual(updater_helper._portable_root(), raiz)


class TestValidacaoRaizExplicita(BaseRaiz):
    def test_rejeita_relativa(self):
        with self.assertRaises(ValueError):
            app_paths.definir_raiz_dados_explícita("data/updates/bin")

    def test_rejeita_dentro_de_updates(self):
        alvo = self.base / "D" / "updates" / "bin" / "data"
        alvo.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            app_paths.definir_raiz_dados_explícita(alvo)

    def test_rejeita_arquivo(self):
        arquivo = self.base / "arquivo.txt"
        arquivo.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            app_paths.definir_raiz_dados_explícita(arquivo)

    def test_rejeita_divergente_apos_fixada(self):
        a = self.base / "a"
        b = self.base / "b"
        a.mkdir()
        b.mkdir()
        app_paths.definir_raiz_dados_explícita(a)
        with self.assertRaises(ValueError):
            app_paths.definir_raiz_dados_explícita(b)

    def test_idempotente_mesma_raiz(self):
        a = self.base / "a"
        a.mkdir()
        self.assertEqual(app_paths.definir_raiz_dados_explícita(a),
                         app_paths.definir_raiz_dados_explícita(a))

    def test_env_invalida_e_ignorada_fail_closed(self):
        os.environ["FRS_MERCADO_DATA_ROOT"] = "relative/nao/absoluta"
        self.assertIsNone(app_paths._raiz_do_ambiente())

    def test_env_valida_eh_usada(self):
        alvo = self.base / "dados"
        alvo.mkdir()
        os.environ["FRS_MERCADO_DATA_ROOT"] = str(alvo)
        self.assertEqual(app_paths._raiz_do_ambiente(), alvo.resolve())


class TestHelperRejeitaRaizInvalida(BaseRaiz):
    def test_main_rejeita_raiz_invalida_e_retorna_1(self):
        raiz = self._criar_layout_portable()
        job = raiz / "data" / "updates" / "1.0.22" / "update-job.json"
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_text("{}", encoding="utf-8")
        fantasma = raiz / "data" / "updates" / "bin" / "data"
        fantasma.mkdir(parents=True, exist_ok=True)
        codigo = updater_helper.main(["--job", str(job), "--data-root", str(fantasma)])
        self.assertEqual(codigo, 1)
        self.assertFalse((fantasma / "update_state.json").exists())

    def test_main_sem_raiz_usa_heuristica_mas_registra(self):
        raiz = self._criar_layout_portable()
        job = raiz / "data" / "updates" / "1.0.22" / "update-job.json"
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_text(json.dumps({"schema": "errado"}), encoding="utf-8")
        with patch.object(updater_helper, "run_job", return_value=0) as rj:
            codigo = updater_helper.main(["--job", str(job)])
        self.assertEqual(codigo, 0)
        rj.assert_called_once()


class TestMaquinaDeEstados(BaseRaiz):
    def _updater(self):
        return secure.Updater(parent=None, gate_provider=lambda: (True, ""))

    def _estado(self) -> str:
        return str(secure.read_update_state().get("state") or "")

    def test_sem_atualizacao_nao_deixa_checking(self):
        u = self._updater()
        with patch.object(secure, "fetch_update_manifest", return_value=None):
            self.assertIsNone(u.checar_atualizacao("owner/repo"))
        self.assertNotEqual(self._estado(), secure.UpdateState.CHECKING.value)

    def test_erro_de_rede_vai_para_failed(self):
        u = self._updater()
        with patch.object(secure, "fetch_update_manifest", side_effect=RuntimeError("rede")):
            self.assertIsNone(u.checar_atualizacao("owner/repo"))
        self.assertEqual(self._estado(), secure.UpdateState.FAILED.value)

    def test_falha_anterior_nao_impede_tentativa_futura(self):
        u = self._updater()
        with patch.object(secure, "fetch_update_manifest", side_effect=RuntimeError("rede")):
            u.checar_atualizacao("owner/repo")
        self.assertEqual(self._estado(), secure.UpdateState.FAILED.value)
        release = secure.ReleaseInfo(
            version="9.9.9", asset_name="a.zip", asset_url="http://x/a.zip",
            sequence=99, issued_at="2026-01-01T00:00:00Z", size=1,
            sha256="ab", key_id="k", manifest={})
        with patch.object(secure, "fetch_update_manifest", return_value=release), \
             patch.object(secure, "compare_versions", return_value=1), \
             patch.object(secure, "get_local_version", return_value="1.0.22"):
            self.assertIsNotNone(u.checar_atualizacao("owner/repo"))
        self.assertEqual(self._estado(), secure.UpdateState.AVAILABLE.value)

    def test_release_igual_a_local_nao_fica_checking(self):
        u = self._updater()
        release = secure.ReleaseInfo(
            version="1.0.22", asset_name="a.zip", asset_url="http://x/a.zip",
            sequence=3, issued_at="2026-01-01T00:00:00Z", size=1,
            sha256="ab", key_id="k", manifest={})
        with patch.object(secure, "fetch_update_manifest", return_value=release), \
             patch.object(secure, "compare_versions", return_value=0), \
             patch.object(secure, "get_local_version", return_value="1.0.22"):
            self.assertIsNone(u.checar_atualizacao("owner/repo"))
        self.assertNotEqual(self._estado(), secure.UpdateState.CHECKING.value)

    def test_adiamento_grava_deferred_e_permite_released_pendente(self):
        u = self._updater()
        release = secure.ReleaseInfo(
            version="2.0.0", asset_name="a.zip", asset_url="http://x/a.zip",
            sequence=5, issued_at="2026-01-01T00:00:00Z", size=1,
            sha256="ab", key_id="k", manifest={})
        secure._set_state(
            secure.UpdateState.DEFERRED, version="2.0.0",
            asset_url=release.asset_url, asset_name=release.asset_name,
            size=1, sha256="ab", key_id="k", sequence=5,
            deferred_until_ts=secure.current_ts() + 3600, error="")
        with patch.object(secure, "fetch_update_manifest", return_value=release), \
             patch.object(secure, "compare_versions", return_value=1), \
             patch.object(secure, "get_local_version", return_value="1.0.22"):
            self.assertIsNone(u.checar_atualizacao("owner/repo", considerar_adiamento=True))
        self.assertEqual(self._estado(), secure.UpdateState.DEFERRED.value)
        self.assertIsNotNone(u._release_pendente_atual())


if __name__ == "__main__":
    unittest.main(verbosity=2)
