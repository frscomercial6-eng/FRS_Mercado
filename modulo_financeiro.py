import sqlite3
import customtkinter as ctk
from tkinter import messagebox
from datetime import datetime
from database_manager import get_db_connection, registrar_log
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero

DB_PATH = 'mercado.db'


def formatar_percentual_inteiro(valor):
    try:
        return str(int(round(float(valor))))
    except Exception:
        return "0"


def _normalizar_ncm(ncm):
    return "".join(ch for ch in str(ncm or "") if ch.isdigit())


def obter_aliquota_por_ncm(ncm):
    """Busca alíquota configurada por prefixo de NCM, com fallback padrão (*)"""
    ncm_norm = _normalizar_ncm(ncm)
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ncm_prefixo, aliquota_percentual
                FROM config_aliquotas_ncm
                WHERE ativo = 1
                ORDER BY CASE WHEN ncm_prefixo = '*' THEN 0 ELSE LENGTH(ncm_prefixo) END DESC
                """
            )
            regras = cursor.fetchall()
    except Exception as e:
        registrar_log(None, "Aliquota NCM", "Falha", f"Erro ao obter alíquota: {e}")
        return 0.0

    for prefixo, aliquota in regras:
        prefixo_txt = str(prefixo or "").strip()
        if prefixo_txt == "*":
            return float(aliquota or 0.0)
        if ncm_norm.startswith(prefixo_txt):
            return float(aliquota or 0.0)

    return 0.0


def listar_aliquotas_ncm():
    """Lista parâmetros de alíquotas para manutenção externa (admin/futuras telas)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ncm_prefixo, aliquota_percentual, descricao, ativo
                FROM config_aliquotas_ncm
                ORDER BY CASE WHEN ncm_prefixo='*' THEN 999 ELSE LENGTH(ncm_prefixo) END DESC, ncm_prefixo
                """
            )
            return cursor.fetchall()
    except Exception as e:
        registrar_log(None, "Aliquota NCM", "Falha", f"Erro ao listar alíquotas: {e}")
        return []


def atualizar_aliquota_ncm(ncm_prefixo, aliquota_percentual, descricao="", ativo=1):
    """Cria/atualiza alíquota por prefixo de NCM sem necessidade de alterar código."""
    prefixo = str(ncm_prefixo or "").strip()
    if not prefixo:
        raise ValueError("Prefixo NCM é obrigatório (use '*' para padrão).")
    if prefixo != "*":
        prefixo = "".join(ch for ch in prefixo if ch.isdigit())
        if not prefixo:
            raise ValueError("Prefixo NCM inválido.")

    aliquota = parse_numero(aliquota_percentual, "Aliquota", permitir_vazio=False, default=0.0, minimo=0)
    ativo_flag = 1 if int(ativo) else 0

    with get_db_connection() as conn:
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
            (prefixo, aliquota, str(descricao or "").strip(), ativo_flag),
        )


def listar_aliquotas_fiscais_ncm():
    """Lista regras fiscais com ICMS/PIS/COFINS/IBS/CBS por prefixo de NCM."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    ncm_prefixo,
                    aliquota_icms,
                    aliquota_pis,
                    aliquota_cofins,
                    aliquota_ibs,
                    aliquota_cbs,
                    descricao,
                    ativo
                FROM config_aliquotas_fiscais_ncm
                ORDER BY CASE WHEN ncm_prefixo='*' THEN 999 ELSE LENGTH(ncm_prefixo) END DESC, ncm_prefixo
                """
            )
            return cursor.fetchall()
    except Exception as e:
        registrar_log(None, "Aliquotas Fiscais NCM", "Falha", f"Erro ao listar alíquotas fiscais: {e}")
        return []


def atualizar_aliquotas_fiscais_ncm(
    ncm_prefixo,
    aliquota_icms=0.0,
    aliquota_pis=0.0,
    aliquota_cofins=0.0,
    aliquota_ibs=0.0,
    aliquota_cbs=0.0,
    descricao="",
    ativo=1,
):
    """Cria/atualiza regras fiscais por NCM para facilitar ajustes na transição 2027+."""
    prefixo = str(ncm_prefixo or "").strip()
    if not prefixo:
        raise ValueError("Prefixo NCM é obrigatório (use '*' para padrão).")
    if prefixo != "*":
        prefixo = "".join(ch for ch in prefixo if ch.isdigit())
        if not prefixo:
            raise ValueError("Prefixo NCM inválido.")

    icms = parse_numero(aliquota_icms, "Aliquota ICMS", permitir_vazio=True, default=0.0, minimo=0)
    pis = parse_numero(aliquota_pis, "Aliquota PIS", permitir_vazio=True, default=0.0, minimo=0)
    cofins = parse_numero(aliquota_cofins, "Aliquota COFINS", permitir_vazio=True, default=0.0, minimo=0)
    ibs = parse_numero(aliquota_ibs, "Aliquota IBS", permitir_vazio=True, default=0.0, minimo=0)
    cbs = parse_numero(aliquota_cbs, "Aliquota CBS", permitir_vazio=True, default=0.0, minimo=0)
    ativo_flag = 1 if int(ativo) else 0

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO config_aliquotas_fiscais_ncm (
                ncm_prefixo, aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs, descricao, ativo, atualizado_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ncm_prefixo)
            DO UPDATE SET
                aliquota_icms = excluded.aliquota_icms,
                aliquota_pis = excluded.aliquota_pis,
                aliquota_cofins = excluded.aliquota_cofins,
                aliquota_ibs = excluded.aliquota_ibs,
                aliquota_cbs = excluded.aliquota_cbs,
                descricao = excluded.descricao,
                ativo = excluded.ativo,
                atualizado_em = CURRENT_TIMESTAMP
            """,
            (prefixo, icms, pis, cofins, ibs, cbs, str(descricao or "").strip(), ativo_flag),
        )


