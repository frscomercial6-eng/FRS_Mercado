"""BLOCO 4.6 / L6 — Configuracoes / Google Drive / Download de backup.

100% offline: nenhum acesso ao Drive real, nenhum login/OAuth, nenhum upload.
APPDATA temporario ANTES de qualquer import (modulo_relatorio grava log na
importacao). Bancos e configs sempre temporarios.

DOWNLOAD != RESTORE: nenhum teste aqui restaura mercado.db nem config.json.
"""

import json
import os
import pickle
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-l6-tests-")
_OLD_APPDATA = os.environ.get("APPDATA")
os.environ["APPDATA"] = _TEST_APPDATA.name

import backup_contract as contrato  # noqa: E402
import backup_download as download  # noqa: E402
import drive_conexao as conexao  # noqa: E402
import modulo_config  # noqa: E402


PASTA_RAIZ = "FRS Mercado"
PASTA_BACKUPS = "backups"
MERCADO = "market-teste"


class _Token:
    def __init__(self, valid=False, expired=False, refresh_token=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _About:
    def __init__(self, email, falha=False):
        self._email = email
        self._falha = falha

    def get(self, fields=None):
        if self._falha:
            raise RuntimeError("OAuth cancelado")
        return _Exec({"user": {"emailAddress": self._email}})


class _FakeService:
    def __init__(self, email="conta.real@gmail.com"):
        self.nodes = {}
        self.blobs = {}
        self.calls = []
        self.email = email
        self.about_falha = False
        self.falhar_list = False
        self.falhar_media = False
        self._seq = 0

    def _next_id(self):
        self._seq += 1
        return f"id{self._seq:03d}"

    def files(self):
        return _Files(self)

    def about(self):
        return _About(self.email, self.about_falha)

    def pasta_backups(self, mercado=MERCADO):
        """Id da pasta de backups do mercado (mesmo formato de setup_arvore)."""
        mkt = [n for n in self.nodes.values() if n["name"] == mercado][0]
        pasta = [n for n in self.nodes.values()
                 if n["name"] == PASTA_BACKUPS and n["parents"] == [mkt["id"]]][0]
        return pasta["id"]

    def setup_arvore(self, mercado=MERCADO):
        raiz = self._criar(PASTA_RAIZ)
        mkt = self._criar(mercado, raiz)
        return self._criar(PASTA_BACKUPS, mkt)

    def _criar(self, nome, parent=None, props=None, tamanho=100):
        node = {
            "id": self._next_id(), "name": nome,
            "parents": [parent] if parent else [],
            "appProperties": dict(props or {}), "size": tamanho,
        }
        self.nodes[node["id"]] = node
        return node["id"]


class _Files:
    def __init__(self, svc):
        self.svc = svc

    def list(self, q="", spaces=None, fields=None):
        self.svc.calls.append(("list", q))
        if self.svc.falhar_list:
            raise RuntimeError("Drive indisponivel")
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
                "id": node["id"], "name": node["name"], "size": node["size"],
                "parents": node["parents"], "appProperties": node["appProperties"],
            })
        return _Exec({"files": out})

    def get_media(self, fileId=None):
        self.svc.calls.append(("get_media", fileId))
        if self.svc.falhar_media:
            raise RuntimeError("download indisponivel")
        return _Exec(self.svc.blobs.get(fileId, b""))

    def create(self, body=None, media_body=None, fields=None, resumable=False):
        self.svc.calls.append(("create", body))
        dados = b""
        if media_body is not None:
            try:
                dados = media_body._stream()
            except Exception:
                with open(media_body._filename(), "rb") as fh:
                    dados = fh.read()
        node = {
            "id": self.svc._next_id(), "name": body.get("name"),
            "parents": list(body.get("parents") or []),
            "appProperties": dict(body.get("appProperties") or {}),
            "size": len(dados),
        }
        self.svc.nodes[node["id"]] = node
        self.svc.blobs[node["id"]] = dados
        return _Exec({"id": node["id"]})

    def delete(self, fileId=None):
        self.svc.nodes.pop(fileId, None)
        return _Exec({})

    def update(self, *a, **k):
        raise AssertionError("files().update PROIBIDO")


