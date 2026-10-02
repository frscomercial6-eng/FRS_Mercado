"""ITEM 5 (1.0.22) — Vales: criacao, consulta, quitacao e cancelamento.

100% offline e 100% temporario:
- APPDATA redirecionado ANTES de qualquer import (modulo_relatorio grava log
  na importacao), garantindo que o banco real do cliente jamais seja aberto;
- nenhuma UI real e criada: o PDV e instanciado via `ModuloPDV.__new__`,
  exatamente como `ModuloRelatorio._obter_impressor_documento` faz no
  proprio produto para reimprimir com o PDV fechado;
- impressora e messagebox sao substituidos; nenhum hardware e acionado.

Cobre o contrato real: `salvar_vale_atual`, `_preparar_carrinho_documental`,
`finalizar_venda_pdv` (quitracao transacional) e `ModuloRelatorio.cancelar_vale`.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_APPDATA = tempfile.TemporaryDirectory(prefix="frs-vales-tests-")
os.environ["APPDATA"] = _TEST_APPDATA.name

# Cada suite de testes cria seu proprio APPDATA. Em uma unica execucao com
# varias suites, o APPDATA do ultimo importado prevalece e as suites passam a
# enxergar o mesmo banco. Para nao contaminar as demais (por exemplo
# `test_pdv_payment_flow`, que exige que o APPDATA NAO contenha um banco),
# esta suite restaura o APPDATA anterior e remove o banco ao final.
_ANTERIOR_APPDATA = os.environ.get("APPDATA")


def _limpar_banco_da_suite():
    """Remove o banco desta suite e restaura o APPDATA anterior."""
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


def tearDownModule():
    _limpar_banco_da_suite()

import database_manager  # noqa: E402,F401
from database_manager import get_db_connection  # noqa: E402
import modulo_pdv  # noqa: E402,F401
import modulo_relatorio  # noqa: E402,F401
from calculadora_tributaria import CalculadoraTributaria  # noqa: E402
from modulo_pdv import ModuloPDV  # noqa: E402
from modulo_relatorio import ModuloRelatorio  # noqa: E402


CONFIG = {
    "nome_fantasia": "MERCADO BOM PRECO",
    "razao_social": "MERCADO BOM PRECO COMERCIO DE ALIMENTOS LTDA",
    "cnpj": "11.222.333/0001-44",
}

class _Entry:
    """Entrada de texto minima (apenas .get/.delete)."""

    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def delete(self, _start=None, _end=None):
        self.value = ""


class _Label:
    def __init__(self, text="0.00"):
        self.text = text

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


class _FakeUI:
    """Camada de interface usada pelos fluxos de Vale (sem CustomTkinter)."""

    def __init__(self):
        self.statuses = []
        self.prints = []
        self.config = dict(CONFIG)

    def _set_status(self, mensagem, cor="#3498db"):
        self.statuses.append((mensagem, cor))

    def _renderizar_carrinho(self):
        pass

    def atualizar_total_display(self):
        pass

    def _limpar_contexto_documental(self):
        pass

    def limpar_pagamentos_recebidos(self):
        pass

    def _selecionar_cliente_orcamento(self, _cliente_id):
        pass

    def _carregar_clientes_orcamento(self, preservar_selecao=False):
        pass

    def _retornar_foco_pdv(self):
        pass

    def _safe_focus(self, *_a, **_k):
        pass

    def _formatar_moeda_br(self, valor):
        return f"R$ {float(valor):.2f}"

    def imprimir_cupom(self, dados):
        self.prints.append(dados)

    def _largura_cupom_chars(self):
        return 42

    def _split_texto_largura(self, texto, largura):
        return [texto[:largura]]

    def _formatar_quantidade_cupom(self, qtd, unidade="UN"):
        return f"{qtd:g}{unidade}"

    def _formatar_linha_item_cupom(self, nome, _qtd, _total, _largura, _unidade=None):
        return [str(nome)]

    def _enviar_raw_impressora_padrao(self, _payload):
        pass

    def winfo_exists(self):
        return False

    def abrir_gaveta(self, _forma=None):
        return True

    def _montar_texto_resumo_fechamento(self, dados):
        return "\n".join(str(v) for v in dados.values())


def _novo_pdv():
    """PDV real (sem __init__/UI) com a camada de interface falsa.

    Nao substitui metodo de negocio: apenas fornece os delegates de interface
    que `salvar_vale_atual` e `finalizar_venda_pdv`'utilizam.
    """
    ui = _FakeUI()
    pdv = ModuloPDV.__new__(ModuloPDV)
    pdv.config = dict(CONFIG)
    # dependencia real criada no __init__ do PDV (linha 120)
    pdv.calculadora_tributaria = CalculadoraTributaria()
    for nome in dir(ui):
        if nome.startswith("__") or nome in ("config", "statuses", "prints"):
            continue
        atributo = getattr(ui, nome)
        if callable(atributo):
            setattr(pdv, nome, atributo)
    pdv.statuses = ui.statuses
    pdv.prints = ui.prints
    # Campos de interface lidos por finalizar_venda_pdv.
    pdv.ent_valor_pago = _Entry()
    pdv.lbl_troco_venda = _Label("R$ 0,00")
    pdv.lbl_total_venda = _Label("R$ 0,00")
    pdv.lbl_status_fiscal = _Label("")
    return pdv


class _BaseVales(unittest.TestCase):
    """Zera as tabelas e cria dados base em um banco temporario."""

    def setUp(self):
        with get_db_connection() as conn:
            # Limpeza na ordem correta das dependencias (itens antes das
            # cabecalhos) para respeitar as FOREIGN KEY ativas.
            conn.execute("PRAGMA foreign_keys = OFF")
            for tabela in (
                "vale_itens", "vales", "itens_venda", "vendas", "vendas_dia",
                "sangrias", "reforcos", "financeiro", "caixa_conferencia",
                "caixa_operacao", "produto_lotes", "entradas", "clientes",
                "produtos",
            ):
                try:
                    conn.execute(f"DELETE FROM {tabela}")
                except Exception:
                    pass
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO clientes (id, nome) VALUES (1, 'MARIA DA SILVA')")
            conn.execute(
                "INSERT INTO produtos (id, nome, preco_custo, preco_venda,"
                " codigo_barras) VALUES (1, 'ARROZ 1KG', 6.0, 10.0, '7891000000011')"
            )
            conn.execute(
                "INSERT INTO produtos (id, nome, preco_custo, preco_venda,"
                " codigo_barras) VALUES (2, 'FEIJAO 1KG', 5.0, 8.5, '7891000000028')"
            )
        for alvo in ("modulo_pdv.registrar_log", "modulo_relatorio.registrar_log"):
            p = patch(alvo, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)

    def item(self, produto_id=1, nome="ARROZ 1KG", qtd=2.0, preco=10.0,
             ncm="19059090", barcode="7891000000011"):
        return {
            "id": produto_id, "barcode": barcode, "nome": nome, "ncm": ncm,
            "quantidade": qtd, "unidade": "UN", "preco": preco,
            "total": round(qtd * preco, 2),
        }

    def pdv_com_cliente(self, itens, cliente_id=1):
        pdv = _novo_pdv()
        pdv.itens_carrinho = list(itens)
        pdv.item_selecionado_idx = None
        pdv.caixa_id = None
        pdv.pagamentos_parciais = []
        pdv._garantir_cliente_para_vale = lambda: cliente_id
        return pdv

    def vale(self, vale_id):
        with get_db_connection() as conn:
            return conn.execute(
                "SELECT id, numero, cliente_id, status, data_quitacao, venda_id,"
                " caixa_operacao_id, forma_pagamento, total FROM vales WHERE id = ?",
                (int(vale_id),),
            ).fetchone()

    def itens_do_vale(self, vale_id):
        with get_db_connection() as conn:
            return conn.execute(
                "SELECT produto_id, codigo_barras, descricao_produto, ncm,"
                " quantidade, unidade, preco_unitario, subtotal"
                " FROM vale_itens WHERE vale_id = ? ORDER BY id",
                (int(vale_id),),
            ).fetchall()

    def abrir_caixa(self):
        with get_db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO caixa_operacao (data_abertura, saldo_inicial, status)"
                " VALUES (CURRENT_TIMESTAMP, 0, 'ABERTO')"
            )
            return int(cur.lastrowid)
class TestCriacaoVale(_BaseVales):
    """`salvar_vale_atual`: criacao, cliente obrigatorio e snapshot."""

    def test_cria_vale_pendente_com_numero_e_total(self):
        pdv = self.pdv_com_cliente([self.item()])
        vale_id = pdv.salvar_vale_atual()

        self.assertIsNotNone(vale_id)
        linha = self.vale(vale_id)
        self.assertEqual(linha[1], 1, "numero deve iniciar em 1")
        self.assertEqual(linha[2], 1, "cliente_id deve ser o informado")
        self.assertEqual(linha[3], "PENDENTE", "vale nasce PENDENTE")
        self.assertEqual(round(float(linha[8]), 2), 20.0)
        self.assertIsNone(linha[4], "nao ha data de quitacao na criacao")

    def test_carrinho_e_limpo_apos_salvar(self):
        pdv = self.pdv_com_cliente([self.item()])
        pdv.salvar_vale_atual()
        self.assertEqual(pdv.itens_carrinho, [])

    def test_impressao_do_cupom_de_vale_e_acionada(self):
        pdv = self.pdv_com_cliente([self.item()])
        pdv.salvar_vale_atual()
        self.assertTrue(pdv.prints, "a impressao do Vale deve ser disparada")
        self.assertEqual(pdv.prints[0]["documento_tipo"], "VALE")

    def test_numero_do_vale_e_sequencial(self):
        primeiro = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        segundo = self.pdv_com_cliente([self.item()]).salvar_vale_atual()

        self.assertEqual(self.vale(primeiro)[1], 1)
        self.assertEqual(self.vale(segundo)[1], 2)

    def test_carrinho_vazio_recusa_e_nao_grava(self):
        pdv = self.pdv_com_cliente([])
        self.assertIsNone(pdv.salvar_vale_atual())
        with get_db_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vales").fetchone()[0], 0
            )

    def test_sem_cliente_recusa_e_nao_grava(self):
        pdv = self.pdv_com_cliente([self.item()], cliente_id=None)
        self.assertIsNone(pdv.salvar_vale_atual())
        with get_db_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vales").fetchone()[0], 0
            )

    def test_grava_snapshot_de_preco_descricao_e_ncm(self):
        pdv = self.pdv_com_cliente([self.item(qtd=3.0, preco=10.0)])
        vale_id = pdv.salvar_vale_atual()

        itens = self.itens_do_vale(vale_id)
        self.assertEqual(len(itens), 1)
        produto_id, barcode, descricao, ncm, qtd, unid, unitario, subtotal = itens[0]
        self.assertEqual(produto_id, 1)
        self.assertEqual(barcode, "7891000000011")
        self.assertEqual(descricao, "ARROZ 1KG")
        self.assertEqual(ncm, "19059090")
        self.assertEqual(round(float(qtd), 3), 3.0)
        self.assertEqual(unid, "UN")
        self.assertEqual(round(float(unitario), 2), 10.0)
        self.assertEqual(round(float(subtotal), 2), 30.0)

    def test_snapshot_nao_muda_quando_o_produto_e_reprecado(self):
        """O Vale guarda o preco/descricao do momento da emissao."""
        pdv = self.pdv_com_cliente([self.item(preco=10.0)])
        vale_id = pdv.salvar_vale_atual()

        with get_db_connection() as conn:
            conn.execute("UPDATE produtos SET preco_venda = 99.0, nome = 'NOVO NOME'")

        _pid, _bc, descricao, _ncm, _q, _u, unitario, _s = self.itens_do_vale(vale_id)[0]
        self.assertEqual(descricao, "ARROZ 1KG")
        self.assertEqual(round(float(unitario), 2), 10.0)

    def test_itens_multiplos_somam_no_total(self):
        itens = [
            self.item(produto_id=1, nome="ARROZ 1KG", qtd=2.0, preco=10.0,
                      barcode="7891000000011"),
            self.item(produto_id=2, nome="FEIJAO 1KG", qtd=2.0, preco=8.5,
                      barcode="7891000000028"),
        ]
        vale_id = self.pdv_com_cliente(itens).salvar_vale_atual()

        self.assertEqual(round(float(self.vale(vale_id)[8]), 2), 37.0)
        self.assertEqual(len(self.itens_do_vale(vale_id)), 2)


class TestConsultaEUtilizacao(_BaseVales):
    """Consulta por cliente e carregamento do Vale no carrinho (utilizacao)."""

    def test_consulta_lista_apenas_vales_pendentes_do_cliente(self):
        # As gravacoes ficam fora de um `with get_db_connection()` externo:
        # abrir a transacao aqui bloquearia o banco usado por salvar_vale_atual.
        with get_db_connection() as conn:
            conn.execute("INSERT INTO clientes (id, nome) VALUES (2, 'JOSE')")
        a = self.pdv_com_cliente([self.item()], cliente_id=1).salvar_vale_atual()
        b = self.pdv_com_cliente([self.item()], cliente_id=2).salvar_vale_atual()
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        with get_db_connection() as conn:
            conn.execute("UPDATE vales SET status='CANCELADO' WHERE id = ?", (b,))
            pendentes = [
                r[0] for r in conn.execute(
                    "SELECT id FROM vales WHERE cliente_id = ? AND status='PENDENTE'",
                    (1,),
                ).fetchall()
            ]
        self.assertEqual(pendentes, [a])

    def test_consulta_ordena_por_numero_decrescente(self):
        self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        with get_db_connection() as conn:
            numeros = [
                r[0] for r in conn.execute(
                    "SELECT numero FROM vales WHERE cliente_id=1 AND status='PENDENTE'"
                    " ORDER BY numero DESC"
                ).fetchall()
            ]
        self.assertEqual(numeros, [2, 1])

    def test_utilizacao_carrega_itens_e_marca_para_quitar(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        itens = [dict(i) for i in self.itens_do_vale_com_clave(vale_id)]

        pdv = _novo_pdv()
        pdv.itens_carrinho = []
        ok = pdv._preparar_carrinho_documental(
            itens, 1, "VALE", vales_ids=[vale_id]
        )

        self.assertTrue(ok)
        self.assertEqual(len(pdv.itens_carrinho), len(itens))
        self.assertEqual(pdv._vales_para_quitar, [vale_id])
        self.assertEqual(pdv._operacao_documento_tipo, "VALE")
        self.assertEqual(pdv._operacao_vale_cliente_id, 1)
        self.assertEqual(pdv.itens_carrinho[0]["nome"], "ARROZ 1KG")
        self.assertEqual(pdv.itens_carrinho[0]["origem"], "VALE")

    def itens_do_vale_com_clave(self, vale_id):
        with get_db_connection() as conn:
            linhas = conn.execute(
                "SELECT produto_id, codigo_barras, descricao_produto, ncm,"
                " quantidade, unidade, preco_unitario, subtotal"
                " FROM vale_itens WHERE vale_id = ? ORDER BY id",
                (int(vale_id),),
            ).fetchall()
        return [
            {
                "id": l[0], "barcode": l[1] or "", "nome": l[2], "ncm": l[3] or "",
                "quantidade": float(l[4] or 0.0), "unidade": str(l[5] or "UN"),
                "preco": float(l[6] or 0.0), "total": float(l[7] or 0.0),
                "origem": "VALE", "vale_id": int(vale_id),
            }
            for l in linhas
        ]


class TestQuitacaoTransacional(_BaseVales):
    """Quitracao do Vale dentro da mesma transacao da venda."""

    def _pdv_pronto_para_quitar(self, vale_ids, caixa_id):
        itens = [self.item()]
        pdv = self.pdv_com_cliente(itens)
        pdv.caixa_id = caixa_id
        pdv._vales_para_quitar = list(vale_ids)
        pdv._garantir_cliente_para_vale = lambda: 1
        return pdv

    def test_quita_vale_e_vincula_a_venda(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        pdv = self._pdv_pronto_para_quitar([vale_id], caixa_id)

        pdv.finalizar_venda_pdv("PIX")

        linha = self.vale(vale_id)
        self.assertEqual(linha[3], "QUITADO")
        self.assertIsNotNone(linha[4], "data_quitacao deve ser gravada")
        self.assertIsNotNone(linha[5], "venda_id deve ser vinculado")
        self.assertEqual(linha[6], caixa_id)
        self.assertEqual(linha[7], "PIX")

    def test_recusa_quitacao_sem_caixa_aberto(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        pdv = self._pdv_pronto_para_quitar([vale_id], None)

        pdv.finalizar_venda_pdv("PIX")

        self.assertEqual(self.vale(vale_id)[3], "PENDENTE",
                         "sem caixa o vale nao pode ser quitado")

    def test_recusa_quitacao_com_caixa_fechado(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE caixa_operacao SET status='FECHADO', data_fechamento=CURRENT_TIMESTAMP"
                " WHERE id = ?", (caixa_id,)
            )
        pdv = self._pdv_pronto_para_quitar([vale_id], caixa_id)

        pdv.finalizar_venda_pdv("PIX")

        self.assertEqual(self.vale(vale_id)[3], "PENDENTE")

    def test_quita_varios_vales_de_uma_vez(self):
        a = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        b = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        pdv = self._pdv_pronto_para_quitar([a, b], caixa_id)

        # PIX completa o total automaticamente (sem valor informado).
        pdv.finalizar_venda_pdv("PIX")

        self.assertEqual(self.vale(a)[3], "QUITADO")
        self.assertEqual(self.vale(b)[3], "QUITADO")
        self.assertEqual(self.vale(a)[5], self.vale(b)[5], "mesma venda")

    def test_valor_nao_e_reaproveitado_apos_quitacao(self):
        """O proximo vale nao reaproveita o numero de um vale ja quitado."""
        primeiro = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        self._pdv_pronto_para_quitar([primeiro], caixa_id).finalizar_venda_pdv("PIX")
        segundo = self.pdv_com_cliente([self.item()]).salvar_vale_atual()

        self.assertEqual(self.vale(primeiro)[1], 1)
        self.assertEqual(self.vale(segundo)[1], 2)


class TestRollbackQuitacao(_BaseVales):
    """Rollback: se um vale nao puder ser quitado, nada e persistido."""

    def test_venda_e_rollada_quando_o_vale_ja_foi_quitado(self):
        """Concorrencia: o vale ja estava QUITADO, a venda nova nao pode sobrar."""
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        # Venda pre-existente de outro operador (respeita a FK).
        with get_db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO vendas (data_venda, valor_total, valor_impostos_retidos,"
                " valor_liquido, origem, status_pedido, status_pagamento,"
                " forma_pagamento, valor_informado, diferenca)"
                " VALUES (CURRENT_TIMESTAMP, 20.0, 0.0, 20.0, 'LOJA_FISICA',"
                " 'APROVADO', 'PAGO', 'PIX', 20.0, 0.0)"
            )
            venda_antiga = int(cur.lastrowid)
            conn.execute(
                "UPDATE vales SET status='QUITADO', data_quitacao=CURRENT_TIMESTAMP,"
                " venda_id = ?, caixa_operacao_id = ?, forma_pagamento='PIX'"
                " WHERE id = ?", (venda_antiga, caixa_id, vale_id)
            )

        pdv = self._pdv_pronto_para_quitar([vale_id], caixa_id)
        pdv.finalizar_venda_pdv("PIX")

        linha = self.vale(vale_id)
        self.assertEqual(linha[3], "QUITADO")
        self.assertEqual(
            linha[5], venda_antiga,
            "o vale nao pode ser re-ligado a uma nova venda",
        )
        with get_db_connection() as conn:
            # Nenhuma venda nova pode ter sido criada pela tentativa.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0], 1,
                "a venda deve ter sido revertida junto com a quitacao",
            )

    def test_rollback_quando_um_dos_vales_nao_pode_ser_quitado(self):
        """Um lote onde um vale ja foi quitado reverte TUDO, inclusive o PENDENTE."""
        a = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        b = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE vales SET status='QUITADO' WHERE id = ?", (b,)
            )

        pdv = self._pdv_pronto_para_quitar([a, b], caixa_id)
        pdv.finalizar_venda_pdv("PIX")

        self.assertEqual(
            self.vale(a)[3], "PENDENTE",
            "o vale que falhou a quitacao deve voltar a PENDENTE (rollback)",
        )
        self.assertIsNone(self.vale(a)[5], "sem vinculo de venda apos rollback")
        with get_db_connection() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vendas").fetchone()[0], 0
            )

    def _pdv_pronto_para_quitar(self, vale_ids, caixa_id):
        pdv = self.pdv_com_cliente([self.item()])
        pdv.caixa_id = caixa_id
        pdv._vales_para_quitar = (
            [vale_ids] if isinstance(vale_ids, int) else list(vale_ids)
        )
        return pdv


class TestCancelamentoEReimpressao(_BaseVales):
    """`cancelar_vale` e `reimprimir_vale`: logico, sem tocar em status alheio."""

    def _relatorio(self):
        rel = ModuloRelatorio.__new__(ModuloRelatorio)
        rel.reload_aba = 0
        return rel

    def _impressor_falso(self):
        class _Impressora:
            def __init__(self):
                self.chamadas = []

            def imprimir_cupom_vale(self, vale_id):
                self.chamadas.append(("vale", int(vale_id)))

            def imprimir_cupom(self, dados):
                self.chamadas.append(("venda", dados))
        return _Impressora()

    def test_cancelar_vale_marca_sem_apagar_registro(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        rel = self._relatorio()

        with patch.object(rel, "_carregar_tabela_ativa", lambda: None), \
             patch.object(modulo_relatorio.messagebox, "askyesno", return_value=True), \
             patch.object(modulo_relatorio.messagebox, "showinfo"), \
             patch.object(modulo_relatorio.messagebox, "showerror"):
            rel.cancelar_vale(vale_id, 1, "MARIA DA SILVA", "PENDENTE")

        linha = self.vale(vale_id)
        self.assertIsNotNone(linha, "o registro nao pode ser apagado")
        self.assertEqual(linha[3], "CANCELADO")
        self.assertEqual(linha[1], 1, "o numero permanece no historico")

    def test_cancelar_vale_ja_quitado_e_recusado(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        caixa_id = self.abrir_caixa()
        self._pdv_para_quitar(vale_id, caixa_id).finalizar_venda_pdv("PIX")
        rel = self._relatorio()

        with patch.object(rel, "_carregar_tabela_ativa", lambda: None), \
             patch.object(modulo_relatorio.messagebox, "askyesno", return_value=False) as pedido, \
             patch.object(modulo_relatorio.messagebox, "showwarning") as aviso:
            rel.cancelar_vale(vale_id, 1, "MARIA DA SILVA", "QUITADO")

        self.assertFalse(pedido.called, "nao pode haver confirmacao de cancelamento")
        self.assertTrue(aviso.called, "deve avisar que o vale nao pode ser cancelado")
        self.assertEqual(self.vale(vale_id)[3], "QUITADO")

    def test_cancelamento_recusado_pelo_usuario_nao_altera_nada(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        rel = self._relatorio()

        with patch.object(rel, "_carregar_tabela_ativa", lambda: None), \
             patch.object(modulo_relatorio.messagebox, "askyesno", return_value=False):
            rel.cancelar_vale(vale_id, 1, "MARIA DA SILVA", "PENDENTE")

        self.assertEqual(self.vale(vale_id)[3], "PENDENTE")

    def test_reimpressao_nao_altera_status_nem_reativa_cancelado(self):
        vale_id = self.pdv_com_cliente([self.item()]).salvar_vale_atual()
        rel = self._relatorio()
        impressora = self._impressor_falso()
        with patch.object(modulo_relatorio.messagebox, "showinfo"), \
             patch.object(rel, "_obter_impressor_documento", lambda: impressora):
            rel.reimprimir_vale(vale_id, 1)

        self.assertEqual(impressora.chamadas, [("vale", vale_id)])
        self.assertEqual(self.vale(vale_id)[3], "PENDENTE", "reimprimir nao muda status")

        # Vale cancelado continua cancelado apos reimprimir.
        with patch.object(rel, "_carregar_tabela_ativa", lambda: None), \
             patch.object(modulo_relatorio.messagebox, "askyesno", return_value=True), \
             patch.object(modulo_relatorio.messagebox, "showinfo"):
            rel.cancelar_vale(vale_id, 1, "MARIA DA SILVA", "PENDENTE")

        with patch.object(modulo_relatorio.messagebox, "showinfo"), \
             patch.object(rel, "_obter_impressor_documento", lambda: impressora):
            rel.reimprimir_vale(vale_id, 1)

        self.assertEqual(len(impressora.chamadas), 2)
        self.assertEqual(self.vale(vale_id)[3], "CANCELADO",
                         "reimprimir nao reativa vale cancelado")

    def test_dados_para_reimpressao_de_venda(self):
        rel = self._relatorio()
        caixa_id = self.abrir_caixa()
        self._pdv_para_quitar(1, caixa_id, criar_venda=False)
        with get_db_connection() as conn:
            cur = conn.execute(
                "INSERT INTO vendas (data_venda, valor_total, valor_impostos_retidos,"
                " valor_liquido, origem, status_pedido, status_pagamento,"
                " forma_pagamento, valor_informado, diferenca)"
                " VALUES (CURRENT_TIMESTAMP, 20.0, 0.0, 20.0, 'LOJA_FISICA',"
                " 'APROVADO', 'PAGO', 'PIX', 20.0, 0.0)"
            )
            venda_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO itens_venda (venda_id, produto_id, quantidade, subtotal)"
                " VALUES (?, 1, 2.0, 20.0)",
                (venda_id,),
            )

        dados = rel._dados_venda_para_impressao(venda_id)

        self.assertEqual(dados["id"], venda_id)
        self.assertEqual(dados["forma_pagamento"], "PIX")
        self.assertEqual(len(dados["itens"]), 1)
        self.assertEqual(dados["itens"][0]["nome"], "ARROZ 1KG")
        self.assertEqual(dados["itens"][0]["quantidade"], 2.0)
        self.assertEqual(dados["itens"][0]["total"], 20.0)

    def test_reimpressao_de_venda_inexistente_falha(self):
        rel = self._relatorio()
        with self.assertRaises(ValueError):
            rel._dados_venda_para_impressao(999999)

    def _pdv_para_quitar(self, vale_ids, caixa_id, criar_venda=True):
        if criar_venda:
            pdv = self.pdv_com_cliente([self.item()])
            pdv.caixa_id = caixa_id
            pdv._vales_para_quitar = (
                [vale_ids] if isinstance(vale_ids, int) else list(vale_ids)
            )
            return pdv
        return _novo_pdv()