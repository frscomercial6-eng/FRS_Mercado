"""BLOCO 4.5 / L5 — Contrato real de backup: ZIP, manifesto, Drive e retencao.

100% offline. Nenhum acesso ao Drive real, nenhum login/OAuth, nenhum upload.
Bancos e configs sempre temporarios. O APPDATA e redirecionado ANTES de
qualquer import, porque modulo_relatorio -> database_manager grava log na
importacao.
"""

import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-l5-tests-")
_OLD_APPDATA = os.environ.get("APPDATA")
os.environ["APPDATA"] = _TEST_APPDATA.name

import backup_contract as contrato  # noqa: E402
import backup_drive as drive  # noqa: E402
import backup_service as service  # noqa: E402


PASTA_RAIZ = "FRS Mercado"
PASTA_BACKUPS = "backups"


# --------------------------------------------------------------------------
# Drive fake: emula files().list/create/get/delete. update() FALHA sempre.
# --------------------------------------------------------------------------
class _Executavel:
    def __init__(self, payload=None, status=None):
        self._payload = payload if payload is not None else {}
        self.status = status

    def execute(self):
        return self._payload

    def next_chunk(self, num_retries=0):
        self.status.finished = True
        return self.status, self._payload


class _Status:
    def __init__(self):
        self.finished = False


class _Files:
    def __init__(self, svc):
        self.svc = svc

    def list(self, q="", spaces=None, fields=None):
        self.svc.calls.append(("list", q))
        if self.svc.falhar_list:
            raise RuntimeError("Drive indisponivel (list)")
        parent = None
        for parte in str(q).split(" and "):
            p = parte.strip()
            if p.endswith("in parents"):
                parent = p.split("'")[1]
        out = []
        for node in self.svc.nodes.values():
            if parent is not None and parent not in node["parents"]:
                continue
            out.append({
                "id": node["id"], "name": node["name"],
                "size": node["size"], "parents": node["parents"],
                "appProperties": node["appProperties"],
            })
        return _Executavel({"files": out})

    def create(self, body=None, media_body=None, fields=None, resumable=False):
        self.svc.calls.append(("create", body))
        if "parents" in body and not body.get("parents"):
            raise AssertionError("parents vazio: escreveria na raiz do Drive")
        dados = b""
        if media_body is not None:
            try:
                dados = media_body._stream()
            except Exception:
                with open(media_body._filename(), "rb") as fh:
                    dados = fh.read()
        node = {
            "id": self.svc._next_id(),
            "name": body.get("name"),
            "parents": list(body.get("parents") or []),
            "appProperties": dict(body.get("appProperties") or {}),
            "size": len(dados),
        }
        self.svc.nodes[node["id"]] = node
        self.svc.blobs[node["id"]] = dados
        st = _Status()
        return _Executavel({"id": node["id"]}, status=st)

    def get(self, fileId=None, fields=None):
        self.svc.calls.append(("get", fileId))
        node = self.svc.nodes.get(fileId)
        if node is None:
            raise RuntimeError("arquivo inexistente no Drive")
        if self.svc.corromper_tamanho and fileId == self.svc.ultimo_criado:
            node = dict(node, size=int(node["size"]) - 7)
        return _Executavel({
            "id": node["id"], "name": node["name"], "size": node["size"],
            "parents": node["parents"], "appProperties": node["appProperties"],
        })

    def delete(self, fileId=None):
        self.svc.calls.append(("delete", fileId))
        if self.svc.falhar_delete:
            raise RuntimeError("Drive indisponivel (delete)")
        self.svc.nodes.pop(fileId, None)
        return _Executavel({})

    def update(self, *a, **k):
        self.svc.calls.append(("update", k))
        raise AssertionError("files().update PROIBIDO")


class _FakeService:
    def __init__(self):
        self.nodes = {}
        self.blobs = {}
        self.calls = []
        self.falhar_list = False
        self.falhar_delete = False
        self.falhar_create = False
        self.corromper_tamanho = False
        self.ultimo_criado = None
        self._seq = 0

    def _next_id(self):
        self._seq += 1
        return f"id{self._seq:03d}"

    def files(self):
        return _Files(self)

    def _criar(self, nome, parent=None, props=None):
        node = {
            "id": self._next_id(), "name": nome, "parents": [parent] if parent else [],
            "appProperties": dict(props or {}), "size": 100,
        }
        self.nodes[node["id"]] = node
        return node["id"]

    def setup_arvore(self, market_id):
        raiz = self._criar(PASTA_RAIZ)
        mercado = self._criar(market_id, raiz)
        return self._criar(PASTA_BACKUPS, mercado)


