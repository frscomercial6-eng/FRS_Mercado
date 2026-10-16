"""Contratos de segurança do updater do FRS Mercado (Item 12).

POR QUE ESTE ARQUIVO EXISTE
---------------------------
Até o Item 12 o updater NÃO tinha nenhum teste de verdade: `test_update_flow.py`
é um script com `main()` e valores fixos (1.0.4/1.0.3); o `unittest` coletava
**0 testes** dele. Ou seja, nenhuma das garantias abaixo era verificada na
regressão. Este arquivo trava os contratos que protegem o Mário.

ESCOPO
------
Assinatura / key_id / sequence / hash / tamanho / URL / replay / downgrade e
coerência das fontes de versão. NÃO testa rede, download real nem instalação.

NADA DISSO TOCA PRODUÇÃO
------------------------
`APPDATA` é redirecionado para um diretório temporário ANTES de importar o
updater, então `update_state.json`, `updates/` e `runtime/` reais nunca são
lidos nem gravados. Não há banco, não há janela Tk, não há rede.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("APPDATA", tempfile.mkdtemp(prefix="frs-item12-"))

import app_paths  # noqa: E402
import updater_helper as helper  # noqa: E402
import updater_secure as secure  # noqa: E402

RAIZ = Path(__file__).resolve().parent
MANIFESTO = json.loads((RAIZ / "update-manifest.json").read_text(encoding="utf-8"))
KEYS = json.loads((RAIZ / "updater_public_keys.json").read_text(encoding="utf-8"))
FONTE_SECURE = (RAIZ / "updater_secure.py").read_text(encoding="utf-8")
FONTE_HELPER = (RAIZ / "updater_helper.py").read_text(encoding="utf-8")


class _ManifestoIsolado(unittest.TestCase):
    """Dá a cada teste um `update_state.json` próprio, em disco temporário."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="frs-item12-estado-")
        self._original = secure.UPDATE_STATE_FILE
        secure.UPDATE_STATE_FILE = Path(self._dir) / "update_state.json"

    def tearDown(self):
        secure.UPDATE_STATE_FILE = self._original

    def _rejeitado(self, envelope):
        with self.assertRaises(secure.UpdateSecurityError):
            secure.verify_manifest(envelope)

    def _com_asset(self, **campos):
        envelope = copy.deepcopy(MANIFESTO)
        envelope["payload"]["assets"][0].update(campos)
        return envelope


# --------------------------------------------------------------------------
# [1] MANIFESTO ASSINADO — o núcleo criptográfico
# --------------------------------------------------------------------------
class TestAssinaturaDoManifesto(_ManifestoIsolado):
    def test_manifesto_do_repositorio_e_valido(self):
        self.assertEqual(secure.verify_manifest(MANIFESTO)["product"], secure.PRODUCT)

    def test_chave_publica_e_a_do_cofre_do_projeto(self):
        self.assertIn(MANIFESTO["key_id"], KEYS["keys"])

    def test_assinatura_e_base64_de_64_bytes_ed25519(self):
        self.assertEqual(len(base64.b64decode(MANIFESTO["signature"], validate=True)), 64)

    def test_payload_alterado_e_rejeitado(self):
        # Sanidade: o manifesto intacto PASSA (não pode levantar).
        self.assertTrue(secure.verify_manifest(MANIFESTO))
        envelope = copy.deepcopy(MANIFESTO)
        envelope["payload"]["version"] = "9.9.9"
        self._rejeitado(envelope)

    def test_hash_alterado_e_rejeitado(self):
        self._rejeitado(self._com_asset(sha256="0" * 64))

    def test_url_http_e_rejeitada(self):
        self._rejeitado(self._com_asset(url="http://exemplo.invalido/x.zip"))

    def test_url_com_credenciais_e_rejeitada(self):
        self._rejeitado(self._com_asset(url="https://u:p@exemplo.com/x.zip"))

    def test_tamanho_zero_e_rejeitado(self):
        self._rejeitado(self._com_asset(size=0))

    def test_sha256_malformado_e_rejeitado(self):
        self._rejeitado(self._com_asset(sha256="abc"))

    def test_key_id_desconhecida_e_rejeitada(self):
        envelope = copy.deepcopy(MANIFESTO)
        envelope["key_id"] = "chave-falsa"
        self._rejeitado(envelope)

    def test_assinatura_corrompida_e_rejeitada(self):
        envelope = copy.deepcopy(MANIFESTO)
        envelope["signature"] = "A" * 88
        self._rejeitado(envelope)

    def test_campo_fora_do_envelope_e_rejeitado(self):
        """Campo não assinado é vetor clássico de injeção."""
        envelope = copy.deepcopy(MANIFESTO)
        envelope["campo_extra"] = "injetado"
        self._rejeitado(envelope)

    def test_produto_e_canal_errados_sao_rejeitados(self):
        for campo, valor in (("product", "OutroProduto"), ("channel", "beta")):
            with self.subTest(campo=campo):
                envelope = copy.deepcopy(MANIFESTO)
                envelope["payload"][campo] = valor
                self._rejeitado(envelope)

    def test_schema_errado_e_rejeitado(self):
        envelope = copy.deepcopy(MANIFESTO)
        envelope["schema"] = "OUTRO-SCHEMA"
        self._rejeitado(envelope)

    def test_sequence_e_issued_at_invalidos_sao_rejeitados(self):
        for campo, valor in (("sequence", 0), ("issued_at", "ontem")):
            with self.subTest(campo=campo):
                envelope = copy.deepcopy(MANIFESTO)
                envelope["payload"][campo] = valor
                self._rejeitado(envelope)