def _tem_coluna(conn, tabela, coluna):
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % tabela).fetchall()}
        return coluna in cols
    except Exception:
        return False


def _janela_caixa(conn, caixa_id):
    """Retorna (data_abertura, data_fechamento_ou_agora) do caixa para fallback legado."""
    try:
        linha = conn.execute(
            "SELECT data_abertura, COALESCE(data_fechamento, datetime('now','localtime')) FROM caixa_operacao WHERE id = ?",
            (caixa_id,),
        ).fetchone()
        if linha and linha[0]:
            return str(linha[0]), str(linha[1])
    except Exception:
        pass
    return None, None


def _totais_vendas_tabela_caixa(conn, tabela, caixa_id, abertura, fechamento):
    """Soma vendas de UMA tabela restritas ao ciclo do caixa.

    Preferência: caixa_operacao_id = caixa_id (dado novo, preciso).
    Fallback legado: linhas sem vínculo (caixa_operacao_id IS NULL) dentro da
    janela [data_abertura, data_fechamento] — cobre bases antigas sem a coluna
    sem jamais puxar ciclos anteriores já vinculados.
    """
    tem_vinculo = _tem_coluna(conn, tabela, "caixa_operacao_id")
    base_select = (
        "COALESCE(SUM(valor_total), 0.0), COALESCE(SUM(valor_impostos_retidos), 0.0), "
        "COALESCE(SUM(valor_icms), 0.0), COALESCE(SUM(valor_pis), 0.0), "
        "COALESCE(SUM(valor_cofins), 0.0), COALESCE(SUM(valor_ibs), 0.0), "
        "COALESCE(SUM(valor_cbs), 0.0), COALESCE(SUM(CASE WHEN COALESCE(valor_liquido, 0) = 0 "
        "AND COALESCE(valor_impostos_retidos, 0) = 0 THEN valor_total ELSE valor_liquido END), 0.0)"
    )
    if tem_vinculo and abertura and fechamento:
        row = conn.execute(
            "SELECT %s FROM %s WHERE (caixa_operacao_id = ? OR "
            "(caixa_operacao_id IS NULL AND data_venda >= ? AND data_venda <= ?))" % (base_select, tabela),
            (caixa_id, abertura, fechamento),
        ).fetchone()
    elif tem_vinculo:
        row = conn.execute(
            "SELECT %s FROM %s WHERE caixa_operacao_id = ?" % (base_select, tabela),
            (caixa_id,),
        ).fetchone()
    elif abertura and fechamento:
        row = conn.execute(
            "SELECT %s FROM %s WHERE data_venda >= ? AND data_venda <= ?" % (base_select, tabela),
            (abertura, fechamento),
        ).fetchone()
    else:
        row = conn.execute("SELECT %s FROM %s" % (base_select, tabela)).fetchone()
    return [float(v or 0.0) for v in (row or (0,) * 8)]


def _vendas_por_forma_tabela_caixa(conn, tabela, caixa_id, abertura, fechamento):
    tem_vinculo = _tem_coluna(conn, tabela, "caixa_operacao_id")
    if tem_vinculo and abertura and fechamento:
        return conn.execute(
            "SELECT UPPER(COALESCE(forma_pagamento, '')), COALESCE(SUM(valor_total), 0.0) "
            "FROM %s WHERE (caixa_operacao_id = ? OR "
            "(caixa_operacao_id IS NULL AND data_venda >= ? AND data_venda <= ?)) "
            "GROUP BY UPPER(COALESCE(forma_pagamento, ''))" % tabela,
            (caixa_id, abertura, fechamento),
        ).fetchall()
    if tem_vinculo:
        return conn.execute(
            "SELECT UPPER(COALESCE(forma_pagamento, '')), COALESCE(SUM(valor_total), 0.0) "
            "FROM %s WHERE caixa_operacao_id = ? "
            "GROUP BY UPPER(COALESCE(forma_pagamento, ''))" % tabela,
            (caixa_id,),
        ).fetchall()
    if abertura and fechamento:
        return conn.execute(
            "SELECT UPPER(COALESCE(forma_pagamento, '')), COALESCE(SUM(valor_total), 0.0) "
            "FROM %s WHERE data_venda >= ? AND data_venda <= ? "
            "GROUP BY UPPER(COALESCE(forma_pagamento, ''))" % tabela,
            (abertura, fechamento),
        ).fetchall()
    return conn.execute(
        "SELECT UPPER(COALESCE(forma_pagamento, '')), COALESCE(SUM(valor_total), 0.0) "
        "FROM %s GROUP BY UPPER(COALESCE(forma_pagamento, ''))" % tabela,
    ).fetchall()


