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


def obter_resumo_fluxo_caixa_dia():
    """Retorna resumo diário com valores bruto, impostos retidos e líquido."""
    resumo = {"valor_bruto": 0.0, "valor_impostos": 0.0, "valor_liquido": 0.0}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(valor_total), 0.0),
                    COALESCE(SUM(valor_impostos_retidos), 0.0),
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
            bruto, impostos, liquido = cursor.fetchone()

            if float(bruto or 0.0) <= 0:
                cursor.execute(
                    """
                    SELECT
                        COALESCE(SUM(valor_total), 0.0),
                        COALESCE(SUM(valor_impostos_retidos), 0.0),
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
                bruto, impostos, liquido = cursor.fetchone()

        resumo["valor_bruto"] = float(bruto or 0.0)
        resumo["valor_impostos"] = float(impostos or 0.0)
        resumo["valor_liquido"] = float(liquido or 0.0)
        return resumo
    except Exception as e:
        registrar_log(None, "Resumo Fluxo Caixa", "Falha", f"Erro: {e}")
        return resumo


def obter_resumo_fluxo_caixa_periodo(data_ini, data_fim):
    """Retorna resumo por período para relatório fiscal/BI."""
    resumo = {"valor_bruto": 0.0, "valor_impostos": 0.0, "valor_liquido": 0.0}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(valor_total), 0.0),
                    COALESCE(SUM(valor_impostos_retidos), 0.0),
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
            bruto, impostos, liquido = cursor.fetchone()

            if float(bruto or 0.0) <= 0:
                cursor.execute(
                    """
                    SELECT
                        COALESCE(SUM(valor_total), 0.0),
                        COALESCE(SUM(valor_impostos_retidos), 0.0),
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
                bruto, impostos, liquido = cursor.fetchone()

        resumo["valor_bruto"] = float(bruto or 0.0)
        resumo["valor_impostos"] = float(impostos or 0.0)
        resumo["valor_liquido"] = float(liquido or 0.0)
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

def obter_total_vendas_dia():
    """Retorna o total vendido hoje usando vendas como fonte primária."""
    resumo = obter_resumo_fluxo_caixa_dia()
    return resumo["valor_bruto"]

def obter_taxas():
    """Retorna dicionário com as taxas configuradas."""
    taxas = {"DEBITO": 0.0, "CREDITO": 0.0}
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
        self.geometry("400x300")
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

        def salvar():
            try:
                deb = parse_numero(self.ent_debito.get(), "Taxa Débito", minimo=0)
                cre = parse_numero(self.ent_credito.get(), "Taxa Crédito", minimo=0)
                
                with get_db_connection() as conn:
                    conn.execute("UPDATE config_taxas SET percentual = ? WHERE tipo = 'DEBITO'", (deb,))
                    conn.execute("UPDATE config_taxas SET percentual = ? WHERE tipo = 'CREDITO'", (cre,))
                
                messagebox.showinfo("Sucesso", "Taxas atualizadas globalmente.")
                registrar_log(usuario_atual.get("id"), "Config Taxas", "Sucesso", f"Débito: {deb}%, Crédito: {cre}%")
                self.destroy()
            except ValueError:
                messagebox.showerror("Erro", "Insira valores numéricos válidos (ex: 2,99)")

        ctk.CTkButton(self, text="SALVAR CONFIGURAÇÃO", fg_color="green", command=salvar).pack(pady=20)

def fechar_caixa():
    """Consolida vendas_dia no financeiro e limpa a tabela temporária."""
    resumo = obter_resumo_fluxo_caixa_dia()
    total_bruto = resumo["valor_bruto"]
    total_impostos = resumo["valor_impostos"]
    total_liquido = resumo["valor_liquido"]
    
    if total_bruto <= 0:
        return False, "Não há vendas registradas para fechamento."

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Inserir no financeiro
            data_atual = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                INSERT INTO financeiro (data_registro, valor, tipo, valor_bruto, valor_impostos_retidos, descricao)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                data_atual,
                total_liquido,
                'Entrada',
                total_bruto,
                total_impostos,
                f"Fechamento de Caixa - {data_atual[:10]} | Bruto: {total_bruto:.2f} | Impostos: {total_impostos:.2f} | Liquido: {total_liquido:.2f}",
            ))
            
            # 2. Arquivar/Limpar vendas_dia 
            cursor.execute("DELETE FROM vendas_dia")
            
        return True, (
            f"Caixa fechado com sucesso! Bruto: R$ {total_bruto:.2f} | "
            f"Impostos: R$ {total_impostos:.2f} | Liquido: R$ {total_liquido:.2f}"
        )
    except Exception as e:
        # get_db_connection já faz o rollback
        registrar_log(None, "Fechamento de Caixa (Consolidação)", "Falha", f"Erro: {e}")
        return False, f"Erro ao fechar caixa: {e}"