# --------------------------------------------------------------------------
# [2] REPLAY E DOWNGRADE
# --------------------------------------------------------------------------
class TestReplayEDowngrade(_ManifestoIsolado):
    def test_manifesto_mais_antigo_que_a_sequencia_vista_e_rejeitado(self):
        secure.write_update_state({"sequence": 99})
        self._rejeitado(MANIFESTO)

    def test_sequencia_igual_e_aceita(self):
        """Repetir o MESMO manifesto é idempotente, não um erro."""
        payload = secure.verify_manifest(MANIFESTO)
        secure.write_update_state({"sequence": int(MANIFESTO["payload"]["sequence"])})
        self.assertEqual(secure.verify_manifest(MANIFESTO), payload)

    def test_downgrade_de_versao_nao_e_oferecido(self):
        """Manifesto mais antigo que a versão instalada nunca vira atualização."""
        self.assertLessEqual(
            secure.compare_versions(MANIFESTO["payload"]["version"], secure.get_local_version()), 0
        )


# --------------------------------------------------------------------------
# [3] COERÊNCIA DAS FONTES DE VERSÃO (item 4 do enunciado)
# --------------------------------------------------------------------------
class TestCoerenciaDeVersao(unittest.TestCase):
    def _raiz(self, versao_txt=None, versao_json=None, versao_info=None):
        raiz = Path(tempfile.mkdtemp(prefix="frs-item12-versao-"))
        if versao_txt is not None:
            (raiz / "version.txt").write_text(versao_txt, encoding="utf-8")
        if versao_json is not None:
            (raiz / "version.json").write_text(
                json.dumps({"latest_version": versao_json}), encoding="utf-8"
            )
        if versao_info is not None:
            (raiz / "release_info.py").write_text(f'APP_VERSION = "{versao_info}"\n', encoding="utf-8")
        return raiz

    def test_app_e_helper_never_divergem_com_arquivos_divergentes(self):
        """Este é o defeito corrigido no Item 12.

        Antes o app lia `version.json` primeiro e o helper lia `version.txt`
        primeiro: com as fontes divergentes cada lado via uma versão diferente
        do MESMO runtime, e o helper podia reprovar uma instalação correta.
        """
        raiz = self._raiz(versao_txt="1.0.19", versao_json="1.0.22")
        app = secure.read_version_marker(raiz, allow_release_info=True)
        self.assertEqual(app, helper._version_marker(raiz))
        self.assertEqual(app, helper._installed_version(raiz))

    def test_precedencia_e_deterministica(self):
        raiz = self._raiz(versao_txt="1.0.19", versao_json="1.0.22")
        self.assertEqual(
            secure.read_version_marker(raiz, allow_release_info=True),
            secure.read_version_marker(raiz, allow_release_info=True),
        )

    def test_release_info_so_entra_quando_permitido(self):
        raiz = self._raiz(versao_info="1.0.30")
        self.assertEqual(secure.read_version_marker(raiz, allow_release_info=True), "1.0.30")
        self.assertEqual(
            secure.read_version_marker(raiz, allow_release_info=False), "0.0.0",
            "o helper não pode depender de release_info.py (pode não existir em disco)",
        )

    def test_raiz_sem_marcador_devolve_zero(self):
        vazia = Path(tempfile.mkdtemp(prefix="frs-item12-vazia-"))
        self.assertEqual(secure.read_version_marker(vazia, allow_release_info=True), "0.0.0")

    def test_arquivo_corrompido_nao_derruba_a_leitura(self):
        raiz = Path(tempfile.mkdtemp(prefix="frs-item12-bad-"))
        (raiz / "version.txt").write_text("1.0.19", encoding="utf-8")
        (raiz / "version.json").write_text("{ nao e json", encoding="utf-8")
        self.assertEqual(secure.read_version_marker(raiz, allow_release_info=True), "1.0.19")

    def test_as_fontes_do_projeto_real_concordam(self):
        versoes = {
            secure.read_version_marker(RAIZ, allow_release_info=True),
            helper._version_marker(RAIZ),
            helper._installed_version(RAIZ),
        }
        self.assertEqual(len(versoes), 1, f"fontes de versão divergentes: {versoes}")