def _movs_caixa(conn, caixa_id, abertura, fechamento):
    """Lista sangrias e reforcos (suprimentos) EXCLUSIVOS do ciclo.

    Retorna dict com totais e listas [(HH:MM, valor)] ordenadas.
    Sangria: tabela `sangrias` (caixa_operacao_id; fallback legado janela).
    Reforco: `financeiro` Entrada 'Suprimento:%' (caixa_operacao_id;
    fallback legado janela por data_registro).
    """
    sang_list, ref_list = [], []
    try:
        if _tem_coluna(conn, "sangrias", "caixa_operacao_id"):
            if abertura and fechamento:
                rows = conn.execute(
                    "SELECT valor, substr(COALESCE(data_sangria,''),12,5) FROM sangrias "
                    "WHERE (caixa_operacao_id = ? OR (caixa_operacao_id IS NULL "
                    "AND data_sangria >= ? AND data_sangria <= ?)) ORDER BY data_sangria, id",
                    (caixa_id, abertura, fechamento),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT valor, substr(COALESCE(data_sangria,''),12,5) FROM sangrias "
                    "WHERE caixa_operacao_id = ? ORDER BY data_sangria, id",
                    (caixa_id,),
                ).fetchall()
        elif abertura and fechamento:
            rows = conn.execute(
                "SELECT valor, substr(COALESCE(data_sangria,''),12,5) FROM sangrias "
                "WHERE data_sangria >= ? AND data_sangria <= ? ORDER BY data_sangria, id",
                (abertura, fechamento),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT valor, substr(COALESCE(data_sangria,''),12,5) FROM sangrias ORDER BY data_sangria, id"
            ).fetchall()
        for v, hh in rows:
            sang_list.append((str(hh or "--:--"), round(float(v or 0.0), 2)))
    except Exception:
        sang_list = []
    try:
        tem_cx = _tem_coluna(conn, "financeiro", "caixa_operacao_id")
        if tem_cx and abertura and fechamento:
            rows = conn.execute(
                "SELECT valor, substr(COALESCE(data_registro,''),12,5) FROM financeiro "
                "WHERE tipo = 'Entrada' AND descricao LIKE 'Suprimento:%' AND (caixa_operacao_id = ? OR "
                "(caixa_operacao_id IS NULL AND data_registro >= ? AND data_registro <= ?)) "
                "ORDER BY data_registro, id",
                (caixa_id, abertura, fechamento),
            ).fetchall()
        elif tem_cx:
            rows = conn.execute(
                "SELECT valor, substr(COALESCE(data_registro,''),12,5) FROM financeiro "
                "WHERE tipo = 'Entrada' AND descricao LIKE 'Suprimento:%' AND caixa_operacao_id = ? "
                "ORDER BY data_registro, id",
                (caixa_id,),
            ).fetchall()
        elif abertura and fechamento:
            rows = conn.execute(
                "SELECT valor, substr(COALESCE(data_registro,''),12,5) FROM financeiro "
                "WHERE tipo = 'Entrada' AND descricao LIKE 'Suprimento:%' "
                "AND data_registro >= ? AND data_registro <= ? ORDER BY data_registro, id",
                (abertura, fechamento),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT valor, substr(COALESCE(data_registro,''),12,5) FROM financeiro "
                "WHERE tipo = 'Entrada' AND descricao LIKE 'Suprimento:%' ORDER BY data_registro, id"
            ).fetchall()
        for v, hh in rows:
            ref_list.append((str(hh or "--:--"), round(float(v or 0.0), 2)))
    except Exception:
        ref_list = []
    return {
        "sangrias": sang_list,
        "reforcos": ref_list,
        "total_sangrias": round(sum(v for _, v in sang_list), 2),
        "total_reforcos": round(sum(v for _, v in ref_list), 2),
    }


def obter_movimentacoes_caixa(caixa_id):
    """API publica: movimentacoes (sangrias/reforcos) exclusivas do caixa."""
    try:
        with get_db_connection() as conn:
            abertura, fechamento = _janela_caixa(conn, caixa_id)
            return _movs_caixa(conn, caixa_id, abertura, fechamento)
    except Exception:
        return {"sangrias": [], "reforcos": [], "total_sangrias": 0.0, "total_reforcos": 0.0}


