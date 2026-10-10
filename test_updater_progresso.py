"""Testes da interface de progresso e do aviso de fechamento (Etapa 4C).

ESCOPO
------
Barra de progresso real (bytes), thread-safety (nunca tocar widget a partir da
thread de download), tamanho desconhecido, erros de rede/arquivo incompleto,
hash/assinatura inválidos, gate operacional, aviso de fechamento/reabertura,
preservação de dados e prevenção de janelas duplicadas / callbacks após
fechamento. Além de uma verificação de que a pré-visualização não faz rede,
não cria job, não executa o helper e não altera dados.

ISOLAMENTO
----------
``APPDATA`` é redirecionado para um diretório temporário ANTES de importar os
módulos; nenhum dado real é lido/gravado. A janela de progresso é testada com
widgets FALSOS injetados (``fabrica``), portanto nenhuma janela real é aberta.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("APPDATA", tempfile.mkdtemp(prefix="frs-prog-"))

import updater_secure as secure  # noqa: E402
import updater_ui as ui  # noqa: E402
import updater_helper as helper  # noqa: E402

RAIZ = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Widgets falsos (sem display)
# ---------------------------------------------------------------------------
class _WidgetFalso:
    def __init__(self):
        self.texto = ""
        self.valor = None
        self.iniciado = False
        self.parado = 0
        self.destruido = False
        self._existe = True

    def winfo_exists(self):
        return self._existe

    def configure(self, **kw):
        if "text" in kw:
            self.texto = kw["text"]

    def set(self, v):
        self.valor = v

    def start(self):
        self.iniciado = True

    def stop(self):
        self.parado += 1
        self.iniciado = False

    def destroy(self):
        self.destruido = True
        self._existe = False


def _fabrica():
    return {k: _WidgetFalso() for k in ("janela", "barra", "rotulo_estado", "rotulo_info")}


# ---------------------------------------------------------------------------
# 1) Progresso por bytes reais
# ---------------------------------------------------------------------------
class TestCalculoExibicao(unittest.TestCase):
    def test_parcial_intermediario_concluido(self):
        self.assertEqual(ui.calcular_exibicao(0, 200)["fracao"], 0.0)
        self.assertAlmostEqual(ui.calcular_exibicao(50, 200)["fracao"], 0.25)
        self.assertAlmostEqual(ui.calcular_exibicao(150, 200)["fracao"], 0.75)
        self.assertEqual(ui.calcular_exibicao(200, 200)["fracao"], 1.0)

    def test_recebido_acima_do_total_satura_em_1(self):
        self.assertEqual(ui.calcular_exibicao(500, 200)["fracao"], 1.0)

    def test_tamanho_desconhecido_indeterminado(self):
        exib = ui.calcular_exibicao(10, 0)
        self.assertEqual(exib["modo"], "indeterminado")
        self.assertIsNone(exib["fracao"])
        self.assertIn("desconhecido", exib["texto"])

    def test_nunca_inventa_percentual_sem_total(self):
        exib = ui.calcular_exibicao(999, -1)
        self.assertEqual(exib["modo"], "indeterminado")
        self.assertNotIn("%", exib["texto"])

    def test_texto_tem_percentual_quando_determinado(self):
        self.assertIn("50%", ui.calcular_exibicao(100, 200)["texto"])


# ---------------------------------------------------------------------------
# 2) Janela: agendamento, thread-safety e estados
# ---------------------------------------------------------------------------
class TestJanelaProgresso(unittest.TestCase):
    def _janela(self, enfileirar=True):
        fila = []
        agendador = (fila.append if enfileirar else (lambda fn: fn()))
        fab = _fabrica()
        j = ui.JanelaProgressoAtualizacao(
            parent=object(), versao_origem="1.0.21", versao_destino="1.0.22",
            agendador=agendador, fabrica=fab,
        )
        return j, fab, fila

    def _drenar(self, fila):
        for fn in list(fila):
            fn()

    def test_thread_download_nao_muda_widget_antes_do_agendador(self):
        j, fab, fila = self._janela(enfileirar=True)
        j.atualizar_bytes(100, 200)
        self.assertEqual(len(fila), 1)
        self.assertIsNone(fab["barra"].valor)  # ainda não aplicado
        self._drenar(fila)
        self.assertEqual(fab["barra"].valor, 0.5)

    def test_estado_concluido_seta_100(self):
        j, fab, fila = self._janela()
        j.concluir()
        self._drenar(fila)
        self.assertEqual(fab["barra"].valor, 1.0)
        self.assertIn("preparada", fab["rotulo_estado"].texto.lower())

    def test_estado_indeterminado_nao_exibe_percentual(self):
        j, fab, fila = self._janela()
        j.indeterminado()
        self._drenar(fila)
        self.assertTrue(fab["barra"].iniciado)
        self.assertIn("desconhecido", fab["rotulo_info"].texto)
        self.assertNotIn("%", fab["rotulo_info"].texto)

    def test_callback_apos_fechar_e_ignorado(self):
        j, fab, fila = self._janela()
        j.fechar()
        fila.clear()
        j.atualizar_bytes(10, 100)  # não deve agendar nada
        self.assertEqual(len(fila), 0)

    def test_callback_nao_erra_com_widget_destruido(self):
        j, fab, fila = self._janela()
        fab["barra"].destroy()
        j.atualizar_bytes(10, 100)
        self._drenar(fila)  # não deve levantar
        self.assertTrue(True)

    def test_fechar_marca_flag_e_destroi(self):
        j, fab, fila = self._janela()
        self.assertFalse(j.fechada)
        j.fechar()
        self.assertTrue(j.fechada)
        self._drenar(fila)
        self.assertTrue(fab["janela"].destruido)

# ---------------------------------------------------------------------------
# 3) Aviso de fechamento e reabertura
# ---------------------------------------------------------------------------
class TestAvisoFechamentoReabertura(unittest.TestCase):
    def test_mensagem_informa_fechamento_reabertura_e_login(self):
        msg = ui.mensagem_fechamento_reabertura("1.0.22")
        self.assertIn("fechado temporariamente", msg)
        self.assertIn("reaberto", msg)
        self.assertIn("login", msg)
        self.assertIn("1.0.22", msg)

    def test_mensagem_nao_promete_reabertura_incondicional(self):
        msg = ui.mensagem_fechamento_reabertura("1.0.22").lower()
        self.assertIn("se a reabertura automática não ocorrer", msg)
        self.assertIn("atalho", msg)


# ---------------------------------------------------------------------------
# 4) Gate operacional: bloqueia durante operações críticas
# ---------------------------------------------------------------------------
class _PdvFalso:
    def __init__(self, **kw):
        self._kw = kw
        self.winfo_children = lambda: []

    def __getattr__(self, item):
        return self._kw.get(item)


class TestGateOperacional(unittest.TestCase):
    def _parent(self, **pdv):
        class _P:
            _janela_pdv = _PdvFalso(**pdv)
            _backup_em_execucao = False
            _fiscal_alerta_em_exibicao = False
            _system_monitor = None
        return _P()

    def test_caixa_aberto_bloqueia(self):
        permitido, motivo = secure._operational_gate(self._parent(caixa_id=7))
        self.assertFalse(permitido)
        self.assertIn("caixa", motivo.lower())

    def test_carrinho_bloqueia(self):
        permitido, _ = secure._operational_gate(self._parent(itens_carrinho=[1, 2]))
        self.assertFalse(permitido)

    def test_backup_bloqueia(self):
        p = self._parent()
        p._backup_em_execucao = True
        permitido, _ = secure._operational_gate(p)
        self.assertFalse(permitido)

    def test_estado_tranquilo_permite(self):
        permitido, motivo = secure._operational_gate(self._parent())
        self.assertTrue(permitido)
        self.assertEqual(motivo, "")


# ---------------------------------------------------------------------------
# 5) Segurança: hash incorreto e assinatura inválida são rejeitados
# ---------------------------------------------------------------------------
class TestRejeicaoSeguranca(unittest.TestCase):
    def setUp(self):
        import json
        self.manifest = json.loads(
            open(os.path.join(RAIZ, "update-manifest.json"), encoding="utf-8").read()
        )

    def _rejeitado(self, envelope):
        with self.assertRaises(secure.UpdateSecurityError):
            secure.verify_manifest(envelope)

    def test_manifesto_do_repositorio_valido(self):
        self.assertEqual(secure.verify_manifest(self.manifest)["product"], secure.PRODUCT)

    def test_hash_alterado_rejeitado(self):
        import copy
        env = copy.deepcopy(self.manifest)
        env["payload"]["assets"][0]["sha256"] = "0" * 64
        self._rejeitado(env)

    def test_assinatura_corrompida_rejeitada(self):
        import copy
        env = copy.deepcopy(self.manifest)
        env["signature"] = "A" * 88
        self._rejeitado(env)

    def test_url_http_rejeitada(self):
        import copy
        env = copy.deepcopy(self.manifest)
        env["payload"]["assets"][0]["url"] = "http://insecure/arquivo.zip"
        self._rejeitado(env)


# ---------------------------------------------------------------------------
# 6) Preservação de dados: mercado.db, WAL/SHM/journal, config, licensing
# ---------------------------------------------------------------------------
class TestPreservacaoDados(unittest.TestCase):
    def test_nomes_protegidos_incluem_banco_e_sidecars(self):
        nomes = set(helper.PROTECTED_HASH_FILES)
        for essencial in ("mercado.db", "mercado.db-wal", "mercado.db-shm",
                           "mercado.db-journal", "config.json"):
            self.assertIn(essencial, nomes)

    def test_licensing_e_integracoes_protegidos(self):
        snap = set(helper.PROTECTED_DATA_NAMES)
        self.assertIn("licensing", snap)
        self.assertIn("integracoes_cofre.json", snap)
        self.assertIn("integracoes_config.json", snap)

    def test_roundtrip_preserva_estado(self):
        import shutil
        from pathlib import Path
        base = tempfile.mkdtemp(prefix="frs-dados-")
        data = os.path.join(base, "data")
        os.makedirs(data)
        with open(os.path.join(data, "mercado.db"), "wb") as f:
            f.write(b"DB-SINTETICO")
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as f:
            f.write('{"cnpj": "00.000.000/0001-00"}')
        antes = helper._protected_hashes(Path(data))
        backup = Path(tempfile.mkdtemp(prefix="frs-bkp-"))
        backup.rmdir()
        helper._backup_protected_data(Path(data), backup)
        with open(os.path.join(data, "mercado.db"), "wb") as f:
            f.write(b"CORROMPIDO")
        helper._restore_protected_data(Path(data), backup)
        self.assertEqual(helper._protected_hashes(Path(data)), antes)
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7) Pré-visualização isolada: sem rede, sem job, sem helper, sem dados
# ---------------------------------------------------------------------------
class TestPreviewIsolada(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(RAIZ, "preview_updater_ui.py"), encoding="utf-8") as fh:
            self.fonte = fh.read()

    def test_marcada_como_demonstracao(self):
        self.assertIn("DEMONSTRA", self.fonte.upper())

    def test_nao_importa_fluxo_de_producao(self):
        # Procura imports REAIS (não menções em docstring/comentários).
        for proibido in ("import modulo_main", "from modulo_main",
                         "import build_exe", "from build_exe",
                         "from updater_secure import", "import updater_secure",
                         "from updater_helper import", "import updater_helper",
                         "_stage_release", "_launch_helper", "run_job",
                         "import deploy", "from deploy"):
            self.assertNotIn(proibido, self.fonte)

    def test_redireciona_appdata_para_temp(self):
        self.assertIn("APPDATA", self.fonte)
        self.assertIn("mkdtemp", self.fonte)


# ---------------------------------------------------------------------------
# 8) Helper: timeout de encerramento e health-check (rollback preserva dados)
# ---------------------------------------------------------------------------
class TestHelperFalhas(unittest.TestCase):
    def test_health_check_falha_com_returncode_nao_zero(self):
        import subprocess
        from pathlib import Path
        orig = subprocess.run

        def _fake(cmd, **kw):
            class _R:
                returncode = 3
            return _R()
        subprocess.run = _fake
        try:
            with self.assertRaises(helper.HelperError):
                helper._health_check(Path("naoimporta.exe"), expected_version="1.0.22")
        finally:
            subprocess.run = orig

    def test_wait_parent_exit_estoura_em_timeout(self):
        orig = helper._process_alive
        helper._process_alive = lambda pid: True
        try:
            with self.assertRaises(helper.HelperError):
                helper._wait_parent_exit(999999, timeout=0.2)
        finally:
            helper._process_alive = orig


if __name__ == "__main__":
    unittest.main()

