import sys
import traceback
from datetime import datetime

import modulo_financeiro
import modulo_pdv
from database_manager import get_db_connection


class _DummyEntry:
    def delete(self, *_args, **_kwargs):
        return None


class _DummyLabel:
    def configure(self, **_kwargs):
        return None


class _DummyFiscal:
    def exportar_venda(self, _venda_id, _itens, _forma_pgto, _valor_bruto):
        return True, None


class _PDVStub:
    def __init__(self, itens):
        self.itens_carrinho = itens
        self.fiscal = _DummyFiscal()
        self.ent_valor_pago = _DummyEntry()
        self.lbl_troco_venda = _DummyLabel()
        self.ent_quantidade = object()
        self._status = []

    def exportar_venda_fiscal(self, _dados):
        return True

    def _set_status(self, mensagem, cor):
        self._status.append((mensagem, cor))

    def _formatar_moeda_br(self, valor):
        return f"R$ {float(valor):.2f}"

    def _executar_automacao_pos_venda(self, _dados):
        return None

    def _renderizar_carrinho(self):
        return None

    def atualizar_total_display(self):
        return None

    def _avaliar_limite_caixa(self):
        return None

    def _safe_focus(self, _widget):
        return None


def _assert(condicao, mensagem):
    if not condicao:
        raise AssertionError(mensagem)


def _quase_igual(a, b, eps=0.01):
    return abs(float(a) - float(b)) <= eps


def teste_calculo_impostos_ncm():
    prefixo_teste = "991234"
    valor_antigo = None

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT aliquota_percentual, descricao, ativo FROM config_aliquotas_ncm WHERE ncm_prefixo = ?",
            (prefixo_teste,),
        ).fetchone()
        if row:
            valor_antigo = (float(row[0] or 0.0), str(row[1] or ""), int(row[2] or 0))

    modulo_financeiro.atualizar_aliquota_ncm(prefixo_teste, 10.0, "Teste automatizado", 1)

    try:
        resultado = modulo_pdv.calcular_impostos_liquidos(100.0, "99123456")
        _assert(_quase_igual(resultado["aliquota"], 10.0), f"Aliquota incorreta: {resultado}")
        _assert(_quase_igual(resultado["valor_imposto"], 10.0), f"Imposto incorreto: {resultado}")
        _assert(_quase_igual(resultado["valor_liquido"], 90.0), f"Liquido incorreto: {resultado}")
    finally:
        with get_db_connection() as conn:
            if valor_antigo is None:
                conn.execute("DELETE FROM config_aliquotas_ncm WHERE ncm_prefixo = ?", (prefixo_teste,))
            else:
                conn.execute(
                    """
                    INSERT INTO config_aliquotas_ncm (ncm_prefixo, aliquota_percentual, descricao, ativo, atualizado_em)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(ncm_prefixo)
                    DO UPDATE SET
                        aliquota_percentual = excluded.aliquota_percentual,
                        descricao = excluded.descricao,
                        ativo = excluded.ativo,
                        atualizado_em = CURRENT_TIMESTAMP
                    """,
                    (prefixo_teste, valor_antigo[0], valor_antigo[1], valor_antigo[2]),
                )


def teste_cadastro_cliente_fornecedor_sqlite():
    sufixo = datetime.now().strftime("%Y%m%d%H%M%S%f")
    nome_cliente = f"TESTE_CLIENTE_{sufixo}"
    nome_fornecedor = f"TESTE_FORNECEDOR_{sufixo}"
    cliente_id = None
    fornecedor_id = None

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO clientes (nome, documento, telefone, email, endereco) VALUES (?, ?, ?, ?, ?)",
                (nome_cliente, "12345678900", "11999990000", "cliente.teste@frs.local", "Rua Teste, 100"),
            )
            cliente_id = int(cur.lastrowid)

            cur.execute(
                "INSERT INTO fornecedores (nome, cnpj_cpf, telefone, email, endereco) VALUES (?, ?, ?, ?, ?)",
                (nome_fornecedor, "12345678000190", "11988887777", "fornecedor.teste@frs.local", "Av Teste, 200"),
            )
            fornecedor_id = int(cur.lastrowid)

            cliente = conn.execute("SELECT nome, documento FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
            fornecedor = conn.execute(
                "SELECT nome, cnpj_cpf FROM fornecedores WHERE id = ?", (fornecedor_id,)
            ).fetchone()

        _assert(cliente is not None, "Cliente nao encontrado apos salvar.")
        _assert(fornecedor is not None, "Fornecedor nao encontrado apos salvar.")
        _assert(cliente[0] == nome_cliente, f"Nome de cliente divergente: {cliente}")
        _assert(fornecedor[0] == nome_fornecedor, f"Nome de fornecedor divergente: {fornecedor}")
    finally:
        with get_db_connection() as conn:
            if cliente_id:
                conn.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))
            if fornecedor_id:
                conn.execute("DELETE FROM fornecedores WHERE id = ?", (fornecedor_id,))


