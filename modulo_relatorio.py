import customtkinter as ctk
from tkinter import messagebox
import sqlite3
import csv
from datetime import datetime
import os
import shutil
from pathlib import Path

# ReportLab para PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Google Drive API dependencies are intentionally lazy. Relatórios locais must
# import and work even when any Google package is absent from the installation.
import pickle

from database_manager import (
    get_db_connection,
    get_db_path,
    registrar_log,
    GOOGLE_CREDS,
    obter_caminho_dados,
)
from modulo_config import carregar_configuracoes

class ModuloRelatorio(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("BI & Relatórios Estratégicos")
        # Geometria ajustada (cirurgico): a aba VALES exibe dois comandos
        # contextuais por linha ("Ver Itens" + "EXCLUIR VALE"). A largura
        # minima garante que nenhum dos botoes seja cortado/ocultado.
        self.geometry("1240x780")
        self.minsize(1180, 700)
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar relatórios.")
            self.destroy()
            return

        # Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # --- Filtros Temporais ---
        self.frame_filtros = ctk.CTkFrame(self)
        self.frame_filtros.grid(row=0, column=0, padx=20, pady=20, sticky="ew")

        ctk.CTkLabel(self.frame_filtros, text="Data Inicial (AAAA-MM-DD):").pack(side="left", padx=10)
        self.data_ini = ctk.CTkEntry(self.frame_filtros, width=120)
        self.data_ini.insert(0, datetime.now().strftime('%Y-%m-01'))
        self.data_ini.pack(side="left", padx=5)

        ctk.CTkLabel(self.frame_filtros, text="Data Final (AAAA-MM-DD):").pack(side="left", padx=10)
        self.data_fim = ctk.CTkEntry(self.frame_filtros, width=120)
        self.data_fim.insert(0, datetime.now().strftime('%Y-%m-%d'))
        self.data_fim.pack(side="left", padx=5)

        self.btn_filtrar = ctk.CTkButton(self.frame_filtros, text="Atualizar BI", command=self.atualizar_dados)
        self.btn_filtrar.pack(side="left", padx=20)

        self.btn_pdf = ctk.CTkButton(self.frame_filtros, text="Exportar PDF & Drive", fg_color="#2c3e50", command=self.gerar_e_subir_pdf)
        self.btn_pdf.pack(side="right", padx=10)

        self.btn_sped_base = ctk.CTkButton(
            self.frame_filtros,
            text="Esboço SPED (CSV)",
            fg_color="#5a3d00",
            command=self.gerar_esboco_sped,
        )
        self.btn_sped_base.pack(side="right", padx=10)

        self.btn_estorno = ctk.CTkButton(
            self.frame_filtros,
            text="Estornar Venda (ID)",
            fg_color="#8e2323",
            hover_color="#a63a3a",
            command=self.estornar_venda_dialog,
        )
        self.btn_estorno.pack(side="right", padx=10)

        # --- Dashboard de Cartões ---
        self.frame_cards = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_cards.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.frame_cards.grid_columnconfigure((0, 1, 2), weight=1)

        self.card_vendas = self.criar_card(self.frame_cards, "Valor Bruto", "R$ 0,00", "#1f77b4", 0)
        self.card_despesas = self.criar_card(self.frame_cards, "Impostos Retidos", "R$ 0,00", "#c0392b", 1)
        self.card_lucro = self.criar_card(self.frame_cards, "Valor Líquido", "R$ 0,00", "#27ae60", 2)

        # --- Seletor de Visão da Área Inferior ---
        self.frame_abas_tabela = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_abas_tabela.grid(row=2, column=0, padx=20, pady=(4, 2), sticky="ew")

        self.aba_ativa = "VENDAS_DIA"

        self.btn_aba_vendas = ctk.CTkButton(
            self.frame_abas_tabela,
            text="Vendas do Dia",
            width=150,
            height=32,
            font=("Roboto", 12, "bold"),
            fg_color="#1f77b4",
            hover_color="#16659e",
            command=lambda: self._mudar_aba_tabela("VENDAS_DIA"),
        )
        self.btn_aba_vendas.pack(side="left", padx=(0, 8))

        self.btn_aba_vales = ctk.CTkButton(
            self.frame_abas_tabela,
            text="Vales",
            width=140,
            height=32,
            font=("Roboto", 12, "bold"),
            fg_color="#333333",
            hover_color="#444444",
            command=lambda: self._mudar_aba_tabela("VALES"),
        )
        self.btn_aba_vales.pack(side="left", padx=8)

        self.btn_aba_estornos = ctk.CTkButton(
            self.frame_abas_tabela,
            text="Estornos",
            width=140,
            height=32,
            font=("Roboto", 12, "bold"),
            fg_color="#333333",
            hover_color="#444444",
            command=lambda: self._mudar_aba_tabela("ESTORNOS"),
        )
        self.btn_aba_estornos.pack(side="left", padx=8)

        # --- Visualização de Tabelas ---
        self.scroll_tabelas = ctk.CTkScrollableFrame(self)
        self.scroll_tabelas.grid(row=3, column=0, padx=20, pady=(4, 20), sticky="nsew")
        self.grid_rowconfigure(3, weight=2)

        self.atualizar_dados()

    def criar_card(self, master, titulo, valor, cor, col):
        f = ctk.CTkFrame(master, corner_radius=15, border_width=2, border_color=cor)
        f.grid(row=0, column=col, padx=10, pady=10, sticky="nsew")
        ctk.CTkLabel(f, text=titulo, font=("Arial", 14)).pack(pady=(15, 0))
        lbl_valor = ctk.CTkLabel(f, text=valor, font=("Arial", 28, "bold"), text_color=cor)
        lbl_valor.pack(pady=(5, 15))
        return lbl_valor

    def _mudar_aba_tabela(self, aba):
        self.aba_ativa = aba
        self.btn_aba_vendas.configure(fg_color="#1f77b4" if aba == "VENDAS_DIA" else "#333333")
        self.btn_aba_vales.configure(fg_color="#1f77b4" if aba == "VALES" else "#333333")
        self.btn_aba_estornos.configure(fg_color="#1f77b4" if aba == "ESTORNOS" else "#333333")
        self._carregar_tabela_ativa()

    def _carregar_tabela_ativa(self):
        for w in self.scroll_tabelas.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass

        if self.aba_ativa == "VENDAS_DIA":
            self._renderizar_vendas_dia()
        elif self.aba_ativa == "VALES":
            self._renderizar_vales()
        elif self.aba_ativa == "ESTORNOS":
            self._renderizar_estornos()

    def _renderizar_vendas_dia(self):
        cab = ctk.CTkFrame(self.scroll_tabelas, fg_color="#1a1a1a")
        cab.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(cab, text="ID", font=("Roboto", 11, "bold"), width=70).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="DATA / HORA", font=("Roboto", 11, "bold"), width=150).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="TOTAL (R$)", font=("Roboto", 11, "bold"), width=110).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="LÍQUIDO (R$)", font=("Roboto", 11, "bold"), width=110).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="FORMA PGTO", font=("Roboto", 11, "bold"), width=120).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="ORIGEM", font=("Roboto", 11, "bold"), width=110).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="STATUS", font=("Roboto", 11, "bold"), width=100).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="AÇÃO", font=("Roboto", 11, "bold"), width=240).pack(side="left", padx=4)

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                linhas = cursor.execute(
                    """
                    SELECT id, data_venda, valor_total, valor_liquido, forma_pagamento, origem, status_pedido
                    FROM vendas
                    WHERE date(data_venda) = date('now', 'localtime') AND status_pedido != 'ESTORNADO'
                    ORDER BY id DESC
                    """
                ).fetchall()

                if not linhas:
                    ini = self.data_ini.get().strip()
                    fim = self.data_fim.get().strip()
                    linhas = cursor.execute(
                        """
                        SELECT id, data_venda, valor_total, valor_liquido, forma_pagamento, origem, status_pedido
                        FROM vendas
                        WHERE date(data_venda) BETWEEN date(?) AND date(?) AND status_pedido != 'ESTORNADO'
                        ORDER BY id DESC
                        """,
                        (ini, fim),
                    ).fetchall()
        except Exception as e:
            ctk.CTkLabel(self.scroll_tabelas, text=f"Erro ao consultar vendas: {e}", text_color="#ff6666").pack(pady=10)
            return

        if not linhas:
            ctk.CTkLabel(
                self.scroll_tabelas,
                text="Nenhuma venda realizada encontrada para o dia / período selecionado.",
                font=("Roboto", 12, "italic"),
                text_color="#f39c12",
            ).pack(pady=24)
            return

        for idx, (v_id, dt, tot, liq, f_pgto, orig, st) in enumerate(linhas):
            cor_linha = "#222222" if idx % 2 == 0 else "#282828"
            linha_f = ctk.CTkFrame(self.scroll_tabelas, fg_color=cor_linha)
            linha_f.pack(fill="x", pady=2)

            ctk.CTkLabel(linha_f, text=f"#{v_id}", width=70, font=("Roboto", 11, "bold")).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(dt or "")[:19], width=150).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"R$ {float(tot or 0.0):.2f}", width=110, font=("Roboto", 11, "bold"), text_color="#2ecc71").pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"R$ {float(liq or 0.0):.2f}", width=110).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(f_pgto or "DINHEIRO"), width=120).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(orig or "BALCÃO"), width=110).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(st or "APROVADO"), width=100, text_color="#2ecc71").pack(side="left", padx=4)

            btn_est = ctk.CTkButton(
                linha_f,
                text="Estornar",
                width=110,
                height=26,
                fg_color="#8e2323",
                hover_color="#a63a3a",
                font=("Roboto", 10, "bold"),
                command=lambda vid=v_id: self.estornar_venda_dialog(venda_id_inicial=vid),
            )
            btn_est.pack(side="left", padx=6)

            # REIMPRIMIR (reimpressão do comprovante): reutiliza EXATAMENTE a
            # rotina de impressão do PDV (ModuloPDV.imprimir_cupom) para o
            # comprovante APENAS desta venda. Não cria venda, não toca em
            # estoque, caixa, pagamento, status ou histórico.
            btn_reimp = ctk.CTkButton(
                linha_f,
                text="REIMPRIMIR",
                width=110,
                height=26,
                fg_color="#6d4c41",
                hover_color="#7d5a4f",
                font=("Roboto", 10, "bold"),
                command=lambda vid=v_id: self.reimprimir_comprovante_venda(vid),
            )
            btn_reimp.pack(side="left", padx=6)

    def _renderizar_vales(self):
        cab = ctk.CTkFrame(self.scroll_tabelas, fg_color="#1a1a1a")
        cab.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(cab, text="VALE #", font=("Roboto", 11, "bold"), width=80).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="CLIENTE", font=("Roboto", 11, "bold"), width=190, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="DATA CRIAÇÃO", font=("Roboto", 11, "bold"), width=140).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="TOTAL (R$)", font=("Roboto", 11, "bold"), width=110).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="STATUS", font=("Roboto", 11, "bold"), width=110).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="QUITAÇÃO", font=("Roboto", 11, "bold"), width=130).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="AÇÕES", font=("Roboto", 11, "bold"), width=360).pack(side="left", padx=4)

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                vales = cursor.execute(
                    """
                    SELECT v.id, v.numero, COALESCE(c.nome, 'Cliente não informado'), v.data_criacao, v.total, v.status, v.data_quitacao
                    FROM vales v
                    LEFT JOIN clientes c ON c.id = v.cliente_id
                    ORDER BY v.numero DESC
                    """
                ).fetchall()
        except Exception as e:
            ctk.CTkLabel(self.scroll_tabelas, text=f"Erro ao consultar vales: {e}", text_color="#ff6666").pack(pady=10)
            return

        if not vales:
            ctk.CTkLabel(
                self.scroll_tabelas,
                text="Nenhum Vale emitido encontrado.",
                font=("Roboto", 12, "italic"),
                text_color="#f39c12",
            ).pack(pady=24)
            return

        for idx, (v_id, num, cli, dt, tot, st, dt_q) in enumerate(vales):
            cor_linha = "#222222" if idx % 2 == 0 else "#282828"
            linha_f = ctk.CTkFrame(self.scroll_tabelas, fg_color=cor_linha)
            linha_f.pack(fill="x", pady=2)

            ctk.CTkLabel(linha_f, text=f"#{num}", width=80, font=("Roboto", 11, "bold")).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(cli or ""), width=190, anchor="w").pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(dt or "")[:16], width=140).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"R$ {float(tot or 0.0):.2f}", width=110, font=("Roboto", 11, "bold"), text_color="#f39c12").pack(side="left", padx=4)

            status_vale = str(st or "PENDENTE").upper()
            cor_st = "#2ecc71" if status_vale == "QUITADO" else "#e74c3c" if status_vale == "CANCELADO" else "#f39c12"
            ctk.CTkLabel(linha_f, text=str(st or "PENDENTE"), width=110, font=("Roboto", 10, "bold"), text_color=cor_st).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(dt_q or "-")[:16], width=130).pack(side="left", padx=4)

            btn_det = ctk.CTkButton(
                linha_f,
                text="Ver Itens",
                width=100,
                height=26,
                fg_color="#455a64",
                hover_color="#546e7a",
                font=("Roboto", 10, "bold"),
                command=lambda vid=v_id, n=num, c=cli: self._abrir_modal_detalhe_vale(vid, n, c),
            )
            btn_det.pack(side="left", padx=6)

            # Comando REIMPRIMIR: reimpressão do Vale selecionado reutilizando
            # a MESMA rotina/formatação de impressão de Vale já existente
            # (ModuloPDV.imprimir_cupom_vale). Apenas leitura: status
            # (PENDENTE/QUITADO/CANCELADO), estoque, caixa e qualquer dado
            # operacional permanecem intactos.
            btn_reimp = ctk.CTkButton(
                linha_f,
                text="REIMPRIMIR",
                width=100,
                height=26,
                fg_color="#6d4c41",
                hover_color="#7d5a4f",
                font=("Roboto", 10, "bold"),
                command=lambda vid=v_id, n=num: self.reimprimir_vale(vid, n),
            )
            btn_reimp.pack(side="left", padx=6)

            # Comando contextual EXCLUIR VALE: mesmo mecanismo e mesmo padrao
            # visual do comando contextual das VENDAS (aba "Vendas do Dia").
            # Somente Vale PENDENTE pode ser cancelado; QUITADO/CANCELADO ficam
            # protegidos (botao desabilitado) e permanecem no historico.
            btn_exc = ctk.CTkButton(
                linha_f,
                text="EXCLUIR VALE",
                width=120,
                height=26,
                fg_color="#8e2323",
                hover_color="#a63a3a",
                font=("Roboto", 10, "bold"),
                state="normal" if status_vale == "PENDENTE" else "disabled",
                command=lambda vid=v_id, n=num, c=cli, s=status_vale: self.cancelar_vale(vid, n, c, s),
            )
            btn_exc.pack(side="left", padx=6)

    def _abrir_modal_detalhe_vale(self, vale_id, numero, cliente):
        modal = ctk.CTkToplevel(self)
        modal.title(f"Itens do Vale #{numero} — {cliente}")
        # Geometria ajustada (cirurgico): janela maior + minsize para que o
        # rodape (FECHAR) permaneca sempre visivel, mesmo com muitos itens.
        modal.geometry("820x520")
        modal.minsize(700, 430)
        modal.transient(self)
        modal.grab_set()

        ctk.CTkLabel(modal, text=f"VALE #{numero} — {cliente}", font=("Roboto", 14, "bold")).pack(pady=(16, 6))

        scroll = ctk.CTkScrollableFrame(modal, fg_color="#181818")
        scroll.pack(fill="both", expand=True, padx=16, pady=8)

        cab = ctk.CTkFrame(scroll, fg_color="#222222")
        cab.pack(fill="x", pady=2)
        ctk.CTkLabel(cab, text="PRODUTO", font=("Roboto", 10, "bold"), width=250, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="QTD", font=("Roboto", 10, "bold"), width=60).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="UN", font=("Roboto", 10, "bold"), width=40).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="PREÇO", font=("Roboto", 10, "bold"), width=80).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="SUBTOTAL", font=("Roboto", 10, "bold"), width=90).pack(side="left", padx=4)

        total_vale = 0.0
        with get_db_connection() as conn:
            itens = conn.execute(
                """
                SELECT descricao_produto, quantidade, unidade, preco_unitario, subtotal
                FROM vale_itens WHERE vale_id = ? ORDER BY id
                """,
                (int(vale_id),),
            ).fetchall()

        for it in itens:
            linha_f = ctk.CTkFrame(scroll, fg_color="transparent")
            linha_f.pack(fill="x", pady=1)
            ctk.CTkLabel(linha_f, text=str(it[0]), width=250, anchor="w").pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"{float(it[1] or 0.0):g}", width=60).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(it[2] or "UN"), width=40).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"R$ {float(it[3] or 0.0):.2f}", width=80).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"R$ {float(it[4] or 0.0):.2f}", width=90).pack(side="left", padx=4)
            total_vale += float(it[4] or 0.0)

        ctk.CTkLabel(modal, text=f"TOTAL DO VALE: R$ {total_vale:.2f}", font=("Roboto", 14, "bold"), text_color="#2ecc71").pack(pady=(4, 12))

        # REIMPRIMIR VALE (reimpressão): mesma rotina/formatação de impressão
        # de Vale já existente — não altera status, estoque, caixa nem dados.
        ctk.CTkButton(
            modal,
            text="REIMPRIMIR VALE",
            width=160,
            fg_color="#6d4c41",
            hover_color="#7d5a4f",
            font=("Roboto", 12, "bold"),
            command=lambda: self.reimprimir_vale(vale_id, numero),
        ).pack(side="left", padx=(16, 8), pady=(0, 14))

        ctk.CTkButton(modal, text="FECHAR", width=120, fg_color="#555555", command=modal.destroy).pack(pady=(0, 14))

    def cancelar_vale(self, vale_id, numero, cliente, status):
        """Cancelamento logico de Vale PENDENTE pela area de Relatorios.

        Reutiliza o mecanismo existente de status do Vale (PENDENTE/QUITADO):
        o registro NAO e apagado, o numero historico permanece, e nenhum
        estoque, venda, movimento financeiro ou caixa e tocado. Vale QUITADO
        nao pode ser excluido nem alterado.
        """
        status_atual = str(status or "PENDENTE").upper()
        if status_atual != "PENDENTE":
            messagebox.showwarning(
                "Excluir Vale",
                f"O Vale #{numero} está {status_atual} e não pode ser excluído.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Confirmar Exclusão do Vale",
            f"Cancelar o Vale #{numero} — {cliente}?\n\n"
            "O Vale ficará marcado como CANCELADO e não poderá mais ser\n"
            "aberto ou finalizado. Estoque, vendas, financeiro e caixa não\n"
            "são alterados e o número do Vale permanece no histórico.",
            parent=self,
        ):
            return

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE vales SET status = 'CANCELADO' WHERE id = ? AND status = 'PENDENTE'",
                    (int(vale_id),),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Somente Vale com status PENDENTE pode ser excluído.")
                registrar_log(
                    None,
                    "Relatório Vale",
                    "Sucesso",
                    f"Vale #{numero} (id {vale_id}) cancelado",
                    conn=conn,
                )
        except Exception as e:
            messagebox.showerror(
                "Excluir Vale", f"Falha ao cancelar o Vale:\n{e}", parent=self
            )
            return

        messagebox.showinfo(
            "Vale cancelado",
            f"Vale #{numero} cancelado.\nEle permanece no histórico como CANCELADO.",
            parent=self,
        )
        self._carregar_tabela_ativa()

    # ------------------------------------------------------------------
    # REIMPRESSÃO (Relatórios) — reutiliza EXATAMENTE as rotinas de
    # impressão já existentes no PDV (ModuloPDV.imprimir_cupom e
    # ModuloPDV.imprimir_cupom_vale), preservando formato, transporte
    # térmico (ESC/POS) e regras de erro já validadas. Somente leitura:
    # nenhuma venda, item, status, estoque, caixa, pagamento, financeiro
    # ou histórico é criado/alterado/apagado por estas ações.
    # ------------------------------------------------------------------
    def _obter_impressor_documento(self):
        """Retorna o impressor reutilizando a rotina real do PDV.

        Mesmo padrão já adotado pelo projeto (checklist_operacional.py):
        `ModuloPDV.__new__` instancia a classe SEM abrir a janela do PDV e
        apenas fornece a configuração usada pela composição do cupom.
        Assim a reimpressão funciona mesmo com o PDV fechado, sem criar
        tela, venda ou movimento de caixa.
        """
        from modulo_pdv import ModuloPDV

        impressor = ModuloPDV.__new__(ModuloPDV)
        impressor.config = carregar_configuracoes() or {}
        return impressor

    def _dados_venda_para_impressao(self, venda_id):
        """Monta (somente leitura) o payload do comprovante de uma venda."""
        with get_db_connection() as conn:
            venda = conn.execute(
                "SELECT id, valor_total, forma_pagamento FROM vendas WHERE id = ?",
                (int(venda_id),),
            ).fetchone()
            if not venda:
                raise ValueError(f"Venda #{venda_id} não encontrada.")
            itens = conn.execute(
                """
                SELECT COALESCE(p.nome, 'ITEM'), p.unidade, iv.quantidade, iv.subtotal
                FROM itens_venda iv
                LEFT JOIN produtos p ON p.id = iv.produto_id
                WHERE iv.venda_id = ?
                ORDER BY iv.id
                """,
                (int(venda_id),),
            ).fetchall()

        if not itens:
            raise ValueError(f"Venda #{venda_id} sem itens para reimpressão.")

        itens_cupom = []
        for nome, unidade, qtd, subtotal in itens:
            qtd_f = float(qtd or 0.0)
            subtotal_f = float(subtotal or 0.0)
            unitario = subtotal_f / qtd_f if qtd_f else 0.0
            itens_cupom.append(
                {
                    "nome": str(nome or "ITEM"),
                    "quantidade": qtd_f,
                    "unidade": str(unidade or "UN"),
                    "preco_unitario": unitario,
                    "subtotal": subtotal_f,
                    "total": subtotal_f,
                }
            )

        return {
            "id": int(venda[0]),
            "itens": itens_cupom,
            "total": float(venda[1] or 0.0),
            "forma_pagamento": str(venda[2] or "N/A"),
        }

    def reimprimir_comprovante_venda(self, venda_id):
        """REIMPRIME o comprovante da venda selecionada (reimpressão apenas).

        Reutiliza `ModuloPDV.imprimir_cupom` (mesma composição e mesmo
        transporte ESC/POS da venda original). NÃO cria venda, NÃO toca em
        estoque, caixa, pagamento, status (inclusive ESTORNADO) ou
        histórico — apenas envia o comprovante à impressora padrão.
        """
        try:
            dados = self._dados_venda_para_impressao(venda_id)
            impressor = self._obter_impressor_documento()
            impressor.imprimir_cupom(dados)
            registrar_log(
                None,
                "Relatório Reimpressão",
                "Sucesso",
                f"Comprovante da venda #{int(venda_id)} reimpresso (sem alteração operacional)",
            )
        except Exception as e:
            messagebox.showerror(
                "Reimprimir Comprovante",
                f"Falha ao reimprimir o comprovante da venda #{venda_id}:\n{e}",
                parent=self,
            )
            return

        messagebox.showinfo(
            "Reimprimir Comprovante",
            f"Comprovante da venda #{int(venda_id)} enviado à impressora.\n"
            "Reimpressão apenas: nenhuma venda, estoque, caixa ou pagamento foi alterado.",
            parent=self,
        )

    def reimprimir_vale(self, vale_id, numero=None):
        """REIMPRIME o Vale selecionado usando a rotina/formatação existente.

        Reutiliza `ModuloPDV.imprimir_cupom_vale`. NÃO altera status
        (PENDENTE/QUITADO/CANCELADO), estoque, caixa, financeiro ou
        histórico — inclusive em Vale CANCELADO (que NÃO é reativado).
        """
        rotulo = f"#{numero}" if numero is not None else f"(id {vale_id})"
        try:
            impressor = self._obter_impressor_documento()
            impressor.imprimir_cupom_vale(vale_id)
            registrar_log(
                None,
                "Relatório Reimpressão",
                "Sucesso",
                f"Vale {rotulo} reimpresso (sem alteração operacional)",
            )
        except Exception as e:
            messagebox.showerror(
                "Reimprimir Vale",
                f"Falha ao reimprimir o Vale {rotulo}:\n{e}",
                parent=self,
            )
            return

        messagebox.showinfo(
            "Reimprimir Vale",
            f"Vale {rotulo} enviado à impressora.\n"
            "Reimpressão apenas: status, estoque, caixa e financeiro não foram alterados.",
            parent=self,
        )

    def _renderizar_estornos(self):
        cab = ctk.CTkFrame(self.scroll_tabelas, fg_color="#1a1a1a")
        cab.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(cab, text="VENDA ID", font=("Roboto", 11, "bold"), width=90).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="DATA VENDA", font=("Roboto", 11, "bold"), width=160).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="VALOR TOTAL (R$)", font=("Roboto", 11, "bold"), width=130).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="FORMA PGTO", font=("Roboto", 11, "bold"), width=130).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="ORIGEM", font=("Roboto", 11, "bold"), width=130).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="SITUAÇÃO", font=("Roboto", 11, "bold"), width=130).pack(side="left", padx=4)
        ctk.CTkLabel(cab, text="AÇÃO", font=("Roboto", 11, "bold"), width=240).pack(side="left", padx=4)

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                estornadas = cursor.execute(
                    """
                    SELECT id, data_venda, valor_total, forma_pagamento, origem, status_pedido
                    FROM vendas
                    WHERE status_pedido = 'ESTORNADO'
                    ORDER BY id DESC
                    """
                ).fetchall()
        except Exception as e:
            ctk.CTkLabel(self.scroll_tabelas, text=f"Erro ao consultar estornos: {e}", text_color="#ff6666").pack(pady=10)
            return

        if not estornadas:
            ctk.CTkLabel(
                self.scroll_tabelas,
                text="Nenhuma venda estornada registrada.",
                font=("Roboto", 12, "italic"),
                text_color="#2ecc71",
            ).pack(pady=24)
            return

        for idx, (v_id, dt, tot, f_pgto, orig, st) in enumerate(estornadas):
            cor_linha = "#222222" if idx % 2 == 0 else "#282828"
            linha_f = ctk.CTkFrame(self.scroll_tabelas, fg_color=cor_linha)
            linha_f.pack(fill="x", pady=2)

            ctk.CTkLabel(linha_f, text=f"#{v_id}", width=90, font=("Roboto", 11, "bold")).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(dt or "")[:19], width=160).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=f"R$ {float(tot or 0.0):.2f}", width=130, font=("Roboto", 11, "bold")).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(f_pgto or "DINHEIRO"), width=130).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text=str(orig or "BALCÃO"), width=130).pack(side="left", padx=4)
            ctk.CTkLabel(linha_f, text="ESTORNADA", width=130, font=("Roboto", 11, "bold"), text_color="#e74c3c").pack(side="left", padx=4)

            # REIMPRIMIR (reimpressão): mesma rotina de comprovante de venda
            # (ModuloPDV.imprimir_cupom) — somente leitura, sem efeitos
            # operacionais; o status ESTORNADA do registro não é alterado.
            btn_reimp = ctk.CTkButton(
                linha_f,
                text="REIMPRIMIR",
                width=110,
                height=26,
                fg_color="#6d4c41",
                hover_color="#7d5a4f",
                font=("Roboto", 10, "bold"),
                command=lambda vid=v_id: self.reimprimir_comprovante_venda(vid),
            )
            btn_reimp.pack(side="left", padx=6)

    def atualizar_dados(self):
        """Busca dados no banco e atualiza a interface."""
        ini = self.data_ini.get()
        fim = self.data_fim.get()

        try:
            import modulo_financeiro

            resumo = modulo_financeiro.obter_resumo_fluxo_caixa_periodo(ini, fim)
            self.card_vendas.configure(text=f"R$ {resumo['valor_bruto']:,.2f}")
            self.card_despesas.configure(text=f"R$ {resumo['valor_impostos']:,.2f}")
            self.card_lucro.configure(text=f"R$ {resumo['valor_liquido']:,.2f}")

            self._carregar_tabela_ativa()

        except Exception as e:
            messagebox.showerror("Erro BI", f"Erro ao processar dados: {e}")

    def estornar_venda_dialog(self, venda_id_inicial=None):
        """UI mínima de estorno de venda (4B-3): restaura lotes + agregado."""
        modal = ctk.CTkToplevel(self)
        modal.title("Estornar Venda")
        modal.geometry("440x280")
        modal.grab_set()

        ctk.CTkLabel(modal, text="ID da venda (vendas.id):").pack(pady=(18, 4))
        ent_id = ctk.CTkEntry(modal, width=140)
        if venda_id_inicial is not None:
            ent_id.insert(0, str(venda_id_inicial))
        ent_id.pack()

        ctk.CTkLabel(modal, text="Motivo (opcional):").pack(pady=(12, 4))
        ent_motivo = ctk.CTkEntry(modal, width=300)
        ent_motivo.pack()

        def _confirmar():
            bruto = ent_id.get().strip()
            try:
                venda_id = int(bruto)
            except ValueError:
                messagebox.showerror("Estorno", "Informe um ID numérico válido.", parent=modal)
                return
            if not messagebox.askyesno(
                "Confirmar Estorno",
                f"Estornar a venda {venda_id}?\n\n"
                "O estoque (lotes e agregado) será restaurado conforme os lotes\n"
                "registrados em itens_venda e a operação não poderá ser repetida.",
                parent=modal,
            ):
                return
            try:
                from modulo_estoque import estornar_venda

                restaurado = estornar_venda(venda_id, motivo=ent_motivo.get().strip())
            except Exception as exc:
                messagebox.showerror("Estorno", f"Falha ao estornar:\n{exc}", parent=modal)
                return
            modal.destroy()
            detalhe = (
                ", ".join(f"produto {pid}: {qtd:g}" for pid, qtd in sorted(restaurado.items()))
                or "nada a restaurar"
            )
            messagebox.showinfo(
                "Estorno concluído",
                f"Venda {venda_id} estornada com sucesso.\nEstoque restaurado: {detalhe}",
            )
            self.atualizar_dados()

        ctk.CTkButton(
            modal, text="Confirmar Estorno", fg_color="#8e2323", hover_color="#a63a3a",
            command=_confirmar,
        ).pack(pady=18)

    def gerar_e_subir_pdf(self):
        """Gera o PDF profissional e envia ao Drive."""
        ini, fim = self.data_ini.get(), self.data_fim.get()
        nome_arquivo = f"Relatorio_Vendas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        caminho_pdf = Path(obter_caminho_dados("relatorios", nome_arquivo)).resolve()

        try:
            doc = SimpleDocTemplate(str(caminho_pdf), pagesize=A4)
            styles = getSampleStyleSheet()
            elements = []

            # Cabeçalho
            config = carregar_configuracoes()
            elements.append(Paragraph(f"<b>{config.get('razao_social', 'MERCADO FRS')}</b>", styles['Title']))
            elements.append(Paragraph(f"Relatório de Gestão: {ini} até {fim}", styles['Normal']))
            elements.append(Spacer(1, 20))

            # Sessão Financeira
            elements.append(Paragraph("1. RESUMO FINANCEIRO", styles['Heading2']))
            import modulo_financeiro
            resumo = modulo_financeiro.obter_resumo_fluxo_caixa_periodo(ini, fim)
            data_fin = [
                ["Descrição", "Valor"],
                ["Valor Bruto", f"R$ {resumo['valor_bruto']:.2f}"],
                ["Impostos Retidos", f"R$ {resumo['valor_impostos']:.2f}"],
                ["Valor Líquido", f"R$ {resumo['valor_liquido']:.2f}"],
            ]
            t_fin = Table(data_fin, colWidths=[300, 150])
            t_fin.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.grey), ('TEXTCOLOR',(0,0),(-1,0),colors.whitesmoke)]))
            elements.append(t_fin)

            # Sessão Estoque Crítico
            elements.append(Spacer(1, 20))
            elements.append(Paragraph("2. PRODUTOS CRÍTICOS (Estoque Baixo)", styles['Heading2']))
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT nome, quantidade_atual FROM produtos WHERE quantidade_atual < 5")
                produtos = cursor.fetchall()
                data_prod = [["Produto", "Qtd Atual"]] + list(produtos)
                t_prod = Table(data_prod, colWidths=[350, 100])
                t_prod.setStyle(TableStyle([('GRID', (0,0), (-1,-1), 1, colors.black)]))
                elements.append(t_prod)

            doc.build(elements)
            
            # Upload para o Drive
            self.upload_para_drive(str(caminho_pdf), config.get("drive_backup_folder_id"))
            
            messagebox.showinfo("Sucesso", f"Relatório gerado e enviado ao Google Drive!\nArquivo: {nome_arquivo}")
            registrar_log(None, "Relatório BI", "Sucesso", f"PDF gerado e enviado ao Drive: {nome_arquivo}")

        except Exception as e:
            messagebox.showerror("Erro PDF/Drive", f"Falha na exportação: {e}")
            registrar_log(None, "Relatório BI", "Falha", str(e))

    def gerar_esboco_sped(self):
        """Gera CSV base para futura integração SPED Fiscal."""
        ini, fim = self.data_ini.get(), self.data_fim.get()
        nome_arquivo = f"esboco_sped_fiscal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        caminho_csv = Path(obter_caminho_dados("relatorios", nome_arquivo)).resolve()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        date(v.data_venda) AS data_movimento,
                        v.id AS venda_id,
                        p.codigo_barras,
                        p.ncm,
                        iv.quantidade,
                        iv.subtotal AS valor_bruto_item,
                        ROUND(
                            CASE WHEN v.valor_total > 0
                                THEN (iv.subtotal / v.valor_total) * v.valor_impostos_retidos
                                ELSE 0
                            END,
                            2
                        ) AS impostos_item,
                        ROUND(
                            iv.subtotal -
                            CASE WHEN v.valor_total > 0
                                THEN (iv.subtotal / v.valor_total) * v.valor_impostos_retidos
                                ELSE 0
                            END,
                            2
                        ) AS valor_liquido_item,
                        v.forma_pagamento
                    FROM vendas v
                    JOIN itens_venda iv ON iv.venda_id = v.id
                    JOIN produtos p ON p.id = iv.produto_id
                    WHERE date(v.data_venda) BETWEEN date(?) AND date(?)
                    ORDER BY v.data_venda, v.id, iv.id
                    """,
                    (ini, fim),
                )
                linhas = cursor.fetchall()

            caminho_csv.parent.mkdir(parents=True, exist_ok=True)
            with caminho_csv.open("w", newline="", encoding="utf-8") as arq:
                writer = csv.writer(arq, delimiter=';')
                writer.writerow([
                    "data_movimento",
                    "venda_id",
                    "codigo_barras",
                    "ncm",
                    "quantidade",
                    "valor_bruto_item",
                    "impostos_item",
                    "valor_liquido_item",
                    "forma_pagamento",
                ])
                writer.writerows(linhas)

            messagebox.showinfo("Sucesso", f"Esboço SPED gerado em:\n{caminho_csv}")
            registrar_log(None, "Relatório SPED Base", "Sucesso", f"Arquivo gerado: {nome_arquivo}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao gerar esboço SPED: {e}")
            registrar_log(None, "Relatório SPED Base", "Falha", str(e))

    @staticmethod
    def _obter_drive_service():
        """Autentica via OAuth2, reaproveitando token local para evitar novo login."""
        if not GOOGLE_CREDS["credentials"]:
            raise Exception("Credenciais do Google (credentials.json) não encontradas.")

        SCOPES = ['https://www.googleapis.com/auth/drive.file']
        creds = None
        token_path = obter_caminho_dados("token.pickle")
        try:
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Google Drive requer as bibliotecas Google instaladas. "
                "A configuração de Drive não está disponível."
            ) from exc
        
        # Gerenciamento de token para evitar login repetitivo
        if os.path.exists(token_path):
            with open(token_path, 'rb') as token:
                creds = pickle.load(token)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Dependência opcional: a importação só ocorre quando o Drive
                # realmente precisa iniciar o fluxo OAuth e não pode afetar
                # relatórios locais.
                try:
                    from google_auth_oauthlib.flow import InstalledAppFlow
                except ImportError as exc:
                    raise RuntimeError(
                        "Google Drive requer a biblioteca google-auth-oauthlib, "
                        "que não está instalada."
                    ) from exc
                flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS["credentials"], SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)

        return build('drive', 'v3', credentials=creds)

    @staticmethod
    def upload_para_drive(file_path, folder_id):
        """Realiza o upload usando as credenciais OAuth2 configuradas."""
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise RuntimeError(
                "Google Drive requer as bibliotecas Google instaladas. "
                "A configuração de Drive não está disponível."
            ) from exc
        service = ModuloRelatorio._obter_drive_service()
        
        file_metadata = {
            'name': os.path.basename(file_path),
            'parents': [folder_id] if folder_id else []
        }
        media = MediaFileUpload(file_path, mimetype='application/pdf')
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

    @staticmethod
    def provisionar_novo_cliente(email_cliente):
        """
        Provisiona estrutura inicial no Drive do cliente autenticado:
        - Solicita OAuth2 (se necessário)
        - Garante pasta raiz FRS_Solution
        - Cria/copia base inicial template e envia para a pasta
        """
        service = ModuloRelatorio._obter_drive_service()

        email_drive = ""
        try:
            about = service.about().get(fields="user(emailAddress)").execute()
            email_drive = str(about.get("user", {}).get("emailAddress", "")).strip()
        except Exception:
            email_drive = ""

        if email_drive and email_cliente and email_drive.lower() != str(email_cliente).strip().lower():
            registrar_log(
                None,
                "Provisionamento Drive",
                "Aviso",
                f"Email OAuth2 divergente. Esperado: {email_cliente}; autenticado: {email_drive}",
            )

        query_pasta = (
            "name = 'FRS_Solution' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        resposta = service.files().list(q=query_pasta, spaces='drive', fields='files(id,name)').execute()
        pastas = resposta.get("files", [])

        if pastas:
            pasta_id = pastas[0]["id"]
        else:
            metadata_pasta = {
                "name": "FRS_Solution",
                "mimeType": "application/vnd.google-apps.folder",
            }
            pasta_criada = service.files().create(body=metadata_pasta, fields='id').execute()
            pasta_id = pasta_criada["id"]

        # Garante base local existente e cria snapshot template para provisionamento.
        origem_db = Path(get_db_path())
        if not origem_db.exists():
            with get_db_connection():
                pass

        pasta_templates = Path(obter_caminho_dados("templates"))
        pasta_templates.mkdir(parents=True, exist_ok=True)
        template_db = pasta_templates / "base_inicial_template.db"
        shutil.copy2(str(origem_db), str(template_db))

        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise RuntimeError(
                "Google Drive requer a biblioteca google-api-python-client, "
                "que não está instalada."
            ) from exc
        nome_arquivo = template_db.name
        query_arquivo = (
            f"name = '{nome_arquivo}' and "
            f"'{pasta_id}' in parents and trashed = false"
        )
        arquivos = service.files().list(q=query_arquivo, spaces='drive', fields='files(id,name)').execute().get("files", [])

        media = MediaFileUpload(str(template_db), mimetype='application/octet-stream', resumable=False)
        if arquivos:
            service.files().update(fileId=arquivos[0]["id"], media_body=media).execute()
            acao_arquivo = "atualizado"
        else:
            service.files().create(
                body={"name": nome_arquivo, "parents": [pasta_id]},
                media_body=media,
                fields='id',
            ).execute()
            acao_arquivo = "criado"

        registrar_log(
            None,
            "Provisionamento Drive",
            "Sucesso",
            f"Pasta FRS_Solution pronta e template {acao_arquivo}. Email OAuth: {email_drive or 'desconhecido'}",
        )

        return {
            "email_oauth": email_drive,
            "pasta_raiz_id": pasta_id,
            "arquivo_template": nome_arquivo,
            "status": "ok",
        }

    @staticmethod
    def ensure_market_drive_root(market_id: str) -> dict:
        """Garante pasta raiz por mercado no Google Drive autenticado."""
        market_id = str(market_id or "").strip()
        if not market_id:
            raise ValueError("market_id ausente para provisionamento do Drive.")

        service = ModuloRelatorio._obter_drive_service()

        query_root = (
            "name = 'FRS_Solution' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        root_files = service.files().list(q=query_root, spaces='drive', fields='files(id,name)').execute().get("files", [])
        if root_files:
            root_id = root_files[0]["id"]
        else:
            root_id = service.files().create(
                body={"name": "FRS_Solution", "mimeType": "application/vnd.google-apps.folder"},
                fields='id',
            ).execute()["id"]

        market_query = (
            f"name = '{market_id}' and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            f"'{root_id}' in parents and trashed = false"
        )
        market_files = service.files().list(q=market_query, spaces='drive', fields='files(id,name)').execute().get("files", [])
        if market_files:
            market_folder_id = market_files[0]["id"]
        else:
            market_folder_id = service.files().create(
                body={
                    "name": market_id,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [root_id],
                },
                fields='id',
            ).execute()["id"]

        return {
            "root_folder_id": root_id,
            "market_folder_id": market_folder_id,
            "market_id": market_id,
        }

if __name__ == "__main__":
    app = ctk.CTk()
    def abrir(): ModuloRelatorio()
    ctk.CTkButton(app, text="Abrir BI", command=abrir).pack(pady=50)
    app.mainloop()