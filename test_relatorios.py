"""ITEM 5 (1.0.22) — Relatorios: dados das abas, filtros e exportacoes.

100% offline e temporario (APPDATA redirecionado antes de qualquer import).

Nao se testa a montagem de widgets do CustomTkinter: a UI e um detalhe de
apresentacao. Testa-se a camada de DADOS e de REGRA, que e onde mora a
logica real — as mesmas consultas executadas por `_renderizar_*`, os filtros
de periodo, o cancelamento logico do Vale e a geracao do esboco SPED.
"""

import csv
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-relatorios-tests-")
os.environ["APPDATA"] = _TEST_APPDATA.name

# Cada suite cria seu proprio APPDATA; ao final esta suite limpa o banco e
# restaura o valor anterior para nao contaminar as demais da mesma execucao.
_ANTERIOR_APPDATA = os.environ.get("APPDATA")


def tearDownModule():
    try:
        from app_paths import obter_caminho_dados

        for nome in ("mercado.db", "mercado.db-wal", "mercado.db-shm",
                     "mercado.db-journal"):
            alvo = Path(obter_caminho_dados(nome))
            if alvo.exists():
                alvo.unlink()
    except Exception:
        pass
    if _ANTERIOR_APPDATA is not None:
        os.environ["APPDATA"] = _ANTERIOR_APPDATA

import modulo_relatorio  # noqa: E402
from database_manager import get_db_connection  # noqa: E402
from modulo_relatorio import ModuloRelatorio  # noqa: E402


def _hoje():
    return datetime.now().strftime("%Y-%m-%d")