def obter_resumo_fluxo_caixa_dia(caixa_id=None):
    """Retorna resumo com valores bruto, impostos retidos e líquido.

    Com caixa_id: soma SOMENTE o ciclo do caixa (vendas + vendas_dia).
    Sem caixa_id: comportamento legado (dia atual) para relatórios/BI.
    """
    resumo = {
        "valor_bruto": 0.0,
        "valor_impostos": 0.0,
        "valor_liquido": 0.0,
        "valor_icms": 0.0,
        "valor_pis": 0.0,
        "valor_cofins": 0.0,
        "valor_ibs": 0.0,
        "valor_cbs": 0.0,
    }
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if caixa_id is not None:
                abertura, fechamento = _janela_caixa(conn, caixa_id)
                tot_v = _totais_vendas_tabela_caixa(conn, "vendas", caixa_id, abertura, fechamento)
                tot_d = _totais_vendas_tabela_caixa(conn, "vendas_dia", caixa_id, abertura, fechamento)
                # vendas_dia é espelho ágil do dia; quando a venda já está
                # vinculada em `vendas`, somar as duas tabelas duplicaria.
                # Usa vendas_dia apenas se `vendas` não tem nada no ciclo.
                tot = tot_d if tot_v[0] <= 0 < tot_d[0] else tot_v
                bruto, impostos, icms, pis, cofins, ibs, cbs, liquido = tot
            else:
                cursor.execute(
                    """
                    SELECT
                        COALESCE(SUM(valor_total), 0.0),
                        COALESCE(SUM(valor_impostos_retidos), 0.0),
                        COALESCE(SUM(valor_icms), 0.0),
                        COALESCE(SUM(valor_pis), 0.0),
                        COALESCE(SUM(valor_cofins), 0.0),
                        COALESCE(SUM(valor_ibs), 0.0),
                        COALESCE(SUM(valor_cbs), 0.0),
                        COALESCE(SUM(
                            CASE
                                WHEN COALESCE(valor_liquido, 0) = 0 AND COALESCE(valor_impostos_retidos, 0) = 0
                                    THEN valor_total
                                ELSE valor_liquido
                            END
                        ), 0.0)
                    FROM vendas
                    WHERE date(data_venda) = date('now', 'localtime')
                    """
                )
                bruto, impostos, icms, pis, cofins, ibs, cbs, liquido = cursor.fetchone()

                if float(bruto or 0.0) <= 0:
                    cursor.execute(
                        """
                        SELECT
                            COALESCE(SUM(valor_total), 0.0),
                            COALESCE(SUM(valor_impostos_retidos), 0.0),
                            COALESCE(SUM(valor_icms), 0.0),
                            COALESCE(SUM(valor_pis), 0.0),
                            COALESCE(SUM(valor_cofins), 0.0),
                            COALESCE(SUM(valor_ibs), 0.0),
                            COALESCE(SUM(valor_cbs), 0.0),
                            COALESCE(SUM(
                                CASE
                                    WHEN COALESCE(valor_liquido, 0) = 0 AND COALESCE(valor_impostos_retidos, 0) = 0
                                        THEN valor_total
                                    ELSE valor_liquido
                                END
                            ), 0.0)
                        FROM vendas_dia
                        WHERE date(data_venda) = date('now', 'localtime')
                        """
                    )
                    bruto, impostos, icms, pis, cofins, ibs, cbs, liquido = cursor.fetchone()

        resumo["valor_bruto"] = float(bruto or 0.0)
        resumo["valor_impostos"] = float(impostos or 0.0)
        resumo["valor_liquido"] = float(liquido or 0.0)
        resumo["valor_icms"] = float(icms or 0.0)
        resumo["valor_pis"] = float(pis or 0.0)
        resumo["valor_cofins"] = float(cofins or 0.0)
        resumo["valor_ibs"] = float(ibs or 0.0)
        resumo["valor_cbs"] = float(cbs or 0.0)
        return resumo
    except Exception as e:
        registrar_log(None, "Resumo Fluxo Caixa", "Falha", f"Erro: {e}")
        return resumo


