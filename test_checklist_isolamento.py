"""B1 — o checklist operacional NUNCA pode apagar o banco do cliente (Item 13).

Este arquivo existe porque o defeito era SILENCIOSO: o roteiro rodava sem
confirmação e apagava `%APPDATA%\FRS_Mercado\data\mercado.db`. Um teste comum
("o script roda e passa") não prova nada aqui, porque o dano acontece FORA do
banco de teste.

Por isso os testes aqui atacam a TRAVA, não o roteiro:

* forçam `get_db_path()` a devolver o caminho do banco real;
* exigem que a rotina de limpeza ABORTE antes de qualquer `unlink`;
* verificam que o arquivo do "cliente" continua existindo e com o mesmo
  conteúdo depois da tentativa.

Nada toca o banco real: o "mercado.db" do cliente usado aqui é um arquivo
falso, criado em diretório temporário, só para provar a recusa.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("APPDATA", tempfile.mkdtemp(prefix="frs-b1-cliente-"))

import checklist_operacional as checklist  # noqa: E402

RAIZ = Path(__file__).resolve().parent
FONTE = (RAIZ / "checklist_operacional.py").read_text(encoding="utf-8")


class _ClienteFalso:
    """Um 'mercado.db do cliente' falso, só para provar que não é apagado."""

    def __init__(self):
            self.dir = Path(tempfile.mkdtemp(prefix="frs-b1-falso-"))
            self.db = self.dir / "mercado.db"
            self.db.write_bytes(b"CONTEUDO-DO-BANCO-DO-CLIENTE")

    def como_str(self) -> str:
            return str(self.db)


class TestChecklistNaoApagaOBancoDoCliente(unittest.TestCase):
    """Força o caminho do banco do CLIENTE e exige recusa, sem dano."""

    def setUp(self):
        self.cliente = _ClienteFalso()
        self._original_get = checklist.get_db_path

    def tearDown(self):
        # Só restaura o dublê. A prova de que o arquivo sobreviveu fica em cada
        # teste (via _afirmar_intacto), e não aqui: um `self.fail` no tearDown
        # mascara o motivo real da falha.
        checklist.get_db_path = self._original_get

    def _afirmar_intacto(self):
        self.assertTrue(self.cliente.db.exists(), "banco do cliente foi removido")
        self.assertEqual(self.cliente.db.read_bytes(), b"CONTEUDO-DO-BANCO-DO-CLIENTE")

    def test_reset_db_recusa_banco_fora_da_raiz_de_teste(self):
        checklist.get_db_path = lambda: self.cliente.como_str()
        with self.assertRaises(checklist._AmbienteDoClienteError):
            checklist._reset_db()
        self._afirmar_intacto()

    def test_garantia_recusa_banco_do_cliente(self):
        checklist.get_db_path = lambda: self.cliente.como_str()
        with self.assertRaises(checklist._AmbienteDoClienteError):
            checklist._garantir_banco_isolado()
        self._afirmar_intacto()

    def test_mensagem_de_recusa_explica_o_motivo(self):
        checklist.get_db_path = lambda: self.cliente.como_str()
        with self.assertRaises(checklist._AmbienteDoClienteError) as ctx:
            checklist._garantir_banco_isolado()
        texto = str(ctx.exception).lower()
        self.assertIn("tempor", texto)
        self.assertIn("cliente", texto)
        self._afirmar_intacto()

    def test_main_encerra_antes_de_apagar_banco_do_cliente(self):
        checklist.get_db_path = lambda: self.cliente.como_str()
        with self.assertRaises(checklist._AmbienteDoClienteError):
            checklist.main()
        self._afirmar_intacto()

    def test_banco_de_teste_e_sempre_temporario(self):
        raiz = Path(checklist._RAIZ_TESTE)
        self.assertTrue(raiz.exists())
        self.assertTrue(checklist._raiz_temporaria(raiz / "mercado.db"))
        self.assertFalse(checklist._raiz_temporaria(Path("C:/qualquer/coisa/mercado.db")))

class TestProtecaoEstruturalDoChecklist(unittest.TestCase):
    """A proteção não pode depender de confirmação humana nem de flag."""

    def test_isolamento_antes_dos_imports_que_resolvem_caminhos(self):
        fonte = FONTE.splitlines()
        idx_appdata = next(i for i, l in enumerate(fonte) if 'os.environ["APPDATA"]' in l)
        idx_db = next(i for i, l in enumerate(fonte) if "from database_manager import" in l)
        self.assertLess(idx_appdata, idx_db, "APPDATA precisa ser isolado antes do import")

    def test_sem_confirmacao_humana_como_protecao(self):
        """Analisamos o código EXECUTÁVEL via AST, não o texto bruto."""
        import ast as _ast

        chamadas = set()
        for no in _ast.walk(_ast.parse(FONTE)):
            if isinstance(no, _ast.Call):
                func = no.func
                nome = getattr(func, "attr", None) or getattr(func, "id", None)
                if nome:
                    chamadas.add(nome)
        for proibido in ("input", "askyesno", "askokcancel", "confirm"):
            with self.subTest(proibido=proibido):
                self.assertNotIn(proibido, chamadas)

        for flag in ('"--force"', '"--yes"', '"--skip-check"'):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, FONTE)

    def test_reset_db_passa_pela_trava_antes_do_unlink(self):
        corpo = FONTE[FONTE.index("def _reset_db():"):]
        self.assertLess(corpo.index("_garantir_banco_isolado()"), corpo.index("unlink("))

    def test_main_chama_a_trava_antes_de_reset_db(self):
        corpo = FONTE[FONTE.index("def main():"):]
        self.assertLess(corpo.index("_garantir_banco_isolado()"), corpo.index("_reset_db()"))


if __name__ == "__main__":
    unittest.main()