class _BaseL6(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-l6-case-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.db = self.base / "mercado.db"
        self.config = self.base / "config.json"
        self.dados = self.base / "downloads"
        self._criar_db()
        self.config.write_text(
            json.dumps({"cnpj": "11.222.333/0001-44", "razao_social": "MERCADO TESTE",
                        "market_id": MERCADO}, ensure_ascii=False), encoding="utf-8",
        )
        self.svc = _FakeService()
        self.folder = self.svc.setup_arvore(MERCADO)
        self.token_path = self.base / "token.pickle"
        patcher = patch("backup_drive.registrar", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(modulo_config, "CONFIG_FILE", str(self.config))
        patcher2.start()
        self.addCleanup(patcher2.stop)
        # salvar_configuracoes() chama salvar_limite_sangria_preventiva(), que
        # abriria o banco no APPDATA. Neutralizado para nao deixar rastro em
        # APPDATA e nao contaminar as demais suites do mesmo processo.
        patcher3 = patch.object(modulo_config, "salvar_limite_sangria_preventiva",
                               lambda *a, **k: None)
        patcher3.start()
        self.addCleanup(patcher3.stop)

    def _criar_db(self):
        conn = sqlite3.connect(str(self.db))
        for tabela in contrato.TABELAS_ESSENCIAIS:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {tabela} (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO produtos (id) VALUES (1)")
        conn.commit()
        conn.close()

    def _pacote(self, market_id=MERCADO, quando=None):
        return contrato.montar_pacote(
            db_path=self.db, config_path=self.config,
            pasta_destino=self.base / "staging", market_id=market_id,
            app_version="1.0.22", tipo_backup="fechamento_caixa",
            origem={"modulo": "desktop"}, quando=quando, aleatorio="abcd1234",
        )

    def _publicar(self, p, mercado=MERCADO, pasta=None):
        pasta = pasta or self.svc.pasta_backups(mercado)
        node_id = self.svc._criar(f"{p['backup_id']}.zip", pasta, {
            "backup_id": p["backup_id"], "market_id": mercado,
            "manifest_sha256": p["manifest_sha256"],
            "backup_format_version": "1", "tipo_backup": "fechamento_caixa",
        })
        self.svc.blobs[node_id] = Path(p["zip_path"]).read_bytes()
        return node_id

    def _patch_caminhos(self):
        return patch.object(conexao, "_caminhos",
                            lambda: (self.token_path, self.config))

    def _patch_credenciais(self):
        return patch("database_manager.GOOGLE_CREDS",
                     {"credentials": "C:\\x\\credentials.json", "google_services": "x"})


class TestGmailEStatus(unittest.TestCase):
    """1,2,3 e 9: validacao de Gmail e credenciais ausentes."""

    def test_gmail_valido(self):
        for email in ("exemplo@gmail.com", "a.b+tag@dominio.com.br"):
            ok, motivo = conexao.validar_gmail(email)
            self.assertTrue(ok, f"{email}: {motivo}")

    def test_gmail_invalido(self):
        for email in ("abc", "sem@dominio", "@gmail.com", "a b@gmail.com", "x@y"):
            self.assertFalse(conexao.validar_gmail(email)[0], email)

    def test_gmail_vazio(self):
        ok, motivo = conexao.validar_gmail("")
        self.assertFalse(ok)
        self.assertIn("Informe", motivo)
        self.assertFalse(conexao.validar_gmail(None)[0])


class TestStatusReal(unittest.TestCase):
    """6,7,8,9,24: o status NUNCA mente sobre a autorizacao."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-l6-status-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.token_path = self.base / "token.pickle"
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"market_id": MERCADO}), encoding="utf-8")

    def _status(self, token_bytes=None, creds=True):
        if token_bytes is not None:
            self.token_path.write_bytes(token_bytes)
        with patch.object(conexao, "_caminhos",
                          lambda: (self.token_path, self.config)), \
             patch("database_manager.GOOGLE_CREDS",
                   {"credentials": "x" if creds else None, "google_services": "y"}), \
             patch.object(conexao, "conta_persistida", lambda: "conta.real@gmail.com"):
            return conexao.status_conexao()

    def test_credenciais_ausentes(self):
        st = self._status(None, creds=False)
        self.assertFalse(st["conectado"])
        self.assertEqual(st["situacao"], conexao.SITUACAO_SEM_CREDENCIAIS)

    def test_token_inexistente(self):
        st = self._status(None)
        self.assertFalse(st["conectado"])
        self.assertEqual(st["situacao"], conexao.SITUACAO_SEM_TOKEN)

    def test_token_invalido(self):
        st = self._status(b"lixo nao e pickle")
        self.assertFalse(st["conectado"])
        self.assertEqual(st["situacao"], conexao.SITUACAO_TOKEN_INVALIDO)

    def test_token_valido_mostra_conta(self):
        st = self._status(pickle.dumps(_Token(valid=True)))
        self.assertTrue(st["conectado"])
        self.assertEqual(st["conta"], "conta.real@gmail.com")
        self.assertIn("conta.real@gmail.com", st["mensagem"])

    def test_token_expirado_renovavel_nao_fica_verde(self):
        st = self._status(pickle.dumps(_Token(expired=True, refresh_token="r")))
        self.assertFalse(st["conectado"])
        self.assertEqual(st["situacao"], conexao.SITUACAO_RENOVAVEL)

    def test_token_revogado_nao_fica_verde(self):
        st = self._status(pickle.dumps(_Token(expired=True, refresh_token=None)))
        self.assertFalse(st["conectado"])
        self.assertEqual(st["situacao"], conexao.SITUACAO_TOKEN_REVOGADO)
        self.assertIn("🔴", st["mensagem"])

    def test_nunca_verde_sem_token_utilizavel(self):
        for token in (_Token(valid=False, expired=True, refresh_token=None),
                      _Token(valid=False, expired=False, refresh_token="r"),
                      _Token(valid=False, expired=True, refresh_token="r")):
            with self.subTest(token=vars(token)):
                st = self._status(pickle.dumps(token))
                self.assertFalse(st["conectado"])
                self.assertNotEqual(st["cor"], "#2ecc71")


class TestConexaoEArvore(_BaseL6):
    """4,5,10,11,12,13,14: OAuth, conta real e arvore automatica."""

    def test_conectar_persiste_conta_real_do_google(self):
        self.svc.email = "conta.real@gmail.com"
        with self._patch_caminhos(), self._patch_credenciais():
            r = conexao.conectar("digitado@gmail.com", service=self.svc)
        self.assertEqual(r["conta"], "conta.real@gmail.com")
        self.assertTrue(r["divergente"])
        salvo = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(salvo["google_drive_conta"], "conta.real@gmail.com")

    def test_conta_divergente_tratada_sem_mascarar(self):
        self.svc.email = "outro@gmail.com"
        with self._patch_caminhos(), self._patch_credenciais():
            r = conexao.conectar("digitado@gmail.com", service=self.svc)
        self.assertEqual(r["conta"], "outro@gmail.com")
        self.assertTrue(r["divergente"])
        self.assertEqual(r["email_informado"], "digitado@gmail.com")

    def test_conta_iguais_nao_marcam_divergencia(self):
        self.svc.email = "mesma@gmail.com"
        with self._patch_caminhos(), self._patch_credenciais():
            r = conexao.conectar("mesma@gmail.com", service=self.svc)
        self.assertFalse(r["divergente"])

    def test_oauth_cancelado_nao_persiste_conta(self):
        self.svc.about_falha = True
        with self._patch_caminhos(), self._patch_credenciais():
            with self.assertRaises(conexao.ErroConexaoDrive):
                conexao.conectar("algo@gmail.com", service=self.svc)
        self.assertNotIn("google_drive_conta",
                         json.loads(self.config.read_text(encoding="utf-8")))

    def test_gmail_invalido_bloqueia_antes_do_oauth(self):
        with self.assertRaises(conexao.ErroConexaoDrive):
            conexao.conectar("nao-e-email", service=self.svc)
        self.assertEqual(self.svc.calls, [])

    def test_arvore_criada_automaticamente(self):
        with self._patch_caminhos(), self._patch_credenciais():
            conexao.conectar("a@gmail.com", service=self.svc)
        nomes = {n["name"] for n in self.svc.nodes.values()}
        self.assertIn(PASTA_RAIZ, nomes)
        self.assertIn(MERCADO, nomes)
        self.assertIn(PASTA_BACKUPS, nomes)

    def test_cliente_nao_informa_pasta_nem_id(self):
        with self._patch_caminhos(), self._patch_credenciais():
            r = conexao.conectar("a@gmail.com", service=self.svc)
        self.assertTrue(r["backups_folder_id"])
        for tipo, body in self.svc.calls:
            if tipo == "create":
                self.assertNotIn("drive_backup_folder_id", str(body))

    def test_conta_recuperada_ao_reabrir_configuracoes(self):
        with self._patch_caminhos(), self._patch_credenciais():
            conexao.conectar("a@gmail.com", service=self.svc)
        with patch.object(modulo_config, "CONFIG_FILE", str(self.config)):
            self.assertEqual(conexao.conta_persistida(), "conta.real@gmail.com")

    def test_nao_persiste_senha_nem_credencial(self):
        with self._patch_caminhos(), self._patch_credenciais():
            conexao.conectar("a@gmail.com", service=self.svc)
        texto = self.config.read_text(encoding="utf-8").lower()
        for proibido in ("password", "senha", "client_secret", "refresh_token",
                         "private_key", "token.pickle"):
            self.assertNotIn(proibido, texto)


class TestDownloadSemRestore(_BaseL6):
    """16-22: download escopado, validado e SEM restore."""

    def _publicar_varios(self, quant=3):
        base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        publicados = []
        for i in range(quant):
            p = self._pacote(quando=base + timedelta(days=i))
            node = self._publicar(p)
            publicados.append((p, node))
        return publicados

    def _mercado_alheio(self):
        raiz = [n for n in self.svc.nodes.values() if n["name"] == PASTA_RAIZ][0]
        pasta = self.svc._criar("market-alheio", raiz["id"])
        return self.svc._criar(PASTA_BACKUPS, pasta)

    def test_listagem_limitada_ao_mercado_atual(self):
        self._publicar_varios()
        pasta_outro = self._mercado_alheio()
        p_outro = self._pacote(market_id="market-alheio")
        self.svc._criar(f"{p_outro['backup_id']}.zip", pasta_outro,
                        {"backup_id": p_outro["backup_id"], "market_id": "market-alheio"})
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            itens = download.listar_backups_mercado(self.svc)
        self.assertTrue(itens)
        for item in itens:
            self.assertEqual(contrato.backup_id_mercado(item["backup_id"]), MERCADO)

    def test_baixar_ultimo_escolhe_o_mais_recente(self):
        publicados = self._publicar_varios(3)
        esperado = max((p for p, _ in publicados), key=lambda p: p["backup_id"])
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            r = download.baixar_ultimo_backup(self.dados, service=self.svc)
        self.assertEqual(r["backup_id"], esperado["backup_id"])
        self.assertTrue(Path(r["caminho"]).is_file())

    def test_escolher_backup_especifico(self):
        publicados = self._publicar_varios(3)
        alvo = publicados[0][0]
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            itens = download.listar_backups_mercado(self.svc)
            r = download.baixar_backup(alvo["backup_id"], self.dados, service=self.svc)
        self.assertEqual(r["backup_id"], alvo["backup_id"])
        self.assertTrue(any(i["backup_id"] == alvo["backup_id"] for i in itens))
        self.assertTrue(all("data_hora" in i and "tipo" in i for i in itens))

    def test_item_de_mercado_alheio_recusado(self):
        pasta_outro = self._mercado_alheio()
        p = self._pacote(market_id="market-alheio")
        node = self.svc._criar(
            f"{p['backup_id']}.zip", pasta_outro,
            {"backup_id": p["backup_id"], "market_id": "market-alheio"})
        self.svc.blobs[node] = Path(p["zip_path"]).read_bytes()
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            with self.assertRaises(download.ErroDownload):
                download.baixar_backup(p["backup_id"], self.dados, service=self.svc)

    def test_pacote_invalido_recusado_e_removido(self):
        p = self._pacote()
        node = self._publicar(p)
        self.svc.blobs[node] = b"PK\x03\x04 lixo nao e zip"
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            with self.assertRaises(download.ErroDownload):
                download.baixar_backup(p["backup_id"], self.dados, service=self.svc)
        self.assertEqual(list(self.dados.glob("*.zip")), [])

    def test_formato_incompativel_recusado(self):
        p = self._pacote()
        pasta = self.svc.pasta_backups(MERCADO)
        node = self.svc._criar(f"{p['backup_id']}.zip", pasta, {
            "backup_id": p["backup_id"], "market_id": MERCADO,
            "backup_format_version": "99", "manifest_sha256": p["manifest_sha256"],
        })
        self.svc.blobs[node] = Path(p["zip_path"]).read_bytes()
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            with self.assertRaises(download.ErroDownload):
                download.baixar_backup(p["backup_id"], self.dados, service=self.svc)

    def test_download_nao_altera_banco_nem_config(self):
        p = self._publicar_varios(1)[0][0]
        antes_db = self.db.read_bytes()
        antes_cfg = self.config.read_bytes()
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            download.baixar_ultimo_backup(self.dados, service=self.svc)
        self.assertEqual(antes_db, self.db.read_bytes())
        self.assertEqual(antes_cfg, self.config.read_bytes())

    def test_download_nao_e_restore(self):
        for arquivo in (modulo_config.__file__, download.__file__):
            texto = Path(arquivo).read_text(encoding="utf-8")
            codigo = "\n".join(l for l in texto.splitlines()
                               if not l.strip().startswith("#"))
            for proibido in ("os.replace", "shutil.move", "def restaurar"):
                self.assertNotIn(proibido, codigo, f"{arquivo}: {proibido}")

    def test_drive_indisponivel_nao_quebra(self):
        self._publicar_varios(1)
        self.svc.falhar_list = True
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            with self.assertRaises(RuntimeError):
                download.listar_backups_mercado(self.svc)

    def test_nenhum_download_de_segredo(self):
        pasta = self.svc.pasta_backups(MERCADO)
        for nome in ("token.pickle", "credentials.json",
                     "client_credentials.sec.json", "firebase-admin-key.json"):
            node = self.svc._criar(nome, pasta, {})
            self.svc.blobs[node] = b"segredo"
        with patch.object(download, "_mercado_id", lambda: MERCADO):
            itens = download.listar_backups_mercado(self.svc)
        self.assertTrue(all(i["nome"].endswith(".zip") for i in itens))


if __name__ == "__main__":
    unittest.main()