def obter_resumo_fluxo_caixa_periodo(data_ini, data_fim):
    """Retorna resumo por período para relatório fiscal/BI."""
    resumo = {
        "valor_bruto": 0.0,
        "valor_impostos": 0.0,
        "valor_liquido": 0.0,
        "valor_icms": 0.0,
        "valor_pis": 0.0,
        "valor_cofins": 0.0,
        "valor_ibs": 0.0,
        "valor_cbs": 0.0,
    }
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(valor_total), 0.0),
                    COALESCE(SUM(valor_impostos_retidos), 0.0),
                    COALESCE(SUM(valor_icms), 0.0),
                    COALESCE(SUM(valor_pis), 0.0),
                    COALESCE(SUM(valor_cofins), 0.0),
                    COALESCE(SUM(valor_ibs), 0.0),
                    COALESCE(SUM(valor_cbs), 0.0),
                    COALESCE(SUM(
                        CASE
                            WHEN COALESCE(valor_liquido, 0) = 0 AND COALESCE(valor_impostos_retidos, 0) = 0
                                THEN valor_total
                            ELSE valor_liquido
                        END
                    ), 0.0)
                FROM vendas
                WHERE date(data_venda) BETWEEN date(?) AND date(?)
                """,
                (data_ini, data_fim),
            )
            bruto, impostos, icms, pis, cofins, ibs, cbs, liquido = cursor.fetchone()

            if float(bruto or 0.0) <= 0:
                cursor.execute(
                    """
                    SELECT
                        COALESCE(SUM(valor_total), 0.0),
                        COALESCE(SUM(valor_impostos_retidos), 0.0),
                        COALESCE(SUM(valor_icms), 0.0),
                        COALESCE(SUM(valor_pis), 0.0),
                        COALESCE(SUM(valor_cofins), 0.0),
                        COALESCE(SUM(valor_ibs), 0.0),
                        COALESCE(SUM(valor_cbs), 0.0),
                        COALESCE(SUM(
                            CASE
                                WHEN COALESCE(valor_liquido, 0) = 0 AND COALESCE(valor_impostos_retidos, 0) = 0
                                    THEN valor_total
                                ELSE valor_liquido
                            END
                        ), 0.0)
                    FROM vendas_dia
                    WHERE date(data_venda) BETWEEN date(?) AND date(?)
                    """,
                    (data_ini, data_fim),
                )
                bruto, impostos, icms, pis, cofins, ibs, cbs, liquido = cursor.fetchone()

        resumo["valor_bruto"] = float(bruto or 0.0)
        resumo["valor_impostos"] = float(impostos or 0.0)
        resumo["valor_liquido"] = float(liquido or 0.0)
        resumo["valor_icms"] = float(icms or 0.0)
        resumo["valor_pis"] = float(pis or 0.0)
        resumo["valor_cofins"] = float(cofins or 0.0)
        resumo["valor_ibs"] = float(ibs or 0.0)
        resumo["valor_cbs"] = float(cbs or 0.0)
        return resumo
    except Exception as e:
        registrar_log(None, "Resumo Fluxo Caixa Periodo", "Falha", f"Erro: {e}")
        return resumo


def obter_resumo_origem_dia():
    """Retorna totais líquidos do dia por origem de venda."""
    resultado = {"LOJA_FISICA": 0.0, "IFOOD": 0.0, "APP_PROPRIO": 0.0, "OUTROS": 0.0}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    UPPER(COALESCE(origem, 'LOJA_FISICA')) AS origem,
                    COALESCE(SUM(
                        CASE
                            WHEN COALESCE(valor_liquido, 0) = 0 AND COALESCE(valor_impostos_retidos, 0) = 0
                                THEN valor_total
                            ELSE valor_liquido
                        END
                    ), 0.0) AS total_liquido
                FROM vendas
                WHERE date(data_venda) = date('now', 'localtime')
                GROUP BY UPPER(COALESCE(origem, 'LOJA_FISICA'))
                """
            )
            for origem, total in cursor.fetchall():
                chave = str(origem or "LOJA_FISICA").upper()
                if chave in resultado:
                    resultado[chave] = float(total or 0.0)
                else:
                    resultado["OUTROS"] += float(total or 0.0)

        return resultado
    except Exception as e:
        registrar_log(None, "Resumo Origem Dia", "Falha", f"Erro: {e}")
        return resultado

def obter_total_vendas_dia(caixa_id=None):
    """Retorna o total vendido hoje usando vendas como fonte primária."""
    resumo = obter_resumo_fluxo_caixa_dia(caixa_id=caixa_id)
    return resumo["valor_bruto"]