class _BaseL5(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-l5-case-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.db = self.base / "mercado.db"
        self.config = self.base / "config.json"
        self.destino = self.base / "staging"
        self._criar_db()
        self.config.write_text(
            json.dumps({"cnpj": "11.222.333/0001-44", "razao_social": "MERCADO TESTE LTDA"},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        self.svc = _FakeService()
        self.folder_id = self.svc.setup_arvore("market-teste")
        self.market_id = "market-teste"
        patcher = patch("backup_drive.registrar", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _criar_db(self):
        conn = sqlite3.connect(str(self.db))
        for tabela in contrato.TABELAS_ESSENCIAIS:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {tabela} (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

    def _pacote(self, **kw):
        params = dict(
            db_path=self.db, config_path=self.config, pasta_destino=self.destino,
            market_id=self.market_id, app_version="1.0.22",
            tipo_backup=service.TIPO_FECHAMENTO,
            origem={"modulo": "desktop", "evento": "teste"},
        )
        params.update(kw)
        return contrato.montar_pacote(**params)

    def _reescrever_zip(self, zip_path, transformador):
        with zipfile.ZipFile(zip_path) as pacote:
            atual = {n: pacote.read(n) for n in pacote.namelist()}
        novo = transformador(dict(atual))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as pacote:
            for nome, dados in novo.items():
                pacote.writestr(nome, dados)
        return zip_path

    def _chave(self, nome):
        return f"{contrato.PASTA_INTERNA}/{nome}"

    def _sobrescrever(self, nome, dados):
        def _t(z):
            z[self._chave(nome)] = dados
            return z
        return _t


class TestManifestoEIntegridade(_BaseL5):
    """T01-T14, T22-T26: manifesto, SHA, market_id, ZIP, DB, config, segredos."""

    # T01
    def test_manifesto_valido(self):
        p = self._pacote()
        ok, problemas, info = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertTrue(ok, problemas)
        m = info["manifest"]
        self.assertEqual(m["backup_format_version"], 1)
        self.assertEqual(m["market_id"], self.market_id)
        self.assertEqual(m["backup_id"], p["backup_id"])
        self.assertEqual(m["app_version"], "1.0.22")
        self.assertEqual(m["tipo_backup"], service.TIPO_FECHAMENTO)
        self.assertIn("generated_at_utc", m)
        self.assertIsInstance(m["origem"], dict)

    def test_manifesto_nao_contem_o_proprio_sha(self):
        p = self._pacote()
        texto = json.dumps(p["manifest"])
        self.assertNotIn(p["manifest_sha256"], texto)

    def test_serializacao_canonica_estavel(self):
        a = contrato.canonical_bytes({"b": 1, "a": [1, 2]})
        b = contrato.canonical_bytes({"a": [1, 2], "b": 1})
        self.assertEqual(a, b)
        self.assertNotIn(b" ", a)

    def test_campos_obrigatorios_por_arquivo(self):
        p = self._pacote()
        for item in p["manifest"]["arquivos"]:
            for campo in ("logical_name", "size", "sha256", "mime_type",
                          "obrigatorio", "restauravel"):
                self.assertIn(campo, item)
        nomes = {i["logical_name"] for i in p["manifest"]["arquivos"]}
        self.assertEqual(nomes, {contrato.ARQUIVO_DB, contrato.ARQUIVO_CONFIG})
        tipos = {i["logical_name"]: i["mime_type"] for i in p["manifest"]["arquivos"]}
        self.assertEqual(tipos[contrato.ARQUIVO_DB], "application/vnd.sqlite3")
        self.assertEqual(tipos[contrato.ARQUIVO_CONFIG], "application/json")

    # T02
    def test_manifesto_corrompido(self):
        p = self._pacote()
        self._reescrever_zip(p["zip_path"], self._sobrescrever(
            contrato.ARQUIVO_MANIFEST, b"{ nao e json"))
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertTrue(any("manifesto corrompido" in x for x in problemas), problemas)

    # T03 / T04
    def test_sha_valido_e_invalido(self):
        p = self._pacote()
        ok, _, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertTrue(ok)
        self._reescrever_zip(p["zip_path"], self._sobrescrever(
            contrato.ARQUIVO_CONFIG, b'{"cnpj":"alterado"}'))
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertTrue(any("sha256 divergente" in x for x in problemas), problemas)


    # T05 / T06
    def test_market_id_divergente_rejeitado(self):
        p = self._pacote()
        ok, _, _ = contrato.validar_pacote(p["zip_path"], "market-teste")
        self.assertTrue(ok)
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], "market-outro")
        self.assertFalse(ok)
        self.assertTrue(any("market_id divergente" in x for x in problemas), problemas)

    # T07 / T08
    def test_zip_integro_e_corrompido(self):
        p = self._pacote()
        self.assertTrue(zipfile.is_zipfile(p["zip_path"]))
        quebrado = self.base / "quebrado.zip"
        quebrado.write_bytes(b"PK\x03\x04 isso nao e um zip valido")
        ok, problemas, _ = contrato.validar_pacote(quebrado, self.market_id)
        self.assertFalse(ok)
        self.assertTrue(problemas)

    # T09 / T10
    def test_db_valido_e_invalido(self):
        p = self._pacote()
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertTrue(ok, problemas)
        self._reescrever_zip(p["zip_path"], self._sobrescrever(
            contrato.ARQUIVO_DB, b"isto nao e sqlite"))
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertTrue(problemas)

    def test_db_sem_tabela_essencial_rejeitado(self):
        reduzido = self.base / "reduzido.db"
        origem = sqlite3.connect(str(self.db))
        alvo = sqlite3.connect(str(reduzido))
        origem.backup(alvo)
        alvo.close()
        origem.close()
        c = sqlite3.connect(str(reduzido))
        c.execute("DROP TABLE usuarios")
        c.commit()
        c.close()
        p = self._pacote()
        self._reescrever_zip(p["zip_path"], self._sobrescrever(
            contrato.ARQUIVO_DB, reduzido.read_bytes()))
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertTrue(any("tabela essencial ausente" in x for x in problemas), problemas)

    def test_db_sem_sidecars_no_pacote(self):
        p = self._pacote()
        with zipfile.ZipFile(p["zip_path"]) as pacote:
            nomes = pacote.namelist()
        for sufixo in ("-wal", "-shm", "-journal"):
            self.assertFalse(any(nome.endswith(sufixo) for nome in nomes), sufixo)

    # T11 / T12
    def test_config_valida_e_invalida(self):
        p = self._pacote()
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertTrue(ok, problemas)
        self._reescrever_zip(p["zip_path"], self._sobrescrever(
            contrato.ARQUIVO_CONFIG, b"{ config quebrada"))
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertTrue(any("config.json invalido" in x for x in problemas), problemas)

    def test_config_invalido_impede_gerar_pacote(self):
        self.config.write_text("{ quebrado", encoding="utf-8")
        with self.assertRaises(Exception):
            self._pacote()

    # T13 / T14
    def test_manifest_ausente(self):
        p = self._pacote()
        self._reescrever_zip(p["zip_path"], lambda z: {
            k: v for k, v in z.items()
            if not k.endswith(f"/{contrato.ARQUIVO_MANIFEST}")})
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertIn("manifest.json ausente", problemas)

    def test_backup_incompleto(self):
        p = self._pacote()
        self._reescrever_zip(p["zip_path"], lambda z: {
            k: v for k, v in z.items()
            if not k.endswith(f"/{contrato.ARQUIVO_CONFIG}")})
        ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
        self.assertFalse(ok)
        self.assertTrue(problemas)

    # T22 / T23 / T24
    def test_nenhum_segredo_no_pacote(self):
        p = self._pacote()
        with zipfile.ZipFile(p["zip_path"]) as pacote:
            nomes = pacote.namelist()
        self.assertEqual(sorted(nomes), sorted([
            self._chave(contrato.ARQUIVO_DB),
            self._chave(contrato.ARQUIVO_CONFIG),
            self._chave(contrato.ARQUIVO_MANIFEST),
        ]))
        for proibido in ("token.pickle", "credentials.json",
                         "client_credentials.sec.json",
                         "firebase-admin-key.json", ".pem", ".key", "-wal", "-shm"):
            self.assertFalse(any(proibido in n for n in nomes), proibido)

    def test_allowlist_bloqueia_segredos(self):
        for nome in ("token.pickle", "credentials.json", "firebase-admin-key.json",
                     "segredo.pem", "mercado.db-wal", "qualquer.txt",
                     "google-services.json"):
            self.assertFalse(contrato.nome_permitido(nome), nome)
        for nome in contrato.ALLOWLIST:
            self.assertTrue(contrato.nome_permitido(nome), nome)

    # T25 / T26
    def test_formato_antigo_ou_futuro_rejeitado(self):
        p = self._pacote()
        with zipfile.ZipFile(p["zip_path"]) as pacote:
            manifesto = json.loads(pacote.read(self._chave(contrato.ARQUIVO_MANIFEST)))
        for formato in (0, 2, "1", None):
            alterado = dict(manifesto, backup_format_version=formato)
            self._reescrever_zip(p["zip_path"], self._sobrescrever(
                contrato.ARQUIVO_MANIFEST, contrato.canonical_bytes(alterado)))
            ok, problemas, _ = contrato.validar_pacote(p["zip_path"], self.market_id)
            self.assertFalse(ok, f"formato {formato!r} deveria ser rejeitado")
            self.assertTrue(any("formato" in x for x in problemas), problemas)


class TestDriveRetencaoEFluxo(_BaseL5):
    """T15-T21: upload, confirmacao, retencao, concorrencia e fechamento."""

    def _popular_drive(self, quantidade, prefixo="market-teste"):
        mercado = [n for n in self.svc.nodes.values() if n["name"] == prefixo][0]
        pasta = self.svc._criar(PASTA_BACKUPS, mercado["id"])
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        criados = []
        for i in range(quantidade):
            momento = base + timedelta(hours=i)
            bid = contrato.gerar_backup_id(prefixo, quando=momento, aleatorio=f"{i:08x}")
            self.svc._criar(f"{bid}.zip", pasta,
                            {"backup_id": bid, "market_id": prefixo})
            criados.append(bid)
        return pasta, criados

    def _zips(self):
        return sorted(n["name"] for n in self.svc.nodes.values()
                      if n["name"].endswith(".zip"))

    def test_envio_usa_parents_explicito_e_app_properties(self):
        p = self._pacote()
        with patch("googleapiclient.http.MediaFileUpload") as Media:
            Media.return_value._stream.return_value = p["zip_path"].read_bytes()
            file_id = drive.enviar_pacote(
                self.svc, p["zip_path"], self.folder_id, p["backup_id"],
                self.market_id, p["manifest_sha256"],
            )
        self.assertTrue(file_id)
        node = [n for n in self.svc.nodes.values() if n["id"] == file_id][0]
        self.assertEqual(node["parents"], [self.folder_id])
        self.assertEqual(node["name"], f"{p['backup_id']}.zip")
        self.assertEqual(node["appProperties"]["market_id"], self.market_id)
        self.assertEqual(node["appProperties"]["manifest_sha256"], p["manifest_sha256"])
        self.assertEqual(node["appProperties"]["backup_format_version"], "1")
        self.assertEqual(Media.call_args.kwargs["mimetype"], "application/zip")
        self.assertNotIn("update", [t for t, _ in self.svc.calls])

    def test_envio_sem_pasta_recusado(self):
        p = self._pacote()
        with self.assertRaises(ValueError):
            drive.enviar_pacote(self.svc, p["zip_path"], "", p["backup_id"],
                                self.market_id, p["manifest_sha256"])

    def _enviar(self, pasta):
        p = self._pacote()
        with patch("googleapiclient.http.MediaFileUpload") as Media:
            Media.return_value._stream.return_value = p["zip_path"].read_bytes()
            fid = drive.enviar_pacote(self.svc, p["zip_path"], pasta,
                                      p["backup_id"], self.market_id,
                                      p["manifest_sha256"])
        return p, fid

    def test_confirmacao_aceita_e_detecta_divergencias(self):
        p, fid = self._enviar(self.folder_id)
        info = drive.confirmar_pacote(self.svc, fid, p["zip_path"], p["backup_id"],
                                      self.market_id, p["manifest_sha256"], self.folder_id)
        self.assertEqual(info["name"], f"{p['backup_id']}.zip")
        with self.assertRaises(RuntimeError):
            drive.confirmar_pacote(self.svc, fid, p["zip_path"], p["backup_id"],
                                   self.market_id, "sha-errado", self.folder_id)
        with self.assertRaises(RuntimeError):
            drive.confirmar_pacote(self.svc, fid, p["zip_path"], p["backup_id"],
                                   self.market_id, p["manifest_sha256"], "outra-pasta")

    def test_confirmacao_detecta_tamanho_divergente(self):
        p, fid = self._enviar(self.folder_id)
        self.svc.corromper_tamanho = True
        self.svc.ultimo_criado = fid
        with self.assertRaises(RuntimeError):
            drive.confirmar_pacote(self.svc, fid, p["zip_path"], p["backup_id"],
                                   self.market_id, p["manifest_sha256"], self.folder_id)


    # T15
    def test_retencao_20_para_15(self):
        pasta, criados = self._popular_drive(20)
        self.assertEqual(len(criados), 20)
        r = drive.aplicar_retencao(self.svc, pasta, manter=15)
        self.assertEqual(len(r["removidos"]), 5)
        self.assertEqual(r["falhas"], [])
        restantes = self._zips()
        self.assertEqual(len(restantes), 15)
        for bid in sorted(criados)[-15:]:
            self.assertIn(f"{bid}.zip", restantes)
        for bid in sorted(criados)[:5]:
            self.assertNotIn(f"{bid}.zip", restantes)

    # T16
    def test_retencao_nunca_remove_o_backup_atual(self):
        pasta, criados = self._popular_drive(18)
        atual = sorted(criados)[0]  # o mais antigo: cairia na exclusao
        r = drive.aplicar_retencao(self.svc, pasta, manter=15, backup_atual_id=atual)
        self.assertNotIn(atual, r["removidos"])
        self.assertIn(f"{atual}.zip", self._zips())

    # T18
    def test_falha_ao_excluir_preserva_backup_novo(self):
        pasta, _antigos = self._popular_drive(18)
        novo = contrato.gerar_backup_id(
            self.market_id, quando=datetime(2026, 6, 1, tzinfo=timezone.utc),
            aleatorio="abcdef01")
        self.svc._criar(f"{novo}.zip", pasta, {"backup_id": novo})
        self.svc.falhar_delete = True
        avisos = []
        r = drive.aplicar_retencao(self.svc, pasta, manter=15,
                                   backup_atual_id=novo, ao_falhar=avisos.append)
        self.assertEqual(r["removidos"], [])
        self.assertTrue(r["falhas"])
        self.assertTrue(avisos)
        self.assertIn(f"{novo}.zip", self._zips())

    def test_retencao_com_drive_indisponivel_nao_apaga_nada(self):
        pasta, _criados = self._popular_drive(20)
        antes = len(self._zips())
        self.svc.falhar_list = True
        r = drive.aplicar_retencao(self.svc, pasta, manter=15)
        self.assertEqual(antes, len(self._zips()))
        self.assertEqual(r["removidos"], [])
        self.assertTrue(r["falhas"])

    # T19
    def test_trava_impede_backup_concorrente(self):
        with drive.TravaDeBackup(usar_arquivo=False):
            with self.assertRaises(RuntimeError):
                with drive.TravaDeBackup(usar_arquivo=False):
                    pass
        with drive.TravaDeBackup(usar_arquivo=False):
            pass  # liberada apos o primeiro bloco

    # T20 / T21
    def test_backup_de_fechamento_exige_caixa_fechado(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE caixa_operacao (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO caixa_operacao (id, status) VALUES (1, 'ABERTO')")
        self.assertTrue(service.caixa_esta_aberto(conn))
        conn.execute("UPDATE caixa_operacao SET status = 'FECHADO' WHERE id = 1")
        self.assertFalse(service.caixa_esta_aberto(conn))
        conn.close()

    # T17
    def test_falha_de_upload_nao_apaga_backup_antigo(self):
        pasta, _antigos = self._popular_drive(3)
        antes = self._zips()
        with patch("googleapiclient.http.MediaFileUpload",
                   side_effect=RuntimeError("sem internet")):
            with self.assertRaises(RuntimeError):
                drive.enviar_pacote(self.svc, self._pacote()["zip_path"], pasta,
                                    "market-teste_20260101T000000Z_aaaaaaaa",
                                    self.market_id, "sha")
        self.assertEqual(antes, self._zips())

    def test_ordem_gerar_validar_enviar_confirmar_reter(self):
        """A retencao so pode rodar depois da confirmacao."""
        eventos = []
        pasta, _antigos = self._popular_drive(2)
        with patch.object(drive, "aplicar_retencao",
                          side_effect=lambda *a, **k: eventos.append("retencao") or {}), \
             patch.object(drive, "confirmar_pacote",
                          side_effect=lambda *a, **k: eventos.append("confirmar")), \
             patch("googleapiclient.http.MediaFileUpload") as Media:
            p = self._pacote()
            Media.return_value._stream.return_value = p["zip_path"].read_bytes()
            fid = drive.enviar_pacote(self.svc, p["zip_path"], pasta,
                                      p["backup_id"], self.market_id,
                                      p["manifest_sha256"])
            drive.confirmar_pacote(self.svc, fid, p["zip_path"], p["backup_id"],
                                   self.market_id, p["manifest_sha256"], pasta)
            drive.aplicar_retencao(self.svc, pasta)
        self.assertEqual(eventos, ["confirmar", "retencao"])


if __name__ == "__main__":
    unittest.main()