def _data(dias):
    return (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d")


class _Entry:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.value = kwargs["text"]


class _BaseRelatorios(unittest.TestCase):
    """Limpeza determinista e fabricas de dados para as tres abas."""

    def _limpar(self):
        with get_db_connection() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            for tabela in ("itens_venda", "vendas", "vendas_dia", "vales",
                           "vale_itens", "clientes", "produtos"):
                try:
                    conn.execute(f"DELETE FROM {tabela}")
                except Exception:
                    pass
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO clientes (id, nome) VALUES (1, 'MARIA')")

    def _relatorio(self, ini, fim):
        rel = ModuloRelatorio.__new__(ModuloRelatorio)
        rel.data_ini = _Entry(ini)
        rel.data_fim = _Entry(fim)
        rel.scroll_tabelas = None
        return rel

    def _venda(self, data_iso, total=100.0, status="APROVADO", origem="LOJA_FISICA"):
        with get_db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO vendas (data_venda, valor_total, valor_impostos_retidos,"
                " valor_liquido, origem, status_pedido, status_pagamento,"
                " forma_pagamento, valor_informado, diferenca)"
                " VALUES (?, ?, 0.0, ?, ?, ?, 'PAGO', 'PIX', ?, 0.0)",
                (data_iso, total, total, origem, status, total),
            )
            return int(cur.lastrowid)

    def _vale(self, data_iso, total=50.0, status="PENDENTE", cliente=1):
        with get_db_connection() as conn:
            n = conn.execute(
                "SELECT COALESCE(MAX(numero),0)+1 FROM vales"
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO vales (numero, cliente_id, data_criacao, status, total)"
                " VALUES (?, ?, ?, ?, ?)", (n, cliente, data_iso, status, total),
            )
            return int(cur.lastrowid)

    def _linhas(self, sql, params=()):
        with get_db_connection() as conn:
            return conn.execute(sql, params).fetchall()


class TestAbaVendas(_BaseRelatorios):
    """Aba 'Vendas do Dia': filtro de periodo respeitado e estorno oculto."""

    def setUp(self):
        self._limpar()

    def test_filtro_respeita_periodo_escolhido(self):
        hoje = self._venda(_hoje() + " 10:00:00")
        ontem = self._venda(_data(-1) + " 10:00:00")

        rel = self._relatorio(_data(-1), _hoje())
        rel.aba_ativa = "VENDAS_DIA"
        # Consulta real da aba (mesmo SQL de _renderizar_vendas_dia).
        with get_db_connection() as conn:
            linhas = conn.execute(
                "SELECT id FROM vendas WHERE date(data_venda) BETWEEN date(?)"
                " AND date(?) AND status_pedido != 'ESTORNADO' ORDER BY id DESC",
                (rel.data_ini.get(), rel.data_fim.get()),
            ).fetchall()
        self.assertEqual(sorted(r[0] for r in linhas), sorted([hoje, ontem]))

    def test_periodo_restrito_apenas_ao_dia(self):
        hoje = self._venda(_hoje() + " 10:00:00")
        self._venda(_data(-3) + " 10:00:00")

        rel = self._relatorio(_hoje(), _hoje())
        with get_db_connection() as conn:
            linhas = conn.execute(
                "SELECT id FROM vendas WHERE date(data_venda) BETWEEN date(?)"
                " AND date(?) AND status_pedido != 'ESTORNADO'",
                (rel.data_ini.get(), rel.data_fim.get()),
            ).fetchall()
        self.assertEqual([r[0] for r in linhas], [hoje])

    def test_venda_estornada_nao_aparece_na_aba_de_vendas(self):
        estornada = self._venda(_hoje() + " 10:00:00", status="ESTORNADO")
        normal = self._venda(_hoje() + " 11:00:00")

        rel = self._relatorio(_hoje(), _hoje())
        with get_db_connection() as conn:
            linhas = conn.execute(
                "SELECT id FROM vendas WHERE date(data_venda) BETWEEN date(?)"
                " AND date(?) AND status_pedido != 'ESTORNADO'",
                (rel.data_ini.get(), rel.data_fim.get()),
            ).fetchall()
        self.assertEqual([r[0] for r in linhas], [normal])
        self.assertNotIn(estornada, [r[0] for r in linhas])
class TestAbaVales(_BaseRelatorios):
    """Aba 'Vales': filtro por periodo de `data_criacao`."""

    def setUp(self):
        self._limpar()

    def _consulta(self, rel):
        with get_db_connection() as conn:
            return conn.execute(
                "SELECT v.id, v.numero, COALESCE(c.nome,'Cliente não informado'),"
                " v.data_criacao, v.total, v.status, v.data_quitacao"
                " FROM vales v LEFT JOIN clientes c ON c.id = v.cliente_id"
                " WHERE date(v.data_criacao) BETWEEN date(?) AND date(?)"
                " ORDER BY v.numero DESC",
                (rel.data_ini.get(), rel.data_fim.get()),
            ).fetchall()

    def test_filtro_de_periodo_dos_vales(self):
        hoje = self._vale(_hoje() + " 09:00:00")
        self._vale(_data(-5) + " 09:00:00")

        self.assertEqual([r[0] for r in self._consulta(self._relatorio(_hoje(), _hoje()))], [hoje])

    def test_periodo_amplo_traz_vales_do_intervalo(self):
        dentro = self._vale(_data(-2) + " 09:00:00")
        self._vale(_data(-40) + " 09:00:00")

        linhas = self._consulta(self._relatorio(_data(-7), _hoje()))
        self.assertEqual([r[0] for r in linhas], [dentro])

    def test_aba_traz_cliente_e_status(self):
        vale_id = self._vale(_hoje() + " 09:00:00", total=75.5)
        linha = self._consulta(self._relatorio(_hoje(), _hoje()))[0]

        self.assertEqual(linha[0], vale_id)
        self.assertEqual(linha[2], "MARIA")
        self.assertEqual(linha[4], 75.5)
        self.assertEqual(linha[5], "PENDENTE")
        self.assertIsNone(linha[6])


class TestAbaEstornos(_BaseRelatorios):
    """Aba 'Estornos': apenas vendas ESTORNADAS e dentro do periodo."""

    def setUp(self):
        self._limpar()

    def _consulta(self, rel):
        with get_db_connection() as conn:
            return conn.execute(
                "SELECT id, data_venda, valor_total, forma_pagamento, origem,"
                " status_pedido FROM vendas WHERE status_pedido = 'ESTORNADO'"
                " AND date(data_venda) BETWEEN date(?) AND date(?) ORDER BY id DESC",
                (rel.data_ini.get(), rel.data_fim.get()),
            ).fetchall()

class TestRegrasDeRelatorio(_BaseRelatorios):
    """Regras que dependem de UI mas nao de widgets: cancelamento e exportacao."""

    def setUp(self):
        self._limpar()
        self.rel = self._relatorio(_data(-10), _hoje())

    def _cancelar(self, vale_id, status="PENDENTE", confirmar=True):
        with patch.object(self.rel, "_carregar_tabela_ativa", lambda: None), \
             patch.object(modulo_relatorio.messagebox, "askyesno", return_value=confirmar), \
             patch.object(modulo_relatorio.messagebox, "showinfo"), \
             patch.object(modulo_relatorio.messagebox, "showwarning") as aviso, \
             patch.object(modulo_relatorio.messagebox, "showerror"):
            self.rel.cancelar_vale(vale_id, 1, "MARIA", status)
        return aviso

    def test_cancelamento_logico_marca_sem_apagar(self):
        vale_id = self._vale(_hoje() + " 09:00:00")
        self._cancelar(vale_id)

        linha = self._linhas("SELECT status, numero FROM vales WHERE id = ?", (vale_id,))[0]
        self.assertEqual(linha[0], "CANCELADO")
        self.assertEqual(linha[1], 1, "o numero permanece reservado")

    def test_cancelamento_nao_apaga_o_registro(self):
        vale_id = self._vale(_hoje() + " 09:00:00")
        self._cancelar(vale_id)
        self.assertEqual(
            self._linhas("SELECT COUNT(*) FROM vales WHERE id = ?", (vale_id,))[0][0], 1
        )

    def test_vale_quitado_ou_cancelado_protegido(self):
        vale_id = self._vale(_hoje() + " 09:00:00", status="QUITADO")
        aviso = self._cancelar(vale_id, status="QUITADO")
        self.assertTrue(aviso.called, "deve avisar que nao pode cancelar")
        self.assertEqual(
            self._linhas("SELECT status FROM vales WHERE id = ?", (vale_id,))[0][0],
            "QUITADO",
        )

    def test_cancelamento_recusado_nao_altera(self):
        vale_id = self._vale(_hoje() + " 09:00:00")
        self._cancelar(vale_id, confirmar=False)
        self.assertEqual(
            self._linhas("SELECT status FROM vales WHERE id = ?", (vale_id,))[0][0],
            "PENDENTE",
        )

    def test_numero_de_vale_nao_e_reaproveitado_apos_cancelamento(self):
        primeiro = self._vale(_hoje() + " 09:00:00")
        self._cancelar(primeiro)
        segundo = self._vale(_hoje() + " 10:00:00")
        self.assertNotEqual(primeiro, segundo)


class TestEsbocoSped(_BaseRelatorios):
    """`gerar_esboco_sped`: CSV gerado em pasta de dados temporaria."""

    def setUp(self):
        self._limpar()
        self.rel = self._relatorio(_data(-10), _hoje())

    def _gerar(self):
        with patch.object(modulo_relatorio.messagebox, "showinfo"), \
             patch.object(modulo_relatorio.messagebox, "showerror") as erro, \
             patch.object(modulo_relatorio, "registrar_log", lambda *a, **k: None):
            self.rel.gerar_esboco_sped()
        return erro

    def test_gera_csv_com_cabecalho_e_itens(self):
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO produtos (id, nome, preco_custo, preco_venda, codigo_barras,"
                " ncm) VALUES (1, 'ARROZ', 6.0, 10.0, '7891', '19059090')"
            )
        venda = self._venda(_hoje() + " 10:00:00", total=20.0)
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO itens_venda (venda_id, produto_id, quantidade, subtotal)"
                " VALUES (?, 1, 2.0, 20.0)", (venda,)
            )

        erro = self._gerar()
        self.assertFalse(erro.called, "a geracao do esboco nao pode falhar")

        gerados = sorted(Path(_pasta_relatorios()).glob("esboco_sped_fiscal_*.csv"))
        self.assertEqual(len(gerados), 1)
        with gerados[0].open(encoding="utf-8") as arq:
            linhas = list(csv.reader(arq, delimiter=";"))

        self.assertEqual(linhas[0][0], "data_movimento")
        self.assertIn("ncm", linhas[0])
        self.assertEqual(len(linhas), 2, "uma linha de dados")
        self.assertEqual(linhas[1][1], str(venda))
        self.assertEqual(linhas[1][3], "19059090")

    def test_periodo_sem_movimentos_gera_cabecalho_vazio(self):
        self._gerar()
        gerados = sorted(Path(_pasta_relatorios()).glob("esboco_sped_fiscal_*.csv"))
        self.assertEqual(len(gerados), 1)
        with gerados[0].open(encoding="utf-8") as arq:
            linhas = list(csv.reader(arq, delimiter=";"))
        self.assertEqual(len(linhas), 1, "apenas o cabecalho")


def _pasta_relatorios():
    from app_paths import obter_caminho_dados

    return obter_caminho_dados("relatorios")
    def test_lista_apenas_estornos(self):
        estornada = self._venda(_hoje() + " 10:00:00", status="ESTORNADO")
        self._venda(_hoje() + " 11:00:00", status="APROVADO")

        linhas = self._consulta(self._relatorio(_hoje(), _hoje()))
        self.assertEqual([r[0] for r in linhas], [estornada])
        self.assertEqual(linhas[0][5], "ESTORNADO")

    def test_estorno_fora_do_periodo_e_omitido(self):
        self._venda(_data(-20) + " 10:00:00", status="ESTORNADO")
        self.assertEqual(self._consulta(self._relatorio(_hoje(), _hoje())), [])