def obter_vendas_dia_por_forma(caixa_id=None):
    """Retorna o total vendido agrupado por forma de pagamento.

    Com caixa_id: SOMENTE o ciclo do caixa (vendas + vendas_dia).
    Sem caixa_id: comportamento legado (dia atual) para relatórios/BI.
    Fonte primária: tabela `vendas`; fallback: `vendas_dia` (PDV ágil).
    Modalidades fora do conjunto padrão são somadas em 'OUTROS'.
    """
    MODALIDADES = ("DINHEIRO", "DEBITO", "CREDITO", "VOUCHER", "PIX")
    resultado = {m: 0.0 for m in MODALIDADES}
    resultado["OUTROS"] = 0.0

    def _normalizar(tipo, valor):
        chave = str(tipo or "").strip().upper()
        total = float(valor or 0.0)
        if chave in resultado:
            resultado[chave] += total
        else:
            resultado["OUTROS"] += total

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if caixa_id is not None:
                abertura, fechamento = _janela_caixa(conn, caixa_id)
                linhas_v = _vendas_por_forma_tabela_caixa(conn, "vendas", caixa_id, abertura, fechamento)
                # vendas_dia é espelho; soma as duas tabelas apenas quando
                # `vendas` está vazia no ciclo (evita duplicar o espelho).
                tem_vendas = any(float(t or 0.0) > 0 for _, t in linhas_v)
                linhas = list(linhas_v)
                if not tem_vendas:
                    linhas += list(_vendas_por_forma_tabela_caixa(conn, "vendas_dia", caixa_id, abertura, fechamento))
            else:
                cursor.execute(
                    """
                    SELECT UPPER(COALESCE(forma_pagamento, '')), COALESCE(SUM(valor_total), 0.0)
                    FROM vendas
                    WHERE date(data_venda) = date('now', 'localtime')
                    GROUP BY UPPER(COALESCE(forma_pagamento, ''))
                    """
                )
                linhas = cursor.fetchall()

                if not linhas:
                    cursor.execute(
                        """
                        SELECT UPPER(COALESCE(forma_pagamento, '')), COALESCE(SUM(valor_total), 0.0)
                        FROM vendas_dia
                        WHERE date(data_venda) = date('now', 'localtime')
                        GROUP BY UPPER(COALESCE(forma_pagamento, ''))
                        """
                    )
                    linhas = cursor.fetchall()

            for tipo, total in linhas:
                _normalizar(tipo, total)
    except Exception as e:
        registrar_log(None, "Resumo Vendas por Forma", "Falha", f"Erro: {e}")

    return resultado

def obter_taxas():
    """Retorna dicionário com as taxas configuradas."""
    taxas = {"DEBITO": 0.0, "CREDITO": 0.0, "VOUCHER": 0.0}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tipo, percentual FROM config_taxas")
            for tipo, valor in cursor.fetchall():
                taxas[tipo] = parse_numero(valor, "Taxa", permitir_vazio=True, default=0.0, minimo=0)
    except Exception as e:
        registrar_log(None, "Obter Taxas", "Falha", f"Erro: {e}")
    return taxas

