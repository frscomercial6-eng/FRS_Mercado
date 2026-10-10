"""Fluxo ponta a ponta ISOLADO (seção 2 do fechamento 1.0.22).

Valida em diretórios temporários (nenhum dado real tocado):
  1. helper e aplicativo usam a MESMA raiz real;
  2. nenhum estado/log/job/temp é gravado em raiz fantasma;
  3. CHECKING não fica abandonado;
  4. falha é registrada na raiz correta e permite tentativa futura;
  5. instalacao/reabertura/rollback em ambiente isolado.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ISOLADO = Path(tempfile.mkdtemp(prefix="frs-e2e-"))
os.environ["APPDATA"] = str(_ISOLADO / "appdata")
os.environ["USERPROFILE"] = str(_ISOLADO / "userprofile")
os.environ["HOME"] = str(_ISOLADO / "home")
(Path(os.environ["APPDATA"])).mkdir(parents=True, exist_ok=True)

import app_paths  # noqa: E402
import updater_helper as helper  # noqa: E402
import updater_secure as secure  # noqa: E402


class TestFluxoIsolado(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="frs-e2e-case-", dir=str(_ISOLADO)))
        self.raiz = self.base / "D" / "data"
        (self.raiz).mkdir(parents=True, exist_ok=True)
        app_paths.definir_raiz_dados_explícita(self.raiz)
        secure.redefinir_caminhos_apos_raiz()

    def tearDown(self):
        app_paths.limpar_raiz_dados_explícita()
        secure.redefinir_caminhos_apos_raiz()

    def test_helper_e_app_compartilham_raiz_sem_fantasma(self):
        job = self.raiz / "updates" / "9.9.9" / "update-job.json"
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_text(json.dumps({"schema": "errado"}), encoding="utf-8")
        with patch.object(helper, "run_job", return_value=0):
            codigo = helper.main(["--job", str(job), "--data-root", str(self.raiz)])
        self.assertEqual(codigo, 0)
        estado = Path(secure.UPDATE_STATE_FILE)
        self.assertEqual(estado.resolve(), (self.raiz / "update_state.json").resolve())
        fantasma = self.raiz / "updates" / "bin" / "data"
        self.assertFalse(fantasma.exists(), "raiz fantasma criada")
        stray = [
            p for p in self.base.rglob("update_state.json")
            if p.resolve() != estado.resolve()
        ]
        self.assertEqual(stray, [], f"estado fora da raiz real: {stray}")

    def test_sem_atualizacao_nao_deixa_checking(self):
        u = secure.Updater(parent=None, gate_provider=lambda: (True, ""))
        with patch.object(secure, "fetch_update_manifest", return_value=None):
            self.assertIsNone(u.checar_atualizacao("owner/repo"))
        self.assertNotEqual(
            str(secure.read_update_state().get("state") or ""),
            secure.UpdateState.CHECKING.value,
        )


    def test_erro_de_rede_vai_para_failed_e_permite_retry(self):
        u = secure.Updater(parent=None, gate_provider=lambda: (True, ""))
        with patch.object(secure, "fetch_update_manifest", side_effect=RuntimeError("rede")):
            u.checar_atualizacao("owner/repo")
        self.assertEqual(
            str(secure.read_update_state().get("state") or ""),
            secure.UpdateState.FAILED.value,
        )
        rel = secure.ReleaseInfo(
            version="9.9.9", asset_name="a.zip", asset_url="http://x/a.zip",
            sequence=9, issued_at="2026-01-01T00:00:00Z", size=1,
            sha256="ab", key_id="k", manifest={})
        with patch.object(secure, "fetch_update_manifest", return_value=rel), \
             patch.object(secure, "compare_versions", return_value=1), \
             patch.object(secure, "get_local_version", return_value="1.0.22"):
            self.assertIsNotNone(u.checar_atualizacao("owner/repo"))
        self.assertEqual(
            str(secure.read_update_state().get("state") or ""),
            secure.UpdateState.AVAILABLE.value,
        )

    def test_falha_registrada_na_raiz_real_e_diagnostico(self):
        u = secure.Updater(parent=None, gate_provider=lambda: (True, ""))
        with patch.object(secure, "fetch_update_manifest", side_effect=RuntimeError("rede")):
            u.checar_atualizacao("owner/repo")
        diag = self.raiz / "update_diagnostics.log"
        self.assertTrue(diag.exists(), "diagnostico nao gravado na raiz real")
        self.assertNotIn("Traceback", diag.read_text(encoding="utf-8"))

    def test_preparar_instalacao_sem_staging_recria_e_rollback(self):
        u = secure.Updater(parent=None, gate_provider=lambda: (True, ""))
        rel = secure.ReleaseInfo(
            version="9.9.9", asset_name="a.zip", asset_url="http://x/a.zip",
            sequence=9, issued_at="2026-01-01T00:00:00Z", size=1,
            sha256="ab", key_id="k", manifest={})
        secure._set_state(
            secure.UpdateState.STAGED, version=rel.version,
            asset_url=rel.asset_url, asset_name=rel.asset_name,
            size=1, sha256="ab", key_id="k", sequence=9,
            staged_path=str(self.raiz / "updates" / "9.9.9" / "perdido.zip"),
            job_path=str(self.raiz / "updates" / "9.9.9" / "perdido.json"),
            error="")
        # (a) Staging refeito, mas o asset validado e invalido => rollback
        # para FAILED (sem tocar o EXE) e libera nova tentativa futura.
        with patch.object(u, "_stage_release", return_value=True) as stage, \
             patch.object(u, "_validate_asset_content", side_effect=RuntimeError("hash divergente")), \
             patch.object(u, "_launch_helper", return_value=True) as launch, \
             patch.object(secure, "messagebox") as mb:
            mb.askyesno.return_value = True
            u._release_pendente = rel
            r = u.preparar_instalacao_no_encerramento()
        stage.assert_called_once()
        launch.assert_not_called()
        self.assertFalse(r)
        self.assertEqual(
            str(secure.read_update_state().get("state") or ""),
            secure.UpdateState.FAILED.value)

        # (b) Caminho de sucesso: staging refeito, conteudo valido => helper
        # recebe job e o estado vai para WAITING_FOR_EXIT (mesma raiz real).
        secure._set_state(
            secure.UpdateState.STAGED, version=rel.version,
            staged_path=str(self.raiz / "updates" / "9.9.9" / "perdido.zip"),
            job_path=str(self.raiz / "updates" / "9.9.9" / "perdido.json"),
            error="")
        with patch.object(u, "_stage_release", return_value=True), \
             patch.object(u, "_validate_asset_content"), \
             patch.object(u, "_launch_helper", return_value=True) as launch2, \
             patch.object(secure, "messagebox") as mb2:
            mb2.askyesno.return_value = True
            u._release_pendente = rel
            self.assertTrue(u.preparar_instalacao_no_encerramento())
        launch2.assert_called_once()
        # _launch_helper foi mockado; o estado permanece STAGED ate a chamada
        # real gravar WAITING_FOR_EXIT. O ponto e: o helper foi invocado com o
        # job da MESMA raiz real (sem raiz fantasma).
        self.assertEqual(
            str(secure.read_update_state().get("state") or ""),
            secure.UpdateState.STAGED.value)
        self.assertTrue(
            Path(secure.UPDATE_ROOT).resolve().is_relative_to(self.raiz.resolve()))


if __name__ == "__main__":
    unittest.main(verbosity=2)