def teste_fluxo_venda_completo_e_dashboard():
    sufixo = datetime.now().strftime("%Y%m%d%H%M%S%f")
    codigo = f"AUTOTESTE_{sufixo}"
    nome_produto = f"PRODUTO_AUTOTESTE_{sufixo}"
    produto_id = None
    venda_id = None
    venda_dia_id = None
    estoque_inicial = 10
    quantidade_vendida = 2

    total_antes = modulo_financeiro.obter_total_vendas_dia()

    prefixo_teste = "991234"
    valor_antigo = None
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT aliquota_percentual, descricao, ativo FROM config_aliquotas_ncm WHERE ncm_prefixo = ?",
            (prefixo_teste,),
        ).fetchone()
        if row:
            valor_antigo = (float(row[0] or 0.0), str(row[1] or ""), int(row[2] or 0))

    modulo_financeiro.atualizar_aliquota_ncm(prefixo_teste, 10.0, "Teste fluxo venda", 1)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO produtos (
                    codigo_barras, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda,
                    quantidade_atual, quantidade_minima, validade, categoria, preco_base, inicio_promocao, fim_promocao, imagem_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    codigo,
                    nome_produto,
                    "UN",
                    "99123456",
                    20.0,
                    50.0,
                    50.0,
                    estoque_inicial,
                    1,
                    None,
                    "AUTOTESTE",
                    50.0,
                    None,
                    None,
                    None,
                ),
            )
            produto_id = int(cur.lastrowid)

        itens = [
            {
                "id": produto_id,
                "quantidade": quantidade_vendida,
                "preco": 50.0,
                "total": 100.0,
                "ncm": "99123456",
                "descricao": nome_produto,
            }
        ]

        stub = _PDVStub(itens)
        modulo_pdv.ModuloPDV.finalizar_venda_pdv(
            stub,
            forma_pgto="PIX",
            valor_pago=100.0,
            imprimir_cupom=False,
            origem_venda="IFOOD",
        )

        with get_db_connection() as conn:
            venda = conn.execute(
                """
                SELECT id, valor_total, status_pedido, status_pagamento, origem
                FROM vendas
                WHERE forma_pagamento = 'PIX' AND origem = 'IFOOD'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            _assert(venda is not None, "Venda nao foi gravada na tabela vendas.")

            venda_id = int(venda[0])
            _assert(_quase_igual(venda[1], 100.0), f"Valor total incorreto em vendas: {venda}")
            _assert(str(venda[2]).upper() == "APROVADO", f"status_pedido esperado APROVADO, recebido: {venda[2]}")
            _assert(str(venda[3]).upper() == "PAGO", f"status_pagamento esperado PAGO, recebido: {venda[3]}")

            venda_dia = conn.execute(
                """
                SELECT id, status_pedido, status_pagamento
                FROM vendas_dia
                WHERE forma_pagamento = 'PIX' AND origem = 'IFOOD'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            _assert(venda_dia is not None, "Venda nao foi gravada na tabela vendas_dia.")
            venda_dia_id = int(venda_dia[0])
            _assert(str(venda_dia[1]).upper() == "APROVADO", "status_pedido em vendas_dia nao esta APROVADO.")
            _assert(str(venda_dia[2]).upper() == "PAGO", "status_pagamento em vendas_dia nao esta PAGO.")

            estoque_atual = conn.execute(
                "SELECT quantidade_atual FROM produtos WHERE id = ?",
                (produto_id,),
            ).fetchone()
            _assert(estoque_atual is not None, "Produto de teste nao encontrado apos venda.")
            esperado = estoque_inicial - quantidade_vendida
            _assert(int(estoque_atual[0]) == esperado, f"Baixa de estoque incorreta: esperado {esperado}, obtido {estoque_atual[0]}")

        total_depois = modulo_financeiro.obter_total_vendas_dia()
        incremento = float(total_depois) - float(total_antes)
        _assert(incremento >= 99.99, f"Dashboard total nao atualizou como esperado. Incremento={incremento:.2f}")
    finally:
        with get_db_connection() as conn:
            if venda_id:
                conn.execute("DELETE FROM itens_venda WHERE venda_id = ?", (venda_id,))
                conn.execute("DELETE FROM vendas WHERE id = ?", (venda_id,))
                conn.execute("DELETE FROM financeiro WHERE descricao LIKE ?", (f"Venda PDV #{venda_id} (%",))
            if venda_dia_id:
                conn.execute("DELETE FROM vendas_dia WHERE id = ?", (venda_dia_id,))
            if produto_id:
                conn.execute("DELETE FROM produtos WHERE id = ?", (produto_id,))

            if valor_antigo is None:
                conn.execute("DELETE FROM config_aliquotas_ncm WHERE ncm_prefixo = ?", (prefixo_teste,))
            else:
                conn.execute(
                    """
                    INSERT INTO config_aliquotas_ncm (ncm_prefixo, aliquota_percentual, descricao, ativo, atualizado_em)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(ncm_prefixo)
                    DO UPDATE SET
                        aliquota_percentual = excluded.aliquota_percentual,
                        descricao = excluded.descricao,
                        ativo = excluded.ativo,
                        atualizado_em = CURRENT_TIMESTAMP
                    """,
                    (prefixo_teste, valor_antigo[0], valor_antigo[1], valor_antigo[2]),
                )


def main():
    testes = [
        ("Calculo NCM", teste_calculo_impostos_ncm),
        ("Cadastro Cliente/Fornecedor SQLite", teste_cadastro_cliente_fornecedor_sqlite),
        ("Fluxo completo venda + dashboard", teste_fluxo_venda_completo_e_dashboard),
    ]

    aprovados = 0
    falhas = []

    for nome, fn in testes:
        try:
            fn()
            aprovados += 1
            print(f"[OK] {nome}")
        except Exception as exc:
            falhas.append((nome, exc, traceback.format_exc()))
            print(f"[FALHA] {nome}: {exc}")

    if falhas:
        print("\n=== ERROS DETECTADOS ===")
        for nome, _exc, tb in falhas:
            print(f"\nTeste: {nome}\n{tb}")
        print(f"\nResultado: FALHA ({aprovados}/{len(testes)} aprovados)")
        return 1

    print(f"\nResultado: OK ({aprovados}/{len(testes)} aprovados)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