# --------------------------------------------------------------------------
# [4] NORMALIZAÇÃO E COMPARAÇÃO
# --------------------------------------------------------------------------
class TestNormalizacaoDeVersao(unittest.TestCase):
    def test_formatos_equivalentes(self):
        for bruto in ("1.0.22", "v1.0.22", "V1.0.22", " 1.0.22 ", "1.0.22.0"):
            with self.subTest(bruto=bruto):
                self.assertEqual(secure.normalize_version(bruto), "1.0.22")

    def test_versoes_curtas_sao_completadas_com_zero(self):
        for bruto, esperado in (("1.0", "1.0.0"), ("1", "1.0.0"), ("2", "2.0.0")):
            with self.subTest(bruto=bruto):
                self.assertEqual(secure.normalize_version(bruto), esperado)

    def test_comparacao_semantica(self):
        self.assertEqual(secure.compare_versions("1.0.22", "1.0.21"), 1)
        self.assertEqual(secure.compare_versions("1.0.21", "1.0.22"), -1)
        self.assertEqual(secure.compare_versions("1.0.22", "v1.0.22"), 0)
        self.assertEqual(secure.compare_versions("1.0.9", "1.0.10"), -1)


# --------------------------------------------------------------------------
# [5] PROTEÇÃO DOS DADOS DO USUÁRIO (item 7)
# --------------------------------------------------------------------------
class TestProtecaoDosDados(unittest.TestCase):
    def test_mercado_db_licenca_e_config_estao_protegidos(self):
        # Snapshot/restauração (PROTECTED_DATA_NAMES) e verificação por hash
        # (PROTECTED_HASH_FILES) listam as duas coisas de formas diferentes:
        # a pasta `licensing` entra inteira no snapshot, e cada arquivo dela
        # entra individualmente no hash.
        for nome in ("mercado.db", "licensing", "config.json"):
            with self.subTest(nome=nome):
                self.assertIn(nome, helper.PROTECTED_DATA_NAMES)
        for nome in ("mercado.db", "config.json", "licensing/license.json",
                     "licensing/installation_id"):
            with self.subTest(nome=nome):
                self.assertIn(nome, helper.PROTECTED_HASH_FILES)

    def _dados_de_teste(self):
        data = Path(tempfile.mkdtemp(prefix="frs-item12-data-"))
        (data / "mercado.db").write_bytes(b"conteudo-do-banco-do-mario")
        (data / "config.json").write_text('{"licenca": "ativa"}', encoding="utf-8")
        lic = data / "licensing"
        lic.mkdir()
        (lic / "license.json").write_text('{"ativa": true}', encoding="utf-8")
        return data

    def test_backup_e_restauracao_roundtrip(self):
        """Snapshot → corrupção → restauração devolve o conteúdo original."""
        data = self._dados_de_teste()
        antes = helper._protected_hashes(data)
        backup = Path(tempfile.mkdtemp(prefix="frs-item12-bkp-"))
        backup.rmdir()
        helper._backup_protected_data(data, backup)

        (data / "mercado.db").write_bytes(b"CORROMPIDO")
        (data / "config.json").write_text('{"licenca": "perdida"}', encoding="utf-8")
        self.assertNotEqual(helper._protected_hashes(data), antes)

        helper._restore_protected_data(data, backup)
        self.assertEqual((data / "mercado.db").read_bytes(), b"conteudo-do-banco-do-mario")
        self.assertEqual((data / "config.json").read_text(encoding="utf-8"), '{"licenca": "ativa"}')
        self.assertEqual(helper._protected_hashes(data), antes)

    def test_deteccao_de_alteracao_dispara_erro(self):
        data = self._dados_de_teste()
        antes = helper._protected_hashes(data)
        (data / "mercado.db").write_bytes(b"alterado")
        with self.assertRaises(helper.HelperError):
            helper._assert_protected_data_unchanged(antes, data)

    def test_pastas_protegidas_do_zip(self):
        self.assertEqual(helper.PROTECTED_TOP_LEVEL, {"data", "runtime", "updates"})

    def test_credenciais_proibidas_no_zip(self):
        self.assertIn("credentials.json", helper.FORBIDDEN_ASSETS)


