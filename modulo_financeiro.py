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

def obter_total_vendas_dia():
    """Retorna a soma de todas as vendas na tabela vendas_dia."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(valor_total) FROM vendas_dia")
            resultado = cursor.fetchone()[0]
        return resultado if resultado else 0.0
    except Exception as e:
        registrar_log(None, "Obter Total Vendas Dia", "Falha", f"Erro: {e}")
        print(f"Erro ao buscar vendas do dia: {e}")
        return 0.0

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
    total = obter_total_vendas_dia()
    
    if total <= 0:
        return False, "Não há vendas registradas para fechamento."

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Inserir no financeiro
            data_atual = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                INSERT INTO financeiro (data_registro, valor, tipo, descricao)
                VALUES (?, ?, ?, ?)
            ''', (data_atual, total, 'Entrada', f'Fechamento de Caixa - {data_atual[:10]}'))
            
            # 2. Arquivar/Limpar vendas_dia 
            cursor.execute("DELETE FROM vendas_dia")
            
        return True, f"Caixa fechado com sucesso! Total: R$ {total:.2f}"
    except Exception as e:
        # get_db_connection já faz o rollback
        registrar_log(None, "Fechamento de Caixa (Consolidação)", "Falha", f"Erro: {e}")
        return False, f"Erro ao fechar caixa: {e}"