class JanelaConfigTaxas(ctk.CTkToplevel):
    def __init__(self, master, usuario_atual):
        super().__init__(master)
        self.title("Configuração de Taxas de Cartão")
        self.geometry("400x360")
        self.grab_set()
        
        if usuario_atual.get("permissao") != "Administrador":
            messagebox.showerror("Acesso Negado", "Apenas administradores podem alterar taxas.")
            self.destroy()
            return

        taxas = obter_taxas()
        
        ctk.CTkLabel(self, text="CONFIGURAÇÃO DE TAXAS", font=("Arial", 16, "bold")).pack(pady=20)
        
        # Campos de Taxa
        self.frame_campos = ctk.CTkFrame(self)
        self.frame_campos.pack(padx=20, fill="x")

        ctk.CTkLabel(self.frame_campos, text="Taxa Débito (%):").grid(row=0, column=0, padx=10, pady=10)
        self.ent_debito = ctk.CTkEntry(self.frame_campos)
        self.ent_debito.insert(0, formatar_percentual_inteiro(taxas["DEBITO"]))
        self.ent_debito.grid(row=0, column=1, padx=10, pady=10)
        aplicar_padrao_entrada_numerica(self.ent_debito, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(self.frame_campos, text="Taxa Crédito (%):").grid(row=1, column=0, padx=10, pady=10)
        self.ent_credito = ctk.CTkEntry(self.frame_campos)
        self.ent_credito.insert(0, formatar_percentual_inteiro(taxas["CREDITO"]))
        self.ent_credito.grid(row=1, column=1, padx=10, pady=10)
        aplicar_padrao_entrada_numerica(self.ent_credito, inteiro=False, casas_decimais=2)

        ctk.CTkLabel(self.frame_campos, text="Taxa Voucher (%):").grid(row=2, column=0, padx=10, pady=10)
        self.ent_voucher = ctk.CTkEntry(self.frame_campos)
        self.ent_voucher.insert(0, formatar_percentual_inteiro(taxas.get("VOUCHER", 0.0)))
        self.ent_voucher.grid(row=2, column=1, padx=10, pady=10)
        aplicar_padrao_entrada_numerica(self.ent_voucher, inteiro=False, casas_decimais=2)

        def salvar():
            try:
                deb = parse_numero(self.ent_debito.get(), "Taxa Débito", minimo=0)
                cre = parse_numero(self.ent_credito.get(), "Taxa Crédito", minimo=0)
                vou = parse_numero(self.ent_voucher.get(), "Taxa Voucher", minimo=0)
                
                with get_db_connection() as conn:
                    conn.execute("UPDATE config_taxas SET percentual = ? WHERE tipo = 'DEBITO'", (deb,))
                    conn.execute("UPDATE config_taxas SET percentual = ? WHERE tipo = 'CREDITO'", (cre,))
                    conn.execute("INSERT OR IGNORE INTO config_taxas (tipo, percentual) VALUES ('VOUCHER', 0.0)")
                    conn.execute("UPDATE config_taxas SET percentual = ? WHERE tipo = 'VOUCHER'", (vou,))
                
                messagebox.showinfo("Sucesso", "Taxas atualizadas globalmente.")
                registrar_log(usuario_atual.get("id"), "Config Taxas", "Sucesso", f"Débito: {deb}%, Crédito: {cre}%, Voucher: {vou}%")
                self.destroy()
            except ValueError:
                messagebox.showerror("Erro", "Insira valores numéricos válidos (ex: 2,99)")

        ctk.CTkButton(self, text="SALVAR CONFIGURAÇÃO", fg_color="green", command=salvar).pack(pady=20)

def obter_divergencias_ultima_conferencia():
    """Retorna as divergências (informado - sistema) da última conferência de caixa.

    Lê a tabela `caixa_conferencia` e devolve (caixa_operacao_id, lista de
    dicionários por modalidade). Retorna (None, []) quando a tabela ainda não
    existe em bases antigas ou não há conferência registrada.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            linha = cursor.execute(
                "SELECT caixa_operacao_id FROM caixa_conferencia ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not linha:
                return None, []
            caixa_id = linha[0]
            linhas = cursor.execute(
                """
                SELECT modalidade, valor_sistema, valor_informado, diferenca
                FROM caixa_conferencia
                WHERE caixa_operacao_id = ?
                ORDER BY id
                """,
                (caixa_id,),
            ).fetchall()
        divergencias = [
            {
                "modalidade": str(modalidade or "").upper(),
                "valor_sistema": float(valor_sistema or 0.0),
                "valor_informado": float(valor_informado or 0.0),
                "diferenca": float(diferenca or 0.0),
            }
            for modalidade, valor_sistema, valor_informado, diferenca in linhas
        ]
        return caixa_id, divergencias
    except Exception as e:
        registrar_log(None, "Fechamento de Caixa (Divergências)", "Aviso", f"Conferência indisponível: {e}")
        return None, []


def fechar_caixa(caixa_id=None):
    """Consolida vendas do ciclo no financeiro (isolado por caixa)."""
    resumo = obter_resumo_fluxo_caixa_dia(caixa_id=caixa_id)
    total_bruto = resumo["valor_bruto"]
    total_impostos = resumo["valor_impostos"]
    total_liquido = resumo["valor_liquido"]
    
    caixa_conf_id, _divs_tmp = obter_divergencias_ultima_conferencia()
    if caixa_id is None:
        caixa_id = caixa_conf_id
        divergencias = _divs_tmp
    else:
        divergencias = []
        try:
            with get_db_connection() as _conn2:
                _linhas2 = _conn2.execute(
                    "SELECT modalidade, valor_sistema, valor_informado, diferenca "
                    "FROM caixa_conferencia WHERE caixa_operacao_id = ? ORDER BY id",
                    (caixa_id,),
                ).fetchall()
                divergencias = [
                    {"modalidade": str(m or "").upper(),
                     "valor_sistema": float(vs or 0.0),
                     "valor_informado": float(vi or 0.0),
                     "diferenca": float(df or 0.0)}
                    for m, vs, vi, df in _linhas2
                ]
        except Exception:
            divergencias = []
    divergencias_relevantes = [d for d in divergencias if abs(d["diferenca"]) >= 0.005]
    total_divergencias = round(sum(d["diferenca"] for d in divergencias_relevantes), 2)

    if total_bruto <= 0 and not divergencias_relevantes:
        try:
            with get_db_connection() as conn_z:
                if caixa_id is not None:
                    if _tem_coluna(conn_z, "vendas_dia", "caixa_operacao_id"):
                        _ab, _fe = _janela_caixa(conn_z, caixa_id)
                        if _ab and _fe:
                            conn_z.execute(
                                "DELETE FROM vendas_dia WHERE caixa_operacao_id = ? "
                                "OR (caixa_operacao_id IS NULL AND data_venda >= ? AND data_venda <= ?)",
                                (caixa_id, _ab, _fe),
                            )
                        else:
                            conn_z.execute("DELETE FROM vendas_dia WHERE caixa_operacao_id = ?", (caixa_id,))
        except Exception:
            pass
        return True, "Caixa fechado com sucesso! Sem movimentacao no ciclo."

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cols_fin = {r[1] for r in cursor.execute("PRAGMA table_info(financeiro)").fetchall()}
            
            data_atual = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 1. Inserir a consolidação das vendas do dia no financeiro
            if total_bruto > 0:
                descricao_consolidacao = (
                    f"Fechamento de Caixa - {data_atual[:10]} | Bruto: {total_bruto:.2f} | "
                    f"Impostos: {total_impostos:.2f} | Liquido: {total_liquido:.2f}"
                )
                if "caixa_operacao_id" in cols_fin:
                    cursor.execute('''
                        INSERT INTO financeiro (
                            data_registro, valor, tipo, valor_bruto,
                            valor_impostos_retidos, descricao, caixa_operacao_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        data_atual,
                        total_liquido,
                        'Entrada',
                        total_bruto,
                        total_impostos,
                        descricao_consolidacao,
                        caixa_id,
                    ))
                else:
                    cursor.execute('''
                        INSERT INTO financeiro (
                            data_registro, valor, tipo, valor_bruto,
                            valor_impostos_retidos, descricao
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        data_atual,
                        total_liquido,
                        'Entrada',
                        total_bruto,
                        total_impostos,
                        descricao_consolidacao,
                    ))
            
            # 2. Refletir as divergências da conferência analítica (sobra/falta por modalidade)
            for div in divergencias_relevantes:
                modalidade = div["modalidade"]
                diferenca = round(div["diferenca"], 2)
                tipo_movimento = 'Entrada' if diferenca > 0 else 'Saída'
                rotulo = 'Sobra' if diferenca > 0 else 'Falta'
                descricao_divergencia = (
                    f"Fechamento de Caixa {data_atual[:10]} #{caixa_id} - {rotulo} {modalidade} "
                    f"(Sistema: {div['valor_sistema']:.2f} | Informado: {div['valor_informado']:.2f})"
                )
                if "caixa_operacao_id" in cols_fin:
                    cursor.execute('''
                        INSERT INTO financeiro (
                            data_registro, valor, tipo, valor_bruto,
                            valor_impostos_retidos, descricao, caixa_operacao_id
                        )
                        VALUES (?, ?, ?, ?, 0.0, ?, ?)
                    ''', (
                        data_atual,
                        abs(diferenca),
                        tipo_movimento,
                        abs(diferenca),
                        descricao_divergencia,
                        caixa_id,
                    ))
                else:
                    cursor.execute('''
                        INSERT INTO financeiro (
                            data_registro, valor, tipo, valor_bruto,
                            valor_impostos_retidos, descricao
                        )
                        VALUES (?, ?, ?, ?, 0.0, ?)
                    ''', (
                        data_atual,
                        abs(diferenca),
                        tipo_movimento,
                        abs(diferenca),
                        descricao_divergencia,
                    ))

            # 3. Limpar SOMENTE vendas_dia do ciclo (isolamento entre ciclos)
            if caixa_id is not None and _tem_coluna(conn, "vendas_dia", "caixa_operacao_id"):
                _ab2, _fe2 = _janela_caixa(conn, caixa_id)
                if _ab2 and _fe2:
                    cursor.execute(
                        "DELETE FROM vendas_dia WHERE caixa_operacao_id = ? "
                        "OR (caixa_operacao_id IS NULL AND data_venda >= ? AND data_venda <= ?)",
                        (caixa_id, _ab2, _fe2),
                    )
                else:
                    cursor.execute("DELETE FROM vendas_dia WHERE caixa_operacao_id = ?", (caixa_id,))
            elif caixa_id is not None:
                _ab3, _fe3 = _janela_caixa(conn, caixa_id)
                if _ab3 and _fe3:
                    cursor.execute(
                        "DELETE FROM vendas_dia WHERE data_venda >= ? AND data_venda <= ?",
                        (_ab3, _fe3),
                    )
                else:
                    cursor.execute("DELETE FROM vendas_dia")
            else:
                cursor.execute("DELETE FROM vendas_dia")
            
        if divergencias_relevantes:
            detalhe = "; ".join(f"{d['modalidade']} {d['diferenca']:+.2f}" for d in divergencias_relevantes)
            registrar_log(
                None,
                "Fechamento de Caixa (Consolidação)",
                "Sucesso",
                f"Caixa {caixa_id} | Bruto: {total_bruto:.2f} | Liquido: {total_liquido:.2f} | "
                f"Ajuste por divergência: {total_divergencias:+.2f} | {detalhe}",
            )
            sufixo_divergencias = f" | Divergências: {detalhe} | Ajuste: R$ {total_divergencias:+.2f}"
        else:
            sufixo_divergencias = ""

        return True, (
            f"Caixa fechado com sucesso! Bruto: R$ {total_bruto:.2f} | "
            f"Impostos: R$ {total_impostos:.2f} | Liquido: R$ {total_liquido:.2f}{sufixo_divergencias}"
        )
    except Exception as e:
        # get_db_connection já faz o rollback
        registrar_log(None, "Fechamento de Caixa (Consolidação)", "Falha", f"Erro: {e}")
        return False, f"Erro ao fechar caixa: {e}"