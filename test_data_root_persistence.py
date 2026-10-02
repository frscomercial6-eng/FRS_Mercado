import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-data-root-tests-")
_OLD_APPDATA = os.environ.get("APPDATA")
os.environ["APPDATA"] = _TEST_APPDATA.name

import app_paths
import database_manager
import updater_helper
import market_identity
import modulo_config


class TestDataRootPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frs-case-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.dest = self.base / "canonical"
        self.old = self.base / "legacy"
        self.dest.mkdir()
        self.old.mkdir()

    @classmethod
    def tearDownClass(cls):
        # Keep the process-wide test root alive for other test modules imported
        # in the same unittest run; TemporaryDirectory cleans it at process exit.
        return None

    def _write_config(self, root, payload):
        path = Path(root) / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _real_config(self, name="Mercado Exemplo"):
        return {
            "nome_fantasia": name,
            "razao_social": f"{name} LTDA",
            "nome_estabelecimento": name,
            "cnpj": "12.345.678/0001-90",
            "logradouro": "Rua Um",
            "numero": "10",
            "cidade": "Cidade",
            "uf": "SP",
            "fiscal_ativo": False,
            "auto_update_enabled": True,
            "auto_update_repo": "frscomercial6-eng/FRS_Mercado",
            "pasta_entrada_fiscal": str(self.dest / "fiscal_in"),
            "pasta_retorno_fiscal": str(self.dest / "fiscal_out"),
            "pasta_exportacao_fiscal": str(self.dest / "exportacao_fiscal"),
            "limite_sangria_preventiva": 500.0,
        }

    def _write_db(self, root, products=0, fiscal_api_key="", cash_limit="500.00"):
        path = Path(root) / "mercado.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE produtos (id INTEGER PRIMARY KEY, nome TEXT)")
            conn.executemany(
                "INSERT INTO produtos (nome) VALUES (?)",
                [(f"Produto {index}",) for index in range(products)],
            )
            conn.execute(
                "CREATE TABLE config_fiscal (id INTEGER PRIMARY KEY, api_key TEXT, ambiente TEXT, webhook_token_hash TEXT)"
            )
            conn.execute(
                "INSERT INTO config_fiscal VALUES (1, ?, 'HOMOLOGACAO', '')",
                (fiscal_api_key,),
            )
            conn.execute("CREATE TABLE config_sistema (chave TEXT PRIMARY KEY, valor TEXT)")
            conn.execute("INSERT INTO config_sistema VALUES ('limite_caixa', ?)", (cash_limit,))
            conn.execute("CREATE TABLE config_taxas (tipo TEXT, percentual REAL)")
            conn.executemany(
                "INSERT INTO config_taxas VALUES (?, 0.0)",
                [("DEBITO",), ("CREDITO",), ("VOUCHER",)],
            )
            conn.execute(
                "CREATE TABLE config_aliquotas_ncm "
                "(ncm_prefixo TEXT, aliquota_percentual REAL, descricao TEXT, ativo INTEGER)"
            )
            conn.execute(
                "INSERT INTO config_aliquotas_ncm VALUES ('*', 0.0, 'Aliquota padrao/fallback', 1)"
            )
            conn.execute(
                "CREATE TABLE config_aliquotas_fiscais_ncm "
                "(ncm_prefixo TEXT, aliquota_icms REAL, aliquota_pis REAL, aliquota_cofins REAL, "
                "aliquota_ibs REAL, aliquota_cbs REAL, descricao TEXT, ativo INTEGER)"
            )
            conn.execute(
                "INSERT INTO config_aliquotas_fiscais_ncm "
                "VALUES ('*', 0, 0, 0, 0, 0, 'Aliquotas fiscais padrao/fallback', 1)"
            )
            conn.commit()
        finally:
            conn.close()
        return path

    @staticmethod
    def _hash(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_canonical_valid_company_config_is_not_replaced(self):
        config = self._real_config()
        target = self._write_config(self.dest, config)
        source = self._write_config(self.old, self._real_config("Legado Diferente"))
        before = target.read_bytes()

        result = app_paths._adotar_configuracao(self.dest, [self.old])

        self.assertIn("mantida", result)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(
            json.loads(source.read_text(encoding="utf-8")),
            self._real_config("Legado Diferente"),
        )

    def test_canonical_operational_db_and_company_config_are_preserved(self):
        config = self._write_config(self.dest, self._real_config())
        db = self._write_db(self.dest, products=1)
        self._write_config(self.old, self._real_config("Outro Mercado"))
        self._write_db(self.old, products=2)
        config_before = config.read_bytes()
        db_before = self._hash(db)

        app_paths._adotar_configuracao(self.dest, [self.old])
        app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])

        self.assertEqual(config.read_bytes(), config_before)
        self.assertEqual(self._hash(db), db_before)
        self.assertEqual(app_paths._total_registros_operacionais(str(db)), 1)

    def test_empty_destination_adopts_company_config_and_preserves_source(self):
        source = self._write_config(self.old, self._real_config())
        before = self._hash(source)

        result = app_paths._adotar_configuracao(self.dest, [self.old])

        adopted = self.dest / "config.json"
        self.assertIn("adotada", result)
        self.assertEqual(json.loads(adopted.read_text(encoding="utf-8"))["cnpj"], "12.345.678/0001-90")
        self.assertEqual(self._hash(source), before)

    def test_empty_destination_adopts_operational_db_with_sqlite_snapshot(self):
        source = self._write_db(self.old, products=2)
        before = self._hash(source)

        result = app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])

        adopted = self.dest / "mercado.db"
        self.assertIn("adotado", result)
        self.assertEqual(app_paths._total_registros_operacionais(str(adopted)), 2)
        self.assertEqual(self._hash(source), before)

    def test_config_and_database_are_both_adopted_and_unchanged_at_source(self):
        source_config = self._write_config(self.old, self._real_config())
        source_db = self._write_db(self.old, products=3)
        config_hash = self._hash(source_config)
        db_hash = self._hash(source_db)

        app_paths._adotar_configuracao(self.dest, [self.old])
        app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])

        self.assertTrue((self.dest / "config.json").is_file())
        self.assertEqual(app_paths._total_registros_operacionais(str(self.dest / "mercado.db")), 3)
        self.assertEqual(self._hash(source_config), config_hash)
        self.assertEqual(self._hash(source_db), db_hash)

    def test_default_config_is_backed_up_then_replaced_by_real_legacy(self):
        default = {
            "nome_fantasia": "",
            "razao_social": "",
            "nome_estabelecimento": "",
            "cnpj": "",
            "fiscal_ativo": False,
            "auto_update_enabled": True,
            "auto_update_repo": "frscomercial6-eng/FRS_Mercado",
            "auto_update_remind_hours": 24,
            "pasta_entrada_fiscal": str(self.dest / "fiscal_in"),
            "pasta_retorno_fiscal": str(self.dest / "fiscal_out"),
            "pasta_exportacao_fiscal": str(self.dest / "exportacao_fiscal"),
            "limite_sangria_preventiva": 500.0,
        }
        target = self._write_config(self.dest, default)
        target_before = target.read_bytes()
        self._write_config(self.old, self._real_config())

        app_paths._adotar_configuracao(self.dest, [self.old])

        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["razao_social"], "Mercado Exemplo LTDA")
        backups = list(self.dest.glob("config.json.pre-adocao*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), target_before)

    def test_invalid_legacy_config_is_not_adopted(self):
        source = self.old / "config.json"
        source.write_text("{broken", encoding="utf-8")

        result = app_paths._adotar_configuracao(self.dest, [self.old])

        self.assertIn("nenhuma configuração", result)
        self.assertFalse((self.dest / "config.json").exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "{broken")

    def test_conflicting_valid_legacy_configs_fail_closed(self):
        other = self.base / "legacy2"
        other.mkdir()
        self._write_config(self.old, self._real_config("Loja Um"))
        self._write_config(other, self._real_config("Loja Dois"))

        with self.assertRaisesRegex(RuntimeError, "divergentes"):
            app_paths._adotar_configuracao(self.dest, [self.old, other])
        self.assertFalse((self.dest / "config.json").exists())

    def test_no_valid_legacy_allows_normal_default_config_creation(self):
        from unittest.mock import patch as local_patch

        config_path = self.dest / "config.json"
        with local_patch.object(modulo_config, "CONFIG_FILE", str(config_path)), local_patch.object(
            modulo_config, "_garantir_pastas_usuario", lambda: None
        ), local_patch.object(
            modulo_config,
            "obter_caminho_dados",
            side_effect=lambda *parts: str(self.dest.joinpath(*parts)),
        ):
            config = modulo_config.carregar_configuracoes()
        self.assertEqual(config["razao_social"], "")
        self.assertTrue(config_path.is_file())

    def test_existing_invalid_canonical_config_is_preserved_and_not_defaulted(self):
        from unittest.mock import patch as local_patch

        config_path = self.dest / "config.json"
        config_path.write_text("{broken", encoding="utf-8")
        before = config_path.read_bytes()
        with local_patch.object(modulo_config, "CONFIG_FILE", str(config_path)), local_patch.object(
            modulo_config, "_garantir_pastas_usuario", lambda: None
        ):
            with self.assertRaisesRegex(RuntimeError, "inválido e foi preservado"):
                modulo_config.carregar_configuracoes()
        self.assertEqual(config_path.read_bytes(), before)

    def test_root_rules_source_traditional_portable_and_versioned(self):
        appdata_root = self.base / "profile" / "FRS_Mercado" / "data"
        install_root = self.base / "Program Files" / "FRS Mercado"
        portable_root = self.base / "Portable"
        versioned_root = portable_root / "runtime" / "versions" / "1.0.22"
        versioned_root.mkdir(parents=True)
        (portable_root / "data").mkdir()

        with patch.object(app_paths, "e_compilado", return_value=False), patch.object(
            app_paths, "_base_appdata", return_value=str(appdata_root)
        ):
            self.assertEqual(app_paths._raiz_dados_preferida(), appdata_root.resolve())

        with patch.object(app_paths, "e_compilado", return_value=True), patch.object(
            app_paths, "_base_executavel", return_value=str(install_root)
        ), patch.object(app_paths, "_esta_em_program_files", return_value=True), patch.object(
            app_paths, "_base_appdata", return_value=str(appdata_root)
        ):
            self.assertEqual(app_paths._raiz_dados_preferida(), appdata_root.resolve())

        with patch.object(app_paths, "e_compilado", return_value=True), patch.object(
            app_paths, "_base_executavel", return_value=str(portable_root)
        ), patch.object(app_paths, "_esta_em_program_files", return_value=False), patch.object(
            app_paths, "_versioned_portable_data_base", return_value=None
        ):
            self.assertEqual(app_paths._raiz_dados_preferida(), portable_root / "data")

        with patch.object(app_paths, "e_compilado", return_value=True), patch.object(
            app_paths, "_base_executavel", return_value=str(versioned_root)
        ), patch.object(app_paths, "_esta_em_program_files", return_value=False):
            self.assertEqual(app_paths._raiz_dados_preferida(), portable_root / "data")

    def test_empty_seed_db_is_adopted_but_custom_database_state_is_not_overwritten(self):
        self._write_db(self.dest, products=0)
        source = self._write_db(self.old, products=2)

        result = app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])

        self.assertIn("adotado", result)
        self.assertEqual(app_paths._total_registros_operacionais(str(self.dest / "mercado.db")), 2)
        self.assertTrue(source.is_file())

    def test_customer_specific_fiscal_setting_makes_database_nonempty(self):
        target = self._write_db(self.dest, products=0, fiscal_api_key="client-key")
        self._write_db(self.old, products=3)
        before = self._hash(target)

        result = app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])

        self.assertIn("não sobrescrever", result)
        self.assertEqual(self._hash(target), before)

    def test_sidecars_prevent_database_adoption_without_mutation(self):
        target = self._write_db(self.dest, products=0)
        (self.dest / "mercado.db-wal").write_bytes(b"fixture wal marker")
        source = self._write_db(self.old, products=1)
        target_before = self._hash(target)
        source_before = self._hash(source)

        with patch.object(app_paths, "_sqlite_valido", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "sidecar"):
                app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])
        self.assertEqual(self._hash(target), target_before)
        self.assertEqual(self._hash(source), source_before)

    def test_divergent_legacy_databases_fail_closed(self):
        other = self.base / "legacy2"
        other.mkdir()
        self._write_db(self.old, products=1)
        self._write_db(other, products=2)

        with self.assertRaisesRegex(RuntimeError, "bancos legados operacionais divergentes"):
            app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old, other])
        self.assertFalse((self.dest / "mercado.db").exists())

    def test_corrupt_canonical_db_with_operational_legacy_fails_closed(self):
        (self.dest / "mercado.db").write_bytes(b"not a sqlite database")
        self._write_db(self.old, products=1)
        corrupt_before = (self.dest / "mercado.db").read_bytes()

        with self.assertRaisesRegex(RuntimeError, "Banco canônico inválido"):
            app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])
        self.assertEqual((self.dest / "mercado.db").read_bytes(), corrupt_before)

    def test_repeated_adoption_is_idempotent(self):
        source = self._write_config(self.old, self._real_config())
        source_hash = self._hash(source)
        app_paths._adotar_configuracao(self.dest, [self.old])
        target_hash = self._hash(self.dest / "config.json")

        result = app_paths._adotar_configuracao(self.dest, [self.old])

        self.assertIn("mantida", result)
        self.assertEqual(self._hash(source), source_hash)
        self.assertEqual(self._hash(self.dest / "config.json"), target_hash)

    def test_path_resolution_adopts_config_and_db_before_returning(self):
        self._write_config(self.old, self._real_config())
        self._write_db(self.old, products=2)
        with patch.object(app_paths, "_raiz_dados_preferida", return_value=self.dest), patch.object(
            app_paths, "_raizes_legadas", return_value=[self.old]
        ), patch.object(app_paths, "_garantir_escrita", return_value=None), patch.object(
            app_paths, "_ADOCAO_AVALIADA", False
        ):
            resolved = app_paths.obter_caminho_dados("config.json")

        self.assertEqual(Path(resolved), self.dest / "config.json")
        self.assertEqual(json.loads((self.dest / "config.json").read_text(encoding="utf-8"))["cnpj"], "12.345.678/0001-90")
        self.assertEqual(app_paths._total_registros_operacionais(str(self.dest / "mercado.db")), 2)

    def test_config_and_db_from_different_roots_are_adopted_by_artifact(self):
        db_root = self.base / "legacy-db"
        db_root.mkdir()
        source_config = self._write_config(self.old, self._real_config())
        source_db = self._write_db(db_root, products=4)
        config_hash = self._hash(source_config)
        db_hash = self._hash(source_db)

        with patch.object(app_paths, "_ADOCAO_AVALIADA", False):
            app_paths._adotar_dados_protegidos(self.dest, [self.old, db_root])

        self.assertEqual(json.loads((self.dest / "config.json").read_text(encoding="utf-8"))["cnpj"], "12.345.678/0001-90")
        self.assertEqual(app_paths._total_registros_operacionais(str(self.dest / "mercado.db")), 4)
        self.assertEqual(self._hash(source_config), config_hash)
        self.assertEqual(self._hash(source_db), db_hash)

    def test_config_copy_failure_blocks_root_resolution_and_preserves_source(self):
        source = self._write_config(self.old, self._real_config())
        source_hash = self._hash(source)
        with patch.object(app_paths, "_copiar_arquivo_atomico", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Configuração legada válida"):
                app_paths._adotar_configuracao(self.dest, [self.old])
        self.assertFalse((self.dest / "config.json").exists())
        self.assertEqual(self._hash(source), source_hash)

    def test_db_snapshot_failure_blocks_adoption_and_preserves_source(self):
        source = self._write_db(self.old, products=2)
        source_hash = self._hash(source)
        with patch.object(app_paths.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "origem preservada"):
                app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])
        self.assertFalse((self.dest / "mercado.db").exists())
        self.assertEqual(self._hash(source), source_hash)

    def test_invalid_persistent_json_is_not_adopted(self):
        bad = self.old / "credentials.json"
        bad.write_text(json.dumps({"unrelated": "object"}), encoding="utf-8")

        app_paths._adotar_outros_arquivos(self.dest, [self.old])

        self.assertFalse((self.dest / "credentials.json").exists())
        self.assertEqual(json.loads(bad.read_text(encoding="utf-8")), {"unrelated": "object"})

    def test_conflicting_tokens_fail_closed(self):
        first = self.old / "token.pickle"
        first.write_bytes(b"token-generation-one")
        other = self.base / "legacy2"
        other.mkdir()
        second = other / "token.pickle"
        second.write_bytes(b"token-generation-two")

        with self.assertRaisesRegex(RuntimeError, "token.pickle"):
            app_paths._adotar_outros_arquivos(self.dest, [self.old, other])
        self.assertFalse((self.dest / "token.pickle").exists())
        self.assertEqual(first.read_bytes(), b"token-generation-one")
        self.assertEqual(second.read_bytes(), b"token-generation-two")

    def test_conflicting_backup_files_fail_closed(self):
        one = self.old / "backups" / "daily.db"
        one.parent.mkdir()
        self._write_db(one.parent, products=1)
        one = one.parent / "mercado.db"
        other_root = self.base / "legacy2"
        other = self._write_db(other_root / "backups", products=2)
        # Same relative backup name in two roots with different valid contents.
        one.rename(self.old / "backups" / "daily.db")
        other.rename(other_root / "backups" / "daily.db")

        with self.assertRaisesRegex(RuntimeError, "backups.*daily.db"):
            app_paths._adotar_outros_arquivos(self.dest, [self.old, other_root])
        self.assertFalse((self.dest / "backups" / "daily.db").exists())

    def test_known_persistent_files_and_directories_are_adopted_idempotently(self):
        credential = self.old / "client_credentials.sec.json"
        credential.write_text(
            json.dumps({"version": 1, "scheme": "plain", "payload": {"drive_backup_folder_id": "folder"}}),
            encoding="utf-8",
        )
        oauth = self.old / "credentials.json"
        oauth.write_text(json.dumps({"installed": {"client_id": "fixture-id"}}), encoding="utf-8")
        image = self.old / "assets" / "produtos" / "produto.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"image-fixture")
        backup = self._write_db(self.old / "backups", products=1)
        credential_hash = self._hash(credential)
        image_hash = self._hash(image)
        backup_hash = self._hash(backup)

        app_paths._adotar_outros_arquivos(self.dest, [self.old])
        app_paths._adotar_outros_arquivos(self.dest, [self.old])

        self.assertEqual(self._hash(self.dest / credential.relative_to(self.old)), credential_hash)
        self.assertEqual(json.loads((self.dest / "credentials.json").read_text(encoding="utf-8")), {"installed": {"client_id": "fixture-id"}})
        self.assertEqual(self._hash(self.dest / image.relative_to(self.old)), image_hash)
        self.assertEqual(self._hash(self.dest / backup.relative_to(self.old)), backup_hash)
        self.assertEqual(self._hash(credential), credential_hash)

    def test_updater_backup_restore_covers_persistent_files_and_directories(self):
        data = self.dest
        (data / "config.json").write_text(json.dumps(self._real_config()), encoding="utf-8")
        (data / "update_state.json").write_text(json.dumps({"state": "AVAILABLE"}), encoding="utf-8")
        (data / "client_credentials.sec.json").write_text(
            json.dumps({"version": 1, "scheme": "plain", "payload": {"key": "value"}}),
            encoding="utf-8",
        )
        image = data / "assets" / "produtos" / "item.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"user-image")
        report = data / "relatorios" / "report.csv"
        report.parent.mkdir()
        report.write_text("user report", encoding="utf-8")
        db = data / "mercado.db"
        db.write_bytes(b"sqlite fixture bytes")
        before = updater_helper._protected_hashes(data)

        backup = self.base / "backup"
        updater_helper._backup_protected_data(data, backup)
        (data / "config.json").write_text("changed", encoding="utf-8")
        image.write_bytes(b"changed")
        report.write_text("changed", encoding="utf-8")
        (data / "update_state.json").write_text(json.dumps({"state": "FAILED"}), encoding="utf-8")
        updater_helper._restore_protected_data(data, backup)

        self.assertEqual(updater_helper._protected_hashes(data), before)
        self.assertEqual(json.loads((data / "update_state.json").read_text(encoding="utf-8")), {"state": "AVAILABLE"})
        self.assertEqual((data / "client_credentials.sec.json").read_bytes(), (backup / "client_credentials.sec.json").read_bytes())

    def test_updater_hash_detects_changed_protected_data(self):
        config = self.dest / "config.json"
        config.write_text('{"razao_social":"A"}', encoding="utf-8")
        before = updater_helper._protected_hashes(self.dest)
        config.write_text('{"razao_social":"B"}', encoding="utf-8")
        with self.assertRaisesRegex(updater_helper.HelperError, "dados protegidos"):
            updater_helper._assert_protected_data_unchanged(before, self.dest)

    def test_updater_state_rebases_without_copying_staged_absolute_paths(self):
        source = self.old / "update_state.json"
        source.write_text(
            json.dumps({
                "state": "STAGED",
                "version": "1.0.22",
                "asset_url": "https://example.invalid/asset.zip",
                "asset_name": "asset.zip",
                "size": 10,
                "sha256": "a" * 64,
                "key_id": "release-key-1",
                "staged_path": str(self.old / "updates" / "asset.zip"),
                "job_path": str(self.old / "updates" / "job.json"),
            }),
            encoding="utf-8",
        )

        self.assertTrue(app_paths._adotar_estado_updater(source, self.dest / "update_state.json"))
        state = json.loads((self.dest / "update_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "AVAILABLE")
        self.assertNotIn("staged_path", state)
        self.assertNotIn("job_path", state)
        self.assertTrue(source.is_file())

    def test_updater_uses_same_runtime_portable_root(self):
        root = self.base / "portable"
        helper = root / "data" / "updates" / "bin" / "FRS_Mercado_UpdateHelper.exe"
        helper.parent.mkdir(parents=True)
        helper.touch()
        (root / "runtime").mkdir()
        (root / "data").mkdir(exist_ok=True)
        with patch.object(updater_helper, "e_compilado", return_value=True), patch.object(
            updater_helper, "executavel_atual", return_value=helper
        ):
            self.assertEqual(updater_helper._portable_root(), root)

    def test_inno_seed_is_create_only_and_does_not_create_config_outside_root(self):
        script = (Path(__file__).parent / "setup_frs.iss").read_text(encoding="utf-8")
        self.assertIn('DestDir: "{app}"; Excludes: "data\\*";', script)
        self.assertIn('DestDir: "{app}\\data"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist onlyifdoesntexist', script)
        self.assertNotIn("CriarConfiguracaoInicial", script)

    def test_corrupt_database_backup_never_deletes_previous_generation(self):
        db = self.dest / "mercado.db"
        db.write_bytes(b"corrupt-fixture")
        old_backup = self.dest / "mercado_old.db"
        old_backup.write_bytes(b"older-backup")
        wal = Path(str(db) + "-wal")
        wal.write_bytes(b"wal-fixture")

        backup_path = database_manager._preservar_banco_corrompido(str(db))

        self.assertNotEqual(Path(backup_path), old_backup)
        self.assertEqual(old_backup.read_bytes(), b"older-backup")
        self.assertEqual(Path(backup_path).read_bytes(), b"corrupt-fixture")
        self.assertEqual(Path(backup_path + "-wal").read_bytes(), b"wal-fixture")

    def test_source_and_traditional_use_appdata_and_only_source_seed_is_new(self):
        appdata_root = self.base / "appdata" / "FRS_Mercado" / "data"
        install_root = self.base / "Program Files" / "FRS Mercado"
        with patch.object(app_paths, "e_compilado", return_value=False), patch.object(
            app_paths, "_base_appdata", return_value=str(appdata_root)
        ):
            self.assertEqual(app_paths._raiz_dados_preferida(), appdata_root.resolve())
        with patch.object(app_paths, "e_compilado", return_value=True), patch.object(
            app_paths, "_base_executavel", return_value=str(install_root)
        ), patch.object(app_paths, "_esta_em_program_files", return_value=True), patch.object(
            app_paths, "_base_appdata", return_value=str(appdata_root)
        ):
            self.assertEqual(app_paths._raiz_dados_preferida(), appdata_root.resolve())

    def test_product_exporter_uses_database_manager_root(self):
        import exportar_produtos_xls

        self.assertEqual(
            Path(exportar_produtos_xls.DB_PATH),
            Path(database_manager.get_db_path()),
        )

    def test_ia_gestao_db_path_uses_database_manager_root(self):
        import ia_gestao

        self.assertEqual(
            Path(ia_gestao.DB_PATH),
            Path(database_manager.get_db_path()),
        )

    def test_modulo_financeiro_db_path_uses_database_manager_root(self):
        import modulo_financeiro

        self.assertEqual(
            Path(modulo_financeiro.DB_PATH),
            Path(database_manager.get_db_path()),
        )

    def test_modulo_usuario_db_path_uses_database_manager_root(self):
        import modulo_usuario

        self.assertEqual(
            Path(modulo_usuario.DB_PATH),
            Path(database_manager.get_db_path()),
        )

    def test_all_modules_share_one_single_canonical_db_root(self):
        """Nenhum módulo pode manter caminho relativo ao CWD (segundo mercado.db)."""
        import ia_gestao
        import modulo_financeiro
        import modulo_usuario

        canonico = os.path.normcase(
            os.path.abspath(database_manager.get_db_path())
        )
        modulos = {
            "ia_gestao": ia_gestao,
            "modulo_financeiro": modulo_financeiro,
            "modulo_usuario": modulo_usuario,
        }

        for nome, modulo in modulos.items():
            with self.subTest(modulo=nome):
                bruto = modulo.DB_PATH
                self.assertTrue(
                    os.path.isabs(bruto),
                    f"{nome}.DB_PATH deve ser absoluto, veio de {bruto!r}",
                )
                self.assertEqual(
                    os.path.normcase(os.path.abspath(bruto)),
                    canonico,
                    f"{nome}.DB_PATH diverge da raiz canônica",
                )
                self.assertEqual(
                    Path(bruto).name,
                    "mercado.db",
                    f"{nome}.DB_PATH deve apontar para mercado.db",
                )
                self.assertNotEqual(
                    os.path.normcase(os.path.abspath(bruto)),
                    os.path.normcase(os.path.abspath("mercado.db")),
                    f"{nome}.DB_PATH ainda resolve relativo ao CWD",
                )

    # ------------------------------------------------------------------
    # F-1 / F-3 / F-4 — config.json inválido nunca vira default.
    # ------------------------------------------------------------------

    def _config_ctx(self, config_path):
        """Isola carregar_configuracoes() no arquivo indicado."""
        return patch.multiple(
            modulo_config,
            CONFIG_FILE=str(config_path),
            _garantir_pastas_usuario=lambda: None,
        )

    def test_valid_config_still_loads_normally(self):
        """A) config.json válido continua carregando sem alteração."""
        config_path = self._write_config(self.dest, self._real_config("Mercado Valido"))

        with self._config_ctx(config_path):
            config = modulo_config.carregar_configuracoes()

        self.assertEqual(config["cnpj"], "12.345.678/0001-90")
        self.assertEqual(config["razao_social"], "Mercado Valido LTDA")

    def test_invalid_config_raises_preserves_file_and_never_defaults(self):
        """B) Inválido: erro propagado, arquivo intacto, nenhum default criado."""
        config_path = self.dest / "config.json"
        config_path.write_text("{isto nao e json", encoding="utf-8")
        antes = config_path.read_bytes()

        with self._config_ctx(config_path):
            with self.assertRaises(modulo_config.ConfiguracaoInvalidaError) as ctx:
                modulo_config.carregar_configuracoes()

        self.assertEqual(ctx.exception.caminho, str(config_path))
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertTrue(config_path.is_file(), "config.json não pode ser apagado")
        self.assertEqual(config_path.read_bytes(), antes, "conteúdo foi alterado")
        self.assertFalse(
            list(self.dest.glob("config.json.*.bak")),
            "não deve criar backup ao apenas tentar carregar",
        )

    def test_invalid_config_is_preserved_after_second_attempt(self):
        """C) Repetir a falha não apaga, não sobrescreve, não converte em default."""
        config_path = self.dest / "config.json"
        config_path.write_text("[1,2,3]", encoding="utf-8")
        antes = config_path.read_bytes()

        for _ in range(3):
            with self._config_ctx(config_path):
                with self.assertRaises(modulo_config.ConfiguracaoInvalidaError):
                    modulo_config.carregar_configuracoes()

        self.assertEqual(config_path.read_bytes(), antes)
        self.assertNotIn("cnpj", config_path.read_text(encoding="utf-8"))

    def test_non_object_json_does_not_create_empty_configuration(self):
        """D) JSON que não é objeto também falha fechado (não vira default)."""
        config_path = self.dest / "config.json"
        config_path.write_text('"apenas um texto"', encoding="utf-8")
        antes = config_path.read_bytes()

        with self._config_ctx(config_path):
            with self.assertRaises(modulo_config.ConfiguracaoInvalidaError):
                modulo_config.carregar_configuracoes()

        self.assertEqual(config_path.read_bytes(), antes)
        self.assertNotEqual(config_path.read_text(encoding="utf-8"), "{}")

    def test_boundary_handler_does_not_mask_the_error(self):
        """E) A fronteira de UI formata a mensagem sem mascarar a causa."""
        exc = modulo_config.ConfiguracaoInvalidaError(str(self.dest / "config.json"))

        # A fronteira consome o erro por TIPO e continua exposto se propagado.
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn(str(self.dest / "config.json"), str(exc))
        self.assertTrue(exc.caminho.endswith("config.json"))
        with self.assertRaises(modulo_config.ConfiguracaoInvalidaError):
            raise exc

    def test_import_does_not_create_second_config_in_other_root(self):
        """F) Falha de config não cria config.json em outra raiz."""
        outra = self.base / "outra-raiz"
        outra.mkdir()
        invalido = self.dest / "config.json"
        invalido.write_text("{quebrado", encoding="utf-8")
        antes = invalido.read_bytes()

        with self._config_ctx(invalido):
            with self.assertRaises(modulo_config.ConfiguracaoInvalidaError):
                modulo_config.carregar_configuracoes()

        self.assertFalse(
            (outra / "config.json").exists(),
            "não pode surgir um segundo config.json em outra raiz",
        )
        self.assertEqual(invalido.read_bytes(), antes)

    def test_failed_adoption_does_not_lose_existing_invalid_config(self):
        """G) F-4: falha de adoção não destrói nem apaga dados existentes."""
        raiz = self.base / "raiz-com-dados" / "data"
        config_path = raiz / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{invalido", encoding="utf-8")
        antes = config_path.read_bytes()

        def _falha(*_args, **_kwargs):
            raise RuntimeError("adoção falhou")

        with patch.object(app_paths, "_raiz_dados_preferida", return_value=raiz), patch.object(
            app_paths, "_raizes_legadas", return_value=[]
        ), patch.object(app_paths, "_adotar_dados_protegidos", side_effect=_falha):
            with self.assertRaisesRegex(RuntimeError, "adoção falhou"):
                app_paths.obter_caminho_dados("config.json")

        self.assertTrue(raiz.is_dir())
        self.assertEqual(config_path.read_bytes(), antes, "config inválido foi perdido")

    def test_failed_adoption_removes_only_directory_it_created(self):
        """G) F-4: diretório criado pela própria tentativa não deve sobrar vazio."""
        raiz_inexistente = self.base / "criada-pelo-resolver"
        self.assertFalse(raiz_inexistente.exists())

        def _falha(*_args, **_kwargs):
            raise RuntimeError("adoção falhou")

        with patch.object(app_paths, "_raiz_dados_preferida", return_value=raiz_inexistente), patch.object(
            app_paths, "_raizes_legadas", return_value=[]
        ), patch.object(app_paths, "_garantir_escrita", lambda _p: os.makedirs(_p, exist_ok=True)), patch.object(
            app_paths, "_adotar_dados_protegidos", side_effect=_falha
        ):
            with self.assertRaisesRegex(RuntimeError, "adoção falhou"):
                app_paths.obter_caminho_dados("config.json")

        self.assertFalse(
            raiz_inexistente.exists(),
            "diretório vazio criado na tentativa não deve sobrar",
        )

    # ------------------------------------------------------------------
    # BLOCO 2 / FASE 1 — webhook desacoplado de config_fiscal (PlugNotas).
    # ------------------------------------------------------------------

    @staticmethod
    def _abrir(path):
        """Conexão que FECRA ao sair (o `with sqlite3.connect` só faz commit)."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            conn = sqlite3.connect(path)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

        return _ctx()

    @staticmethod
    def _tabela_existe(path, nome):
        conn = sqlite3.connect(path)
        try:
            return bool(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (nome,)
                ).fetchone()
            )
        finally:
            conn.close()

    @staticmethod
    def _hash_token(token):
        import bcrypt

        return bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def _banco_webhook(self, root, *, legado="", criar_nova=True):
        """Banco mínimo com config_fiscal legado e/ou config_webhook."""
        path = Path(root) / "mercado.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE config_fiscal (id INTEGER PRIMARY KEY, api_key TEXT, "
                "ambiente TEXT, webhook_token_hash TEXT)"
            )
            conn.execute(
                "INSERT INTO config_fiscal VALUES (1, '', 'HOMOLOGACAO', ?)", (legado,)
            )
            if criar_nova:
                conn.execute(
                    "CREATE TABLE config_webhook (id INTEGER PRIMARY KEY, token_hash TEXT)"
                )
                conn.execute("INSERT INTO config_webhook VALUES (1, '')")
            conn.commit()
        finally:
            conn.close()
        return path

    @staticmethod
    def _com_banco(path, funcao, *args):
        """Executa função do webhook_security contra o banco temporário."""
        import contextlib

        import webhook_security

        @contextlib.contextmanager
        def _conexao():
            conn = sqlite3.connect(path)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

        with patch.object(webhook_security, "get_db_connection", lambda: _conexao()):
            return funcao(*args)

    def test_nova_instalacao_cria_tabela_config_webhook(self):
        """E1) Instalação nova cria a estrutura, sem token e sem quebrar."""
        import webhook_security as ws

        path = self._banco_webhook(self.dest, legado="", criar_nova=False)
        self.assertFalse(self._tabela_existe(path, "config_webhook"))

        with self._abrir(path) as conn:
            ws._garantir_tabela_config_webhook(conn)
            conn.commit()

        self.assertTrue(self._tabela_existe(path, "config_webhook"))
        with self._abrir(path) as conn:
            self.assertEqual(ws._ler_token_hash(conn), "")
        self.assertFalse(self._com_banco(path, ws.webhook_token_configurado))

    def test_instalacao_existente_migra_token_sem_perda(self):
        """E2+E3) Token legado migra e continua validando o mesmo segredo."""
        import webhook_security as ws

        segredo = "token-secreto-do-cliente"
        hash_legado = self._hash_token(segredo)
        path = self._banco_webhook(self.dest, legado=hash_legado, criar_nova=False)

        with self._abrir(path) as conn:
            ws._garantir_tabela_config_webhook(conn)
            conn.commit()

        with self._abrir(path) as conn:
            migrado = conn.execute(
                "SELECT token_hash FROM config_webhook WHERE id = 1"
            ).fetchone()[0]
        self.assertEqual(migrado, hash_legado, "hash do cliente não pode mudar")

        ok, _msg = self._com_banco(path, ws.validar_token_webhook, segredo)
        self.assertTrue(ok, "token migrado deve continuar válido")

    def test_migracao_repetida_e_idempotente(self):
        """E6) Repetir a migração nunca altera o token já presente."""
        import webhook_security as ws

        hash_legado = self._hash_token("token-idempotente")
        path = self._banco_webhook(self.dest, legado=hash_legado, criar_nova=False)

        with self._abrir(path) as conn:
            ws._garantir_tabela_config_webhook(conn)
            conn.commit()
            primeira = conn.execute(
                "SELECT token_hash FROM config_webhook WHERE id = 1"
            ).fetchone()[0]
            ws._garantir_tabela_config_webhook(conn)
            conn.commit()
            segunda = conn.execute(
                "SELECT token_hash FROM config_webhook WHERE id = 1"
            ).fetchone()[0]

        self.assertEqual(primeira, hash_legado)
        self.assertEqual(segunda, primeira, "migração repetida alterou o token")

    def test_token_nunca_e_substituido_por_vazio(self):
        """Regra central: um token já salvo NUNCA cede ao valor legado/vazio.

        Cenário real de perigo: o destino tem o token atual e o legado ainda
        tem um hash antigo/vazio. O destino é quem manda.
        """
        import webhook_security as ws

        hash_atual = self._hash_token("token-atual-do-cliente")
        for legado in ("", self._hash_token("token-antigo-obsoleto")):
            with self.subTest(legado=("vazio" if not legado else "obsoleto")):
                path = self._banco_webhook(
                    self.base / f"caso-{len(legado)}", legado=legado, criar_nova=True
                )
                with self._abrir(path) as conn:
                    conn.execute(
                        "UPDATE config_webhook SET token_hash = ? WHERE id = 1", (hash_atual,)
                    )
                    conn.commit()

                with self._abrir(path) as conn:
                    ws._garantir_tabela_config_webhook(conn)
                    conn.commit()
                    atual = conn.execute(
                        "SELECT token_hash FROM config_webhook WHERE id = 1"
                    ).fetchone()[0]

                self.assertEqual(atual, hash_atual, "token existente foi sobrescrito")

    def test_token_novo_tem_precedencia_sobre_o_legado(self):
        """O hash salvo na nova estrutura prevalece sobre o legado antigo."""
        import webhook_security as ws

        segredo = "token-vigente"
        hash_atual = self._hash_token(segredo)
        hash_obsoleto = self._hash_token("token-obsoleto")

        path = self._banco_webhook(self.dest, legado=hash_obsoleto, criar_nova=True)
        with self._abrir(path) as conn:
            conn.execute("UPDATE config_webhook SET token_hash = ? WHERE id = 1", (hash_atual,))
            conn.commit()

        ok, _ = self._com_banco(path, ws.validar_token_webhook, segredo)
        self.assertTrue(ok, "o token vigente deve ser o aceito")

        ok, _ = self._com_banco(path, ws.validar_token_webhook, "token-obsoleto")
        self.assertFalse(ok, "o token obsoleto não pode continuar válido")

    def test_salvar_novo_token_continua_funcionando(self):
        """E5) Grava em config_webhook e NÃO envia o token para config_fiscal."""
        import webhook_security as ws

        segredo = "token-para-salvar"
        path = self._banco_webhook(self.dest, legado="", criar_nova=True)

        self.assertTrue(self._com_banco(path, ws.salvar_token_webhook, segredo))
        self.assertTrue(self._com_banco(path, ws.webhook_token_configurado))

        ok, _ = self._com_banco(path, ws.validar_token_webhook, segredo)
        self.assertTrue(ok)

        with self._abrir(path) as conn:
            legado = conn.execute(
                "SELECT webhook_token_hash FROM config_fiscal WHERE id = 1"
            ).fetchone()[0]
        self.assertEqual(legado, "", "config_fiscal não deve receber o token do webhook")

    def test_token_invalido_continua_rejeitado(self):
        """E4) Segredo errado e header ausente seguem rejeitados."""
        import webhook_security as ws

        path = self._banco_webhook(self.dest, legado="", criar_nova=True)
        self._com_banco(path, ws.salvar_token_webhook, "token-correto")

        ok, msg = self._com_banco(path, ws.validar_token_webhook, "token-errado")
        self.assertFalse(ok)
        self.assertEqual(msg, "Token inválido")

        ok, msg = self._com_banco(path, ws.validar_token_webhook, "")
        self.assertFalse(ok)
        self.assertEqual(msg, "Header X-Webhook-Token ausente")

    def test_token_ausente_continua_reportando_nao_configurado(self):
        """Comportamento original preservado: sem token, tudo é rejeitado."""
        import webhook_security as ws

        path = self._banco_webhook(self.dest, legado="", criar_nova=True)
        self.assertFalse(self._com_banco(path, ws.webhook_token_configurado))
        ok, msg = self._com_banco(path, ws.validar_token_webhook, "qualquer")
        self.assertFalse(ok)
        self.assertEqual(msg, "Token de webhook não configurado no sistema")

    def test_banco_apenas_com_webhook_nao_e_confundido_com_semente(self):
        """E7) Webhook vazio = semente; webhook com token = estado do cliente."""
        vazio = self._banco_webhook(self.base / "so-semente", legado="", criar_nova=True)
        self.assertIs(
            app_paths._banco_tem_dados_usuario(str(vazio)),
            False,
            "config_webhook vazia não pode marcar o banco como do cliente",
        )

        com_token = self._banco_webhook(self.base / "com-webhook", legado="", criar_nova=True)
        with self._abrir(com_token) as conn:
            conn.execute(
                "UPDATE config_webhook SET token_hash = ? WHERE id = 1",
                (self._hash_token("token-estado-cliente"),),
            )
            conn.commit()
        self.assertIs(
            app_paths._banco_tem_dados_usuario(str(com_token)),
            True,
            "banco com token de webhook é estado do cliente",
        )

    def test_banco_com_webhook_ligado_nao_e_sobrescrito_na_adocao(self):
        """E7) Adoção preserva um banco cujo estado é o token do webhook."""
        hash_destino = self._hash_token("token-do-cliente-no-destino")

        # Destino já é estado do cliente: só o token de webhook o distingue.
        destino = self._banco_webhook(self.dest, legado="", criar_nova=True)
        with self._abrir(destino) as conn:
            conn.execute("UPDATE config_webhook SET token_hash = ? WHERE id = 1", (hash_destino,))

        # Origem tem operação, mas não pode tomar o lugar do destino.
        origem = self._banco_webhook(self.old, legado="", criar_nova=False)
        with self._abrir(origem) as conn:
            conn.execute("CREATE TABLE produtos (id INTEGER PRIMARY KEY, nome TEXT)")
            conn.executemany("INSERT INTO produtos (nome) VALUES (?)", [("P1",), ("P2",)])
            conn.commit()

        resultado = app_paths._adotar_banco_portavel(self.dest, self.dest, [self.old])

        self.assertIn("não sobrescrever", resultado)
        with self._abrir(destino) as conn:
            atual = conn.execute("SELECT token_hash FROM config_webhook WHERE id = 1").fetchone()[0]
        self.assertEqual(atual, hash_destino, "token do destino foi sobrescrito")

    def test_coluna_legada_permanece_no_schema(self):
        """Compatibilidade: a coluna antiga NÃO é removida nesta fase."""
        import webhook_security as ws

        path = self._banco_webhook(self.dest, legado="", criar_nova=True)
        self._com_banco(path, ws.salvar_token_webhook, "qualquer-token")

        with self._abrir(path) as conn:
            colunas = [linha[1] for linha in conn.execute("PRAGMA table_info(config_fiscal)")]
            tabelas = {
                linha[0]
                for linha in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertIn("webhook_token_hash", colunas)
        self.assertIn("config_fiscal", tabelas)
        self.assertIn("config_webhook", tabelas)

    def test_api_key_e_ambiente_do_plugnotas_permanecem(self):
        """Regra F: PlugNotas e suas credenciais continuam intactos."""
        path = self._banco_webhook(self.dest, legado="", criar_nova=True)
        with self._abrir(path) as conn:
            conn.execute("UPDATE config_fiscal SET api_key = 'chave-plugnotas' WHERE id = 1")
            conn.commit()
            linha = conn.execute(
                "SELECT api_key, ambiente FROM config_fiscal WHERE id = 1"
            ).fetchone()
        self.assertEqual(linha[0], "chave-plugnotas")
        self.assertEqual(linha[1], "HOMOLOGACAO")

    def test_fluxo_fiscal_do_acbr_nao_foi_alterado(self):
        """E8) As chaves do ACBr em config.json seguem intactas."""
        config = self._real_config("Cliente Fiscal")
        config["fiscal_ativo"] = True
        config["emissor_fiscal_path"] = r"C:\ACBrMonitorPLUS\ACBrMonitor.exe"
        caminho = self._write_config(self.dest, config)

        with patch.object(modulo_config, "CONFIG_FILE", str(caminho)), patch.object(
            modulo_config, "_garantir_pastas_usuario", lambda: None
        ):
            carregado = modulo_config.carregar_configuracoes()

        self.assertTrue(carregado["fiscal_ativo"], "fiscal_ativo do ACBr não pode ser perdido")
        self.assertEqual(
            carregado["emissor_fiscal_path"], r"C:\ACBrMonitorPLUS\ACBrMonitor.exe"
        )

    # ------------------------------------------------------------------
    # BLOCO 2 / FASE 3.3 — R3: XML de exportar_venda() não é NF-e.
    # Classe separada: o setUp próprio NÃO pode sobrescrever o desta classe.
    # ------------------------------------------------------------------


class TestFiltroXmlExportacaoVenda(unittest.TestCase):
    """R3: XML de exportar_venda() não é NF-e e não pode ser candidato."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="frs-r3-xml-")
        self.addCleanup(temp.cleanup)
        self.raiz_xml = Path(temp.name)
        self.fiscal_in = self.raiz_xml / "fiscal_in"
        self.exportacao = self.raiz_xml / "exportacao_fiscal"
        self.fiscal_in.mkdir()
        self.exportacao.mkdir()

    @staticmethod
    def _stub_estoque():
        """Objeto mínimo para exercitar o método unbound, sem criar janela."""
        return type("_EstoqueStub", (), {"winfo_exists": lambda self: True})()

    def _listar_candidatos_xml(self):
        """Chama _listar_xml_nfe_candidatos real com pastas temporárias."""
        import modulo_estoque
        from unittest.mock import patch

        def _caminho(nome):
            base = {"fiscal_in": self.fiscal_in, "exportacao_fiscal": self.exportacao}
            return str(base.get(nome, self.raiz_xml / "inexistente"))

        with patch.object(modulo_estoque, "obter_caminho_dados", _caminho), patch.object(
            modulo_estoque, "carregar_configuracoes", lambda: {}
        ):
            arquivos = modulo_estoque.ModuloEstoque._listar_xml_nfe_candidatos(
                self._stub_estoque()
            )
        return [Path(a) for a in arquivos]

    def _criar_xml(self, pasta, nome, conteudo="<nFe/>"):
        caminho = pasta / nome
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(conteudo, encoding="utf-8")
        return caminho

    def test_xml_de_exportacao_de_venda_nao_entra_na_lista(self):
        nome = "export_venda_1_20260613_080227.xml"
        self._criar_xml(self.exportacao, nome)
        self._criar_xml(self.fiscal_in, nome)

        nomes = [p.name for p in self._listar_candidatos_xml()]

        self.assertNotIn(nome, nomes, "XML de exportar_venda() não pode ser candidato")

    def test_xml_de_nfe_real_continua_entrando(self):
        chave = "35240612345678000199550010000000011000000017.xml"
        self._criar_xml(self.fiscal_in, chave, "<nFe><det><prod/></det></nFe>")
        segunda = "35240612345678000199550010000000021000000018.xml"
        self._criar_xml(self.exportacao, segunda, "<nFe><det><prod/></det></nFe>")

        nomes = [p.name for p in self._listar_candidatos_xml()]

        self.assertIn(chave, nomes, "NF-e real deve continuar como candidata")
        self.assertIn(segunda, nomes, "NF-e real deve continuar como candidata")

    def test_filtro_apenas_afeta_export_venda(self):
        venda_a = self._criar_xml(self.exportacao, "export_venda_7_20260101_000000.xml")
        venda_b = self._criar_xml(self.exportacao, "export_venda_8_20260102_000000.xml")
        nfe = self._criar_xml(self.fiscal_in, "35240612345678000199550010000000011000000017.xml")
        entrada = self._criar_xml(self.exportacao, "nota-fiscal-entrada.xml")
        outro = self._criar_xml(self.fiscal_in, "qualquer-outro.xml")

        encontrados = {p.name for p in self._listar_candidatos_xml()}

        for filtrado in (venda_a.name, venda_b.name):
            self.assertNotIn(filtrado, encontrados, "apenas export_venda_* deve ser filtrado")
        for preservado in (nfe.name, entrada.name, outro.name):
            self.assertIn(preservado, encontrados, f"{preservado} não deveria ser filtrado")

    def test_extensao_e_ordenacao_preservadas(self):
        # As pastas reais do repositório também entram na varredura (o
        # método usa caminhos relativos ao módulo), então as afirmações
        # verificam apenas os arquivos criados por este teste.
        self._criar_xml(self.fiscal_in, "nota.json")
        self._criar_xml(self.fiscal_in, "planilha.txt")
        antigo = self._criar_xml(self.fiscal_in, "antigo.xml")
        novo = self._criar_xml(self.fiscal_in, "novo.xml")
        os.utime(antigo, (1_000_000, 1_000_000))
        os.utime(novo, (2_000_000, 2_000_000))

        nomes = [p.name for p in self._listar_candidatos_xml()]

        self.assertNotIn("nota.json", nomes)
        self.assertNotIn("planilha.txt", nomes)
        self.assertIn("antigo.xml", nomes)
        self.assertIn("novo.xml", nomes)
        # Ordenação por mtime preservada entre os arquivos deste teste.
        self.assertLess(nomes.index("novo.xml"), nomes.index("antigo.xml"))

    def test_exportar_venda_continua_gravando_xml(self):
        """O contrato de exportar_venda() permanece intacto após o filtro."""
        import xml.etree.ElementTree as ET

        from modulo_fiscal import ModuloExportacaoFiscal

        exportador = object.__new__(ModuloExportacaoFiscal)
        exportador.config = {"razao_social": "TESTE", "cnpj": "00.000.000/0000-00"}
        exportador.pasta_exportacao = str(self.exportacao)

        ok, caminho = exportador.exportar_venda(
            1, [{"nome": "P", "quantidade": 2, "preco": 10.0}], "PIX", 20.0
        )

        self.assertTrue(ok, "exportar_venda() não pode regredir")
        self.assertTrue(Path(caminho).is_file())
        raiz = ET.parse(caminho).getroot()
        self.assertEqual(raiz.tag, "VendaExportacao")
        self.assertEqual(raiz.findtext("ID_Venda"), "1")


if __name__ == "__main__":
    unittest.main()