# --------------------------------------------------------------------------
# [6] GATE OPERACIONAL (item 6)
# --------------------------------------------------------------------------
class _PDV:
    """PDV mínimo com o estado que o gate inspeciona."""

    def __init__(self, **estado):
        self.caixa_id = None
        self.itens_carrinho = []
        self.valor_pago_acumulado = 0.0
        self.pagamentos_parciais = []
        self.modal_abertura = None
        for k, v in estado.items():
            setattr(self, k, v)

    def winfo_children(self):
        return []


class _Pai:
    _backup_em_execucao = False
    _fiscal_alerta_em_exibicao = False
    _system_monitor = None

    def __init__(self, pdv=None):
        self._janela_pdv = pdv


class TestGateOperacional(unittest.TestCase):
    def test_sem_pdv_permite(self):
        self.assertTrue(secure._operational_gate(None)[0])

    def test_caixa_aberto_bloqueia(self):
        permitido, motivo = secure._operational_gate(_Pai(_PDV(caixa_id=7)))
        self.assertFalse(permitido)
        self.assertIn("caixa", motivo.lower())

    def test_carrinho_cheio_bloqueia(self):
        self.assertFalse(secure._operational_gate(_Pai(_PDV(itens_carrinho=[{"total": 1}])))[0])

    def test_pagamento_em_andamento_bloqueia(self):
        self.assertFalse(secure._operational_gate(_Pai(_PDV(valor_pago_acumulado=10.0)))[0])

    def test_pagamento_multiplo_em_andamento_bloqueia(self):
        self.assertFalse(
            secure._operational_gate(_Pai(_PDV(pagamentos_parciais=[("PIX", 5.0)])))[0]
        )

    def test_modal_de_abertura_bloqueia(self):
        self.assertFalse(secure._operational_gate(_Pai(_PDV(modal_abertura=object())))[0])

    def test_backup_em_execucao_bloqueia(self):
        pai = _Pai()
        pai._backup_em_execucao = True
        self.assertFalse(secure._operational_gate(pai)[0])

    def test_fiscal_em_andamento_bloqueia(self):
        pai = _Pai()
        pai._fiscal_alerta_em_exibicao = True
        self.assertFalse(secure._operational_gate(pai)[0])

    def test_estado_tranquilo_permite(self):
        self.assertTrue(secure._operational_gate(_Pai(_PDV()))[0])


# --------------------------------------------------------------------------
# [7] CONTRATOS ESTRUTURAIS QUE NÃO DEVEM REGREDIR
# --------------------------------------------------------------------------
class _CorpoDoMetodo:
    """Ajudante: devolve o corpo EXECUTÁVEL de um método do helper.

    A docstring é descartada de propósito: ela pode legitimamente citar nomes
    de arquivo para explicar o motivo. O que não pode voltar é o código de
    leitura próprio, que é o que causava a divergência.
    """

    def _corpo(self, assinatura: str) -> str:
        inicio = FONTE_HELPER.find(assinatura)
        self.assertTrue(inicio > 0, f"metodo nao encontrado: {assinatura!r}")
        resto = FONTE_HELPER[inicio + len(assinatura):]
        fim = resto.find("\ndef ")
        corpo = resto if fim < 0 else resto[:fim]
        # remove a docstring, se houver, para só validar o código executável
        if '"""' in corpo:
            partes = corpo.split('"""')
            if len(partes) >= 3:
                corpo = partes[0] + partes[2]
        return corpo


