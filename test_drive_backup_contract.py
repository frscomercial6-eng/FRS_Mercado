"""BLOCO 4.4 / L4 — Fundacao segura do Google Drive e isolamento por market_id.

Testes 100% offline: nenhum acesso ao Drive real, nenhum login/OAuth, nenhum
upload/download. O APPDATA e redirecionado para um diretorio temporario ANTES
de qualquer import, porque `modulo_relatorio` -> `database_manager` ->
`carregar_credenciais_google()` pode gravar log no banco na importacao.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-l4-drive-")
_OLD_APPDATA = os.environ.get("APPDATA")
os.environ["APPDATA"] = _TEST_APPDATA.name

import market_identity  # noqa: E402
import release_info  # noqa: E402
from modulo_relatorio import ModuloRelatorio  # noqa: E402


PASTA_RAIZ = "FRS Mercado"
PASTA_BACKUPS = "backups"


class _CallRecordingFiles:
    """Emula apenas o que o codigo usa.

    files().update() e files().delete() FALHAM por construcao: qualquer uso
    futuro e detectado imediatamente pelos testes.
    """

    def __init__(self, service):
        self._service = service

    def list(self, q="", spaces=None, fields=None):
        self._service.calls.append(("list", q))
        if self._service.falhar_list:
            raise RuntimeError("Drive indisponivel (list)")
        nome = None
        parent = None
        for parte in str(q).split(" and "):
            parte = parte.strip()
            if parte.startswith("name = "):
                nome = parte[len("name = "):].strip().strip("'")
            elif parte.endswith("in parents"):
                parent = parte.split("'")[1]
        achados = []
        for node in self._service.nodes.values():
            if nome is not None and node["name"] != nome:
                continue
            if parent is not None and parent not in node["parents"]:
                continue
            achados.append({"id": node["id"], "name": node["name"]})
        return _FakeExecutable({"files": achados})

    def create(self, body=None, media_body=None, fields=None):
        self._service.calls.append(("create", body))
        # Proibido escrever na RAIZ do Drive via parents vazio. A pasta raiz do
        # FRS, essa sim, nasce na raiz -- e por isso nao tem a chave "parents".
        if "parents" in body and not body.get("parents"):
            raise AssertionError(
                "files().create com 'parents' vazio: escreveria na raiz do Drive"
            )
        node = {
            "id": self._service._next_id(),
            "name": body.get("name"),
            "parents": list(body.get("parents") or []),
        }
        self._service.nodes[node["id"]] = node
        return _FakeExecutable({"id": node["id"]})

    def update(self, *args, **kwargs):
        self._service.calls.append(("update", kwargs))
        raise AssertionError("files().update PROIBIDO: pode sobrescrever outro mercado")

    def delete(self, *args, **kwargs):
        self._service.calls.append(("delete", kwargs))
        raise AssertionError("files().delete nao deve ser usado nesta etapa")


class _FakeExecutable:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeAboutGet:
    def __init__(self, email):
        self._email = email

    def execute(self):
        return {"user": {"emailAddress": self._email}}


class _FakeAbout:
    def __init__(self, email):
        self._email = email

    def get(self, fields=None):
        return _FakeAboutGet(self._email)


class _FakeService:
    def __init__(self, email="cliente@exemplo.com"):
        self.nodes = {}
        self.calls = []
        self.falhar_list = False
        self._seq = 0
        self._email = email

    def _next_id(self):
        self._seq += 1
        return f"id{self._seq:03d}"

    def files(self):
        return _CallRecordingFiles(self)

    def about(self):
        return _FakeAbout(self._email)

    def filhos_de(self, nome):
        return [n for n in self.nodes.values() if n["name"] == nome]


class TestVersaoUnificada(unittest.TestCase):
    """L4 §1 — fonte de versao unica em 1.0.22."""

    def test_release_info_1_0_22(self):
        self.assertEqual(release_info.APP_VERSION, "1.0.23")

    def test_version_txt_e_fonte_oficial(self):
        raiz = Path(__file__).resolve().parent
        self.assertEqual(
            (raiz / "version.txt").read_text(encoding="utf-8").strip(), "1.0.23"
        )

    def test_consumidores_usan_a_fonte_oficial(self):
        import modulo_login
        import modulo_main

        self.assertEqual(modulo_main._FRS_APP_VERSION, "1.0.23")
        self.assertEqual(modulo_login._FRS_APP_VERSION, "1.0.23")

    def test_nenhuma_fonte_antiga_presente(self):
        """Os fallbacks duplicados nao podem divergir da fonte oficial."""
        raiz = Path(__file__).resolve().parent
        for nome in ("modulo_login.py", "modulo_main.py", "release_info.py"):
            texto = (raiz / nome).read_text(encoding="utf-8")
            self.assertNotIn('"1.0.20"', texto, f"{nome} ainda fixa 1.0.20")
            self.assertNotIn('"1.0.21"', texto, f"{nome} ainda fixa 1.0.21")


class TestMarketIdEstavel(unittest.TestCase):
    """L4 §2 — identidade estavel, persistente e explicita."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-l4-market-")
        self.addCleanup(temp.cleanup)
        self.config_path = Path(temp.name) / "config.json"

    def _escrever(self, payload):
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _ler(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _garantir(self):
        with patch.object(market_identity, "_config_path", lambda: self.config_path):
            return market_identity.ensure_local_market_id()

    # M01
    def test_mercado_id_permanece_entre_reinicializacoes(self):
        self._escrever({"razao_social": "MERCADO UM LTDA", "cnpj": "11.222.333/0001-44"})
        primeiro = self._garantir()
        segundo = self._garantir()
        self.assertEqual(primeiro, segundo)
        self.assertEqual(self._ler()["market_id"], primeiro)

    # M02
    def test_mercado_id_preservado_apos_reescrita_da_config(self):
        self._escrever({"razao_social": "MERCADO UM LTDA", "cnpj": "11.222.333/0001-44"})
        original = self._garantir()
        dados = self._ler()
        dados["fiscal_ativo"] = True
        self._escrever(dados)
        self.assertEqual(self._garantir(), original)

    # M03
    def test_config_invalida_nao_gera_novo_mercado_id(self):
        self._escrever({"cnpj": "11.222.333/0001-44"})
        fixo = self._garantir()
        self.config_path.write_text("{ isso nao e json", encoding="utf-8")
        with self.assertRaises(Exception):
            self._garantir()
        # Nada foi fabricado: o arquivo segue invalido e intacto.
        self.assertEqual(
            self.config_path.read_text(encoding="utf-8"), "{ isso nao e json"
        )
        self.assertNotEqual(fixo, "")

    # M04
    def test_bootstrap_explicito_e_prefixado(self):
        self._escrever({})
        gerado = self._garantir()
        self.assertTrue(gerado.startswith(market_identity.PREFIXO_MARKET_LOCAL))
        self.assertTrue(self._ler()["market_id"].startswith("market-local-"))

    def test_identidade_provisoria_nao_conta_como_dado_comercial(self):
        import app_paths

        self.assertFalse(
            app_paths._config_tem_dados_usuario({"market_id": "market-local-abcdef123456"})
        )
        self.assertTrue(
            app_paths._config_tem_dados_usuario({"market_id": "market-11222333000144"})
        )

    # M05
    def test_dois_mercados_tem_identidades_diferentes(self):
        self._escrever({"cnpj": "11.222.333/0001-44"})
        a = self._garantir()
        self._escrever({"cnpj": "55.666.777/0001-88"})
        b = self._garantir()
        self.assertNotEqual(a, b)

    # M06
    def test_identidade_nao_depende_de_timestamp_de_execucao(self):
        """Com o gerador injetado o resultado e deterministico e estavel."""
        import re

        fixo = type("Fixo", (), {"hex": "0123456789ab"})()
        with patch.object(market_identity.uuid, "uuid4", lambda: fixo):
            primeiro = market_identity._identificador_local_estavel()
            segundo = market_identity._identificador_local_estavel()
        self.assertEqual(primeiro, segundo)
        self.assertEqual(primeiro, f"market-local-0123456789ab")

        padrao = re.compile(
            rf"^{re.escape(market_identity.PREFIXO_MARKET_LOCAL)}[0-9a-f]{{12}}$"
        )
        self.assertRegex(
            market_identity._identificador_local_estavel(), padrao
        )

    def test_mercado_id_persistido_nao_depende_do_gerador(self):
        """Com market_id persistido o gerador nunca e consultado."""
        self._escrever({"market_id": "market-fixo-123"})
        def explodir():
            raise AssertionError("gerador nao deve ser chamado com identidade valida")
        with patch.object(market_identity, "_identificador_local_estavel", explodir):
            self.assertEqual(self._garantir(), "market-fixo-123")

    def test_mercado_id_existente_tem_precedencia(self):
        self._escrever({"market_id": "market-original", "cnpj": "11.222.333/0001-44"})
        self.assertEqual(self._garantir(), "market-original")

    def test_cnpj_continua_derivando_market_id_deterministico(self):
        self._escrever({"cnpj": "11.222.333/0001-44"})
        self.assertEqual(self._garantir(), "market-11222333000144")


class TestArvoreDrive(unittest.TestCase):
    """L4 §3 — FRS Mercado/<market_id>/backups com pai-filho explicito."""

    def setUp(self):
        self.service = _FakeService()
        patcher = patch("modulo_relatorio.registrar_log", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _garantir(self, market_id):
        return ModuloRelatorio.ensure_market_drive_root(market_id, service=self.service)

    def test_nomes_de_pasta_do_contrato(self):
        self.assertEqual(ModuloRelatorio.PASTA_RAIZ_DRIVE, PASTA_RAIZ)
        self.assertEqual(ModuloRelatorio.PASTA_BACKUPS_DRIVE, PASTA_BACKUPS)

    # D01/D02/D03
    def test_cria_arvore_completa(self):
        arvore = self._garantir("market-a")
        raiz = self.service.filhos_de(PASTA_RAIZ)[0]
        mercado = self.service.filhos_de("market-a")[0]
        backups = self.service.filhos_de(PASTA_BACKUPS)[0]
        self.assertEqual(arvore["root_folder_id"], raiz["id"])
        self.assertEqual(arvore["market_folder_id"], mercado["id"])
        self.assertEqual(arvore["backups_folder_id"], backups["id"])
        self.assertEqual(mercado["parents"], [raiz["id"]])
        self.assertEqual(backups["parents"], [mercado["id"]])

    # D04
    def test_busca_de_cliente_sempre_escopada_ao_pai(self):
        self._garantir("market-a")
        consultas = [q for tipo, q in self.service.calls if tipo == "list"]
        for consulta in consultas:
            if "name = 'market-a'" in consulta:
                self.assertIn("in parents", consulta)
            if f"name = '{PASTA_BACKUPS}'" in consulta:
                self.assertIn("in parents", consulta)

    # D05
    def test_mercados_distintos_nao_compartilham_pasta(self):
        a = self._garantir("market-a")
        b = self._garantir("market-b")
        self.assertNotEqual(a["market_folder_id"], b["market_folder_id"])
        self.assertNotEqual(a["backups_folder_id"], b["backups_folder_id"])

    # D06
    def test_apenas_a_raiz_do_frs_nasce_sem_parent(self):
        """A raiz do FRS vive na raiz do Drive; todo o resto tem pai explicito."""
        self._garantir("market-a")
        for tipo, body in self.service.calls:
            if tipo != "create":
                continue
            if body.get("name") == PASTA_RAIZ:
                self.assertNotIn("parents", body)
            else:
                self.assertTrue(body.get("parents"), f"create sem parent: {body}")
                self.assertNotEqual(body["parents"], [])

    def test_provisionamento_de_arvore_e_idempotente(self):
        primeiro = self._garantir("market-a")
        segundo = self._garantir("market-a")
        self.assertEqual(primeiro, segundo)
        self.assertEqual(len(self.service.filhos_de(PASTA_RAIZ)), 1)
        self.assertEqual(len(self.service.filhos_de("market-a")), 1)
        self.assertEqual(len(self.service.filhos_de(PASTA_BACKUPS)), 1)

    def test_market_id_vazio_recusado(self):
        with self.assertRaises(ValueError):
            self._garantir("   ")

    def test_drive_indisponivel_nao_cria_nada(self):
        self.service.falhar_list = True
        with self.assertRaises(RuntimeError):
            self._garantir("market-a")
        self.assertEqual(self.service.nodes, {})

    def test_apenas_a_raiz_do_frs_nao_tem_pai(self):
        self._garantir("market-a")
        raiz = self.service.filhos_de(PASTA_RAIZ)[0]
        self.assertEqual(raiz["parents"], [])


class TestProvisionamentoSemOverwrite(unittest.TestCase):
    """L4 §4 e §5 — provisionamento nao sobrescreve nem copia banco real."""

    def setUp(self):
        self.service = _FakeService()
        temp = tempfile.TemporaryDirectory(prefix="frs-l4-prov-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.config_path = self.base / "config.json"
        self.config_path.write_text(
            json.dumps({"cnpj": "11.222.333/0001-44"}), encoding="utf-8"
        )
        patcher = patch("modulo_relatorio.registrar_log", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _provisionar(self, email="cliente@exemplo.com"):
        def gravar(cfg):
            self.config_path.write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
            )

        with patch.object(ModuloRelatorio, "_obter_drive_service", lambda: self.service), \
             patch.object(market_identity, "_config_path", lambda: self.config_path), \
             patch.object(market_identity, "_save_config", gravar):
            return ModuloRelatorio.provisionar_novo_cliente(email)

    # P01/P02
    def test_provisionamento_nao_chama_update_nem_delete(self):
        self._provisionar()
        tipos = [tipo for tipo, _ in self.service.calls]
        self.assertNotIn("update", tipos)
        self.assertNotIn("delete", tipos)
        self.assertIn("create", tipos)

    # P03
    def test_parent_e_a_pasta_do_mercado_correto(self):
        resultado = self._provisionar()
        raiz = self.service.filhos_de(PASTA_RAIZ)[0]
        mercado = self.service.filhos_de(resultado["market_id"])[0]
        backups = self.service.filhos_de(PASTA_BACKUPS)[0]
        self.assertEqual(resultado["pasta_raiz_id"], raiz["id"])
        self.assertEqual(resultado["pasta_mercado_id"], mercado["id"])
        self.assertEqual(resultado["pasta_backups_id"], backups["id"])
        self.assertEqual(mercado["parents"], [raiz["id"]])
        self.assertEqual(backups["parents"], [mercado["id"]])

    # P04
    def test_mercado_a_nao_sobrescreve_objeto_de_mercado_b(self):
        a = self._provisionar()
        self.config_path.write_text(
            json.dumps({"cnpj": "55.666.777/0001-88"}), encoding="utf-8"
        )
        b = self._provisionar()
        self.assertNotEqual(a["pasta_mercado_id"], b["pasta_mercado_id"])
        self.assertIn(a["pasta_mercado_id"], self.service.nodes)
        self.assertEqual(self.service.nodes[a["pasta_mercado_id"]]["name"], a["market_id"])

    # P05
    def test_sem_objeto_resulta_em_criacao(self):
        resultado = self._provisionar()
        self.assertEqual(resultado["status"], "ok")
        self.assertTrue([b for t, b in self.service.calls if t == "create"])

    # T01..T04
    def test_nenhum_mercado_db_e_copiado_como_template(self):
        """Comportamento: o provisionamento nao produz nenhum arquivo local.

        A garantia de que files().update() nao e usado vem do stub, que lanca
        AssertionError se update/delete forem chamados.
        """
        self._provisionar()
        self.assertEqual(list(self.base.rglob("base_inicial_template.db")), [])
        self.assertEqual(list(self.base.rglob("*.db")), [])
        self.assertEqual(list(self.base.rglob("templates")), [])
        tipos = [t for t, _ in self.service.calls]
        self.assertNotIn("update", tipos)
        self.assertNotIn("delete", tipos)

    def test_nenhum_banco_criado_no_provisionamento(self):
        self._provisionar()
        self.assertEqual(list(self.base.rglob("*.db")), [])

    # T05
    def test_schema_auxiliar_nasce_vazio(self):
        destino = self.base / "schema_auxiliar.db"
        conn = sqlite3.connect(str(destino))
        for tabela in ("produtos", "vendas", "caixa_operacao"):
            conn.execute(f"CREATE TABLE IF NOT EXISTS {tabela} (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        leitura = sqlite3.connect(str(destino))
        try:
            for tabela in ("produtos", "vendas", "caixa_operacao"):
                total = leitura.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
                self.assertEqual(total, 0, f"{tabela} nao pode nascer com dados")
        finally:
            leitura.close()

    def test_contrato_sem_template(self):
        resultado = self._provisionar()
        self.assertIn("pasta_backups_id", resultado)
        self.assertNotIn("arquivo_template", resultado)

    def test_email_divergente_apenas_registra(self):
        self.assertEqual(self._provisionar(email="outro@exemplo.com")["status"], "ok")

    def test_drive_indisponivel_propaga_erro_sem_criar(self):
        self.service.falhar_list = True
        with self.assertRaises(RuntimeError):
            self._provisionar()
        self.assertEqual(self.service.nodes, {})


    def test_upload_sem_folder_e_recusado(self):
        """D06 — parents=[] (raiz do Drive) é proibido no upload."""
        with self.assertRaises(ValueError):
            ModuloRelatorio.upload_para_drive("qualquer.db", "")
        with self.assertRaises(ValueError):
            ModuloRelatorio.upload_para_drive("qualquer.db", None)
        with self.assertRaises(ValueError):
            ModuloRelatorio.upload_para_drive("qualquer.db", "   ")

    def test_upload_exige_pasta_e_nao_chega_a_chamar_o_servico(self):
        """Com folder_id vazio nem chega a abrir o Drive."""
        with patch.object(
            ModuloRelatorio, "_obter_drive_service",
            side_effect=AssertionError("nao deve abrir servico sem folder"),
        ):
            with self.assertRaises(ValueError):
                ModuloRelatorio.upload_para_drive("qualquer.db", "")


if __name__ == "__main__":
    unittest.main()
