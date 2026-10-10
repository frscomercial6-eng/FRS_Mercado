"""Revisão por sequência sem trocar o número (FRS Mercado 1.0.22).

O atualizador distribuído compara APENAS o número de versão
(`compare_versions(release.version, get_local_version()) <= 0`), então uma
republicação da MESMA 1.0.22 seria ignorada por clientes já em 1.0.22.

A correção 1.0.22 introduz um gatilho por SEQUÊNCIA: uma revisão de mesma
versão é aceita somente quando a sequência é estritamente maior que a já
INSTALADA (`installed_sequence`, gravado só no COMMITTED pelo helper) e
sempre após `verify_manifest` (assinatura Ed25519, anti-downgrade, hash,
tamanho, URL https). Nada disso é enfraquecido.

RETROATIVIDADE (honesto): o atualizador JÁ DISTRIBUÍDO na 1.0.21 e na 1.0.22
publicada NÃO possui este código. A 1.0.21 aceita o manifesto 1.0.22 corrigido
(versão maior), mas uma instalação já em 1.0.22 com o atualizador antigo NÃO
recebe a revisão — exigiria mudar o próprio atualizador que ela não tem.

`APPDATA` é redirecionado para diretório temporário ANTES de importar o
atualizador; cada teste tem seu próprio `update_state.json`. Sem rede, sem
instalação, sem chave privada.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("APPDATA", tempfile.mkdtemp(prefix="frs-revisao-seq-"))

import updater_secure as secure  # noqa: E402

RAIZ = Path(__file__).resolve().parent
MANIFESTO_1021 = json.loads((RAIZ / "update-manifest.json").read_text(encoding="utf-8"))


def _release(version: str, sequence: int, **extra) -> secure.ReleaseInfo:
    base = dict(
        version=version,
        asset_name=f"FRS_Mercado_Portable_{version}.zip",
        asset_url=f"https://exemplo.invalido/FRS_Mercado_Portable_{version}.zip",
        auto_update=True,
        product=secure.PRODUCT,
        channel=secure.DEFAULT_CHANNEL,
        sequence=sequence,
        issued_at="2026-10-10T00:00:00+00:00",
        size=1024,
        sha256="a" * 64,
        key_id=MANIFESTO_1021["key_id"],
        asset_type="portable_zip",
        manifest=None,
    )
    base.update(extra)
    return secure.ReleaseInfo(**base)


class _BaseSequencia(unittest.TestCase):
    """Estado isolado por teste + versão local controlada."""

    local = "1.0.21"

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="frs-revisao-seq-estado-")
        self._orig_state = secure.UPDATE_STATE_FILE
        secure.UPDATE_STATE_FILE = Path(self._dir) / "update_state.json"
        self._orig_local = secure.get_local_version
        secure.get_local_version = lambda: self.local

    def tearDown(self):
        secure.UPDATE_STATE_FILE = self._orig_state
        secure.get_local_version = self._orig_local

    def _checar(self, release):
        updater = secure.Updater.__new__(secure.Updater)
        updater.parent = None
        updater.last_error = ""
        updater._release_pendente = None
        with patch.object(secure, "fetch_update_manifest", return_value=release):
            return updater.checar_atualizacao("owner/repo")

    def _instalada(self, sequence: int) -> None:
        secure.write_update_state({
            "state": secure.UpdateState.COMMITTED.value,
            "version": self.local,
            "installed_sequence": int(sequence),
        })


# [1] 1.0.21 -> 1.0.22 (só número, como sempre funcionou)
# ---------------------------------------------------------------------------
class TestDe21Para22(_BaseSequencia):
    local = "1.0.21"

    def test_versao_maior_e_oferecida_mesmo_sem_installed_sequence(self):
        self._instalada(0)
        self.assertIsNotNone(self._checar(_release("1.0.22", 3)))

    def test_1021_nao_possui_logica_de_sequencia(self):
        fonte_1021 = subprocess.run(
            ["git", "-C", str(RAIZ), "show", "v1.0.21:updater_secure.py"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("compare_versions(release.version, get_local_version()) <= 0",
                      fonte_1021)
        self.assertNotIn("installed_sequence", fonte_1021)


# [2] 1.0.22 anterior -> revisão corrigida (mesma versão, sequence maior)
# ---------------------------------------------------------------------------
class TestRevisaoMesmaVersao(_BaseSequencia):
    local = "1.0.22"

    def test_sequencia_maior_que_a_instalada_e_aceita(self):
        self._instalada(3)           # já instalou a 1.0.22/seq 3 (publicada)
        self.assertIsNotNone(self._checar(_release("1.0.22", 4)))

    def test_sequencia_igual_e_recusada(self):
        self._instalada(4)
        self.assertIsNone(self._checar(_release("1.0.22", 4)))

    def test_sequencia_menor_e_recusada(self):
        self._instalada(5)
        self.assertIsNone(self._checar(_release("1.0.22", 4)))

    def test_sem_installed_sequence_previo_nao_repete_indevidamente(self):
        # Primeira execução do código novo: installed_sequence ausente == 0;
        # uma revisão com sequence 0 não pode virar atualização repetida.
        secure.write_update_state({})
        self.assertIsNone(self._checar(_release("1.0.22", 0)))


# [3] Cliente já atualizado -> nenhuma atualização repetida indevidamente
# ---------------------------------------------------------------------------
class TestJaAtualizado(_BaseSequencia):
    local = "1.0.22"

    def test_ja_na_revisao_instalada_nao_reofere(self):
        self._instalada(4)
        self.assertIsNone(self._checar(_release("1.0.22", 4)))

    def test_versao_local_maior_que_a_do_manifesto_e_recusada(self):
        self.local = "1.0.23"
        self._instalada(9)
        self.assertIsNone(self._checar(_release("1.0.22", 99)))


# [4] Recusas de segurança (manifesto/assinatura/versão inferior)
# ---------------------------------------------------------------------------
class TestRecusasDeSeguranca(_BaseSequencia):
    local = "1.0.22"

    def test_manifesto_invalido_falha_na_checagem_sem_update(self):
        self._instalada(0)
        updater = secure.Updater.__new__(secure.Updater)
        updater.parent = None
        updater.last_error = ""
        updater._release_pendente = None
        with patch.object(secure, "fetch_update_manifest",
                          side_effect=secure.UpdateSecurityError("Schema invalido.")):
            self.assertIsNone(updater.checar_atualizacao("owner/repo"))
        self.assertEqual(str(updater.last_error), "Schema invalido.")
        self.assertEqual(secure.read_update_state().get("state"),
                         secure.UpdateState.FAILED.value)

    def test_assinatura_do_repositorio_valida_e_exigida(self):
        # O contrato criptográfico real permanece intacto.
        self.assertTrue(secure.verify_manifest(MANIFESTO_1021))

    def test_manifesto_publicado_e_1022_sequence_3(self):
        # O manifesto publicado no repositório é a 1.0.22/sequence 3 (assinada);
        # a progressão desde a última release é 1.0.21/seq 2 -> 1.0.22/seq 3.
        self.assertEqual(MANIFESTO_1021["payload"]["version"], "1.0.22")
        self.assertEqual(int(MANIFESTO_1021["payload"]["sequence"]), 3)
        self.assertEqual(int(MANIFESTO_1021["payload"]["sequence"]), 2 + 1)


    def test_versao_inferior_nunca_e_oferecida(self):
        self.local = "1.0.22"
        self._instalada(0)
        self.assertIsNone(self._checar(_release("1.0.21", 2)))


if __name__ == "__main__":
    unittest.main()