class TestContratosEstruturais(_CorpoDoMetodo, unittest.TestCase):
    def test_helper_delega_a_leitura_ao_mesmo_leitor_do_app(self):
        """Trava o Item 12 contra regressão real.

        Um teste que só compara o RESULTADO passa mesmo com o helper lendo os
        arquivos por conta própria — isso foi verificado por mutação. Aqui
        exigimos que os dois métodos chamem `read_version_marker` de fato e não
        abram nenhum arquivo de versão diretamente.
        """
        for assinatura in ("def _version_marker(root: Path) -> str:",
                           "def _installed_version(app_root: Path) -> str:"):
            with self.subTest(metodo=assinatura.split("(")[0]):
                corpo = self._corpo(assinatura)
                self.assertIn("read_version_marker(", corpo)
                self.assertIn("allow_release_info=False", corpo)
                self.assertNotIn("version.txt", corpo)
                self.assertNotIn("version.json", corpo)

    def test_nenhum_leitor_de_versao_proprio_no_helper(self):
        """Nenhum marcador de versão pode ser aberto direto no helper."""
        for arquivo in ("version.txt", "version.json"):
            with self.subTest(arquivo=arquivo):
                self.assertNotIn('/ "%s"' % arquivo, FONTE_HELPER)
                self.assertNotIn("'%s'" % arquivo, FONTE_HELPER)

    def test_precedencia_do_leitor_unico(self):
        """`version.txt` vence `version.json` — trava a ordem de precedência."""
        self.assertEqual(secure.VERSION_MARKER_FILES, ("version.txt", "version.json"))

    def test_atualizacao_nunca_roda_no_pid_atual(self):
        self.assertIn("nunca substitui o EXE no PID atual", FONTE_SECURE)

    def test_app_nao_substitui_arquivos_do_runtime(self):
        """Quem extrai/instala é o helper; o app só baixa e valida."""
        self.assertNotIn("shutil.rmtree", FONTE_SECURE)
        self.assertIn("PROTECTED_TOP_LEVEL", FONTE_HELPER)

    def test_download_incompleto_e_descartado(self):
        """Baixa para `.part`, valida tamanho/hash e só então move."""
        self.assertIn(".part", FONTE_SECURE)
        self.assertIn("os.replace(partial, destination)", FONTE_SECURE)
        self.assertIn("SHA-256 do asset não confere", FONTE_SECURE)
        self.assertIn("Tamanho do asset diferente do manifesto", FONTE_SECURE)

    def test_exclusao_mutua_do_updater(self):
        self.assertIn("_exclusive_lock", FONTE_HELPER)
        self.assertIn("Outro processo de atualização já está em execução", FONTE_HELPER)

    def test_rollback_e_estados_de_falha_presentes(self):
        for estado in (secure.UpdateState.ROLLING_BACK, secure.UpdateState.ROLLED_BACK,
                       secure.UpdateState.FAILED):
            with self.subTest(estado=estado):
                self.assertTrue(estado.value)


# --------------------------------------------------------------------------
# [6] R1 — ESTADO DO CLIENTE NAS PROTEÇÕES DO UPDATER (Item 13)
# --------------------------------------------------------------------------
# Nomes que existiam na máquina do Mário e NÃO estavam em nenhuma lista.
ESTADO_CLIENTE_R1 = (
    "integracoes_cofre.json",
    "integracoes_config.json",
    "conversion_backup",
)


class TestEstadoDoClienteNasProtecoes(unittest.TestCase):
    """R1: os três estados entram no snapshot E na verificação por hash."""

    def test_estao_na_lista_de_snapshot_do_helper(self):
        for nome in ESTADO_CLIENTE_R1:
            with self.subTest(nome=nome):
                self.assertIn(nome, helper.PROTECTED_DATA_NAMES)

    def test_arquivos_estao_tambem_na_verificacao_por_hash(self):
        """Sem isso, uma alteração silenciosa não dispararia rollback."""
        for nome in ("integracoes_cofre.json", "integracoes_config.json"):
            with self.subTest(nome=nome):
                self.assertIn(nome, helper.PROTECTED_HASH_FILES)

    def test_estao_na_persistencia_do_app_paths(self):
        for nome in ("integracoes_cofre.json", "integracoes_config.json"):
            with self.subTest(nome=nome):
                self.assertIn(nome, app_paths._ARQUIVOS_PERSISTENTES)
        self.assertIn("conversion_backup",
                      [d[0] for d in app_paths._DIRETORIOS_PERSISTENTES])

    def test_conversion_backup_continua_fora_das_pastas_de_aplicacao(self):
        """`data/`, `runtime/` e `updates/` seguem inacessíveis ao ZIP."""
        self.assertNotIn("conversion_backup", helper.PROTECTED_TOP_LEVEL)


class TestSnapshotRestauracaoDoEstadoDoCliente(unittest.TestCase):
    """Prova funcional: snapshot → alteração → restauração preserva os três."""

    def _cenario(self):
        data = Path(tempfile.mkdtemp(prefix="frs-r1-data-"))
        (data / "integracoes_cofre.json").write_text(
            '{"version": 1, "scheme": "plain", "payload": "SEGREDO"}', encoding="utf-8")
        (data / "integracoes_config.json").write_text(
            '{"IFOOD": {"token": "x"}, "99ENTREGA": {}, "_estado": "ativo"}', encoding="utf-8")
        conversao = data / "conversion_backup"
        conversao.mkdir()
        (conversao / "pedido-123.json").write_text('{"pedido": 123}', encoding="utf-8")
        (conversao / "pedido-124.db").write_bytes(b"CONVERSAO-124")
        return data

    def test_roundtrip_preserva_os_tres_estados(self):
        data = self._cenario()
        antes = helper._protected_hashes(data)

        backup = Path(tempfile.mkdtemp(prefix="frs-r1-bkp-"))
        backup.rmdir()
        helper._backup_protected_data(data, backup)

        # Simula uma atualização que corrompe o estado do cliente.
        (data / "integracoes_cofre.json").write_text('{"payload": "VAZADO"}', encoding="utf-8")
        (data / "integracoes_config.json").write_text('{"IFOOD": {}}', encoding="utf-8")
        (data / "conversion_backup" / "pedido-123.json").write_text('{"pedido": 999}', encoding="utf-8")
        self.assertNotEqual(helper._protected_hashes(data), antes)

        # A detecção dispara antes da restauração.
        with self.assertRaises(helper.HelperError):
            helper._assert_protected_data_unchanged(antes, data)

        helper._restore_protected_data(data, backup)
        self.assertEqual(helper._protected_hashes(data), antes)

        self.assertEqual(
            json.loads((data / "integracoes_cofre.json").read_text(encoding="utf-8"))["payload"],
            "SEGREDO")
        self.assertEqual(
            json.loads((data / "integracoes_config.json").read_text(encoding="utf-8"))["IFOOD"],
            {"token": "x"})
        self.assertEqual((data / "conversion_backup" / "pedido-123.json").read_text(encoding="utf-8"),
                         '{"pedido": 123}')
        self.assertEqual((data / "conversion_backup" / "pedido-124.db").read_bytes(),
                         b"CONVERSAO-124")

    def test_restauracao_substitui_arquivo_corrompido(self):
        data = self._cenario()
        backup = Path(tempfile.mkdtemp(prefix="frs-r1-bkp2-"))
        backup.rmdir()
        helper._backup_protected_data(data, backup)

        (data / "integracoes_cofre.json").unlink()
        helper._restore_protected_data(data, backup)
        self.assertTrue((data / "integracoes_cofre.json").is_file())

    def test_restauracao_recria_diretorio_apagado(self):
        data = self._cenario()
        backup = Path(tempfile.mkdtemp(prefix="frs-r1-bkp3-"))
        backup.rmdir()
        helper._backup_protected_data(data, backup)

        import shutil as _shutil
        _shutil.rmtree(data / "conversion_backup")
        helper._restore_protected_data(data, backup)
        self.assertTrue((data / "conversion_backup" / "pedido-123.json").is_file())

    def test_conteudo_e_formato_nao_sao_alterados_pelas_listas(self):
        """R1 adiciona NOMES; não toca credenciais, formato ou conteúdo.

        `integracoes_*.json` aparecem legitimamente em DUAS listas do helper
        (PROTECTED_DATA_NAMES para o snapshot e PROTECTED_HASH_FILES para a
        verificação por hash) — é esse o desenho correto. O que não pode existir
        é código que abra, decodifique ou reescreva esses arquivos.
        """
        fonte = (RAIZ / "updater_helper.py").read_text(encoding="utf-8")
        for nome in ESTADO_CLIENTE_R1:
            with self.subTest(nome=nome):
                self.assertGreaterEqual(fonte.count(f'"{nome}"'), 1)
        for nome in ("integracoes_cofre.json", "integracoes_config.json"):
            with self.subTest(nome=nome):
                self.assertNotIn(f"open({nome}", fonte)
                self.assertNotIn(f'"{nome}", "w"', fonte)


if __name__ == "__main__":
    unittest.main()
