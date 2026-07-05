from datetime import datetime
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

import modulo_financeiro
from database_manager import get_db_connection, registrar_log, obter_caminho_dados
from modulo_fiscal import ModuloExportacaoFiscal
from modulo_pdv import calcular_impostos_liquidos
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero


class ModuloOrcamento(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Orçamentos e Propostas Comerciais")
        self.geometry("1200x760")
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar orçamentos.")
            self.destroy()
            return

        self.fiscal = ModuloExportacaoFiscal()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._montar_controles()
        self._montar_lista_orcamentos()
        self._montar_detalhes_itens()
        self.carregar_orcamentos()

    def _montar_controles(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=0, column=0, padx=14, pady=(14, 8), sticky="ew")

        ctk.CTkLabel(frame, text="Gestão de Orçamentos", font=("Arial", 18, "bold")).pack(side="left", padx=10, pady=10)

        ctk.CTkLabel(frame, text="Forma pgto conversão:").pack(side="left", padx=(20, 6))
        self.combo_forma_pgto = ctk.CTkOptionMenu(frame, values=["DINHEIRO", "PIX", "DEBITO", "CREDITO"], width=120)
        self.combo_forma_pgto.set("DINHEIRO")
        self.combo_forma_pgto.pack(side="left", padx=4)

        ctk.CTkLabel(frame, text="Valor pago:").pack(side="left", padx=(14, 6))
        self.ent_valor_pago = ctk.CTkEntry(frame, width=120, placeholder_text="0,00")
        self.ent_valor_pago.pack(side="left", padx=4)
        aplicar_padrao_entrada_numerica(self.ent_valor_pago, inteiro=False, casas_decimais=2)

        ctk.CTkButton(frame, text="Atualizar", fg_color="#455a64", command=self.carregar_orcamentos).pack(side="right", padx=8)

    def _montar_lista_orcamentos(self):
        header = ctk.CTkFrame(self, fg_color="#2a2a2a")
        header.grid(row=1, column=0, padx=14, pady=(0, 0), sticky="ew")

        colunas = [
            ("ID", 50),
            ("Data", 130),
            ("Cliente", 220),
            ("Status", 120),
            ("Bruto", 110),
            ("Impostos", 110),
            ("Líquido", 110),
            ("Ações", 330),
        ]
        for texto, largura in colunas:
            ctk.CTkLabel(header, text=texto, width=largura, font=("Arial", 11, "bold")).pack(side="left", padx=4, pady=8)

        self.scroll_orcamentos = ctk.CTkScrollableFrame(self)
        self.scroll_orcamentos.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="nsew")

    def _montar_detalhes_itens(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=3, column=0, padx=14, pady=(0, 14), sticky="nsew")
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="Itens do Orçamento", font=("Arial", 13, "bold")).grid(row=0, column=0, padx=10, pady=(8, 4), sticky="w")

        self.txt_itens = ctk.CTkTextbox(frame, height=180)
        self.txt_itens.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def carregar_orcamentos(self):
        for widget in self.scroll_orcamentos.winfo_children():
            widget.destroy()

        try:
            with get_db_connection() as conn:
                orcamentos = conn.execute(
                    """
                    SELECT
                        o.id,
                        o.data_orcamento,
                        c.nome,
                        o.status,
                        o.valor_total,
                        o.valor_impostos_retidos,
                        o.valor_liquido
                    FROM orcamentos o
                    JOIN clientes c ON c.id = o.cliente_id
                    ORDER BY o.id DESC
                    """
                ).fetchall()
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao carregar orçamentos: {e}")
            return

        for orc in orcamentos:
            self._adicionar_linha_orcamento(orc)

    def _adicionar_linha_orcamento(self, orc):
        row = ctk.CTkFrame(self.scroll_orcamentos, fg_color="transparent")
        row.pack(fill="x", pady=2)

        status = str(orc[3] or "ORCAMENTO")
        status_label = "Orçamento" if status == "ORCAMENTO" else "Concluído/Venda"

        ctk.CTkLabel(row, text=str(orc[0]), width=50).pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(orc[1])[:16], width=130, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(orc[2] or ""), width=220, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(
            row,
            text=status_label,
            width=120,
            fg_color="#1565c0" if status == "ORCAMENTO" else "#2e7d32",
            corner_radius=10,
            font=("Arial", 10, "bold"),
        ).pack(side="left", padx=4)
        ctk.CTkLabel(row, text=f"R$ {float(orc[4] or 0):.2f}", width=110).pack(side="left", padx=4)
        ctk.CTkLabel(row, text=f"R$ {float(orc[5] or 0):.2f}", width=110, text_color="#ffb3b3").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=f"R$ {float(orc[6] or 0):.2f}", width=110, text_color="#a6f4c5").pack(side="left", padx=4)

        acoes = ctk.CTkFrame(row, fg_color="transparent", width=330)
        acoes.pack(side="left", padx=4)
        ctk.CTkButton(acoes, text="Ver Itens", width=90, fg_color="#455a64", command=lambda oid=orc[0]: self.exibir_itens_orcamento(oid)).pack(side="left", padx=3)
        ctk.CTkButton(acoes, text="Exportar PDF", width=110, fg_color="#6d4c41", command=lambda oid=orc[0]: self.exportar_orcamento_pdf(oid)).pack(side="left", padx=3)

        if status == "ORCAMENTO":
            ctk.CTkButton(acoes, text="Converter em Venda", width=120, fg_color="#2e7d32", command=lambda oid=orc[0]: self.converter_em_venda(oid)).pack(side="left", padx=3)

    def exibir_itens_orcamento(self, orcamento_id):
        self.txt_itens.delete("1.0", "end")
        try:
            with get_db_connection() as conn:
                itens = conn.execute(
                    """
                    SELECT descricao_produto, ncm, quantidade, valor_unitario, subtotal
                    FROM orcamento_itens
                    WHERE orcamento_id = ?
                    ORDER BY id
                    """,
                    (orcamento_id,),
                ).fetchall()
        except Exception as e:
            self.txt_itens.insert("end", f"Erro ao carregar itens: {e}")
            return

        if not itens:
            self.txt_itens.insert("end", "Sem itens para este orçamento.")
            return

        self.txt_itens.insert("end", f"Orçamento #{orcamento_id}\n")
        self.txt_itens.insert("end", "-" * 80 + "\n")
        for item in itens:
            self.txt_itens.insert(
                "end",
                f"{item[0]} | NCM: {item[1] or '-'} | Qtd: {item[2]} | Unit: R$ {float(item[3] or 0):.2f} | Total: R$ {float(item[4] or 0):.2f}\n",
            )

    def _ler_valor_pago(self):
        txt = self.ent_valor_pago.get().strip()
        if not txt:
            return 0.0
        return parse_numero(txt, "Valor pago", permitir_vazio=True, default=0.0, minimo=0)

    def converter_em_venda(self, orcamento_id):
        forma_pgto = self.combo_forma_pgto.get().strip() or "DINHEIRO"

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cab = cursor.execute(
                    """
                    SELECT o.id, o.status, o.cliente_id, c.nome
                    FROM orcamentos o
                    JOIN clientes c ON c.id = o.cliente_id
                    WHERE o.id = ?
                    """,
                    (orcamento_id,),
                ).fetchone()
                if not cab:
                    messagebox.showwarning("Conversão", "Orçamento não encontrado.")
                    return

                if str(cab[1]) != "ORCAMENTO":
                    messagebox.showinfo("Conversão", "Este orçamento já foi convertido em venda.")
                    return

                itens = cursor.execute(
                    """
                    SELECT produto_id, codigo_barras, descricao_produto, ncm, quantidade, valor_unitario, subtotal
                    FROM orcamento_itens
                    WHERE orcamento_id = ?
                    ORDER BY id
                    """,
                    (orcamento_id,),
                ).fetchall()

                if not itens:
                    messagebox.showwarning("Conversão", "Orçamento sem itens não pode ser convertido.")
                    return

                valor_bruto = round(sum(float(i[6] or 0.0) for i in itens), 2)
                valor_impostos = 0.0
                itens_fiscais = []

                for item in itens:
                    resultado = calcular_impostos_liquidos(item[6], item[3])
                    valor_impostos += resultado["valor_imposto"]
                    itens_fiscais.append(
                        {
                            "id": item[0],
                            "barcode": item[1] or "",
                            "nome": item[2],
                            "ncm": item[3] or "",
                            "quantidade": int(item[4] or 0),
                            "preco": float(item[5] or 0.0),
                            "total": float(item[6] or 0.0),
                        }
                    )

                valor_impostos = round(valor_impostos, 2)
                valor_liquido = round(valor_bruto - valor_impostos, 2)

                taxas = modulo_financeiro.obter_taxas()
                taxa_aplicada = 0.0
                if forma_pgto in ["DEBITO", "CREDITO"]:
                    taxa_aplicada = float(taxas.get(forma_pgto, 0.0) or 0.0)
                    valor_liquido = round(valor_liquido - (valor_bruto * (taxa_aplicada / 100.0)), 2)
                if valor_liquido < 0:
                    valor_liquido = 0.0

                valor_pago = self._ler_valor_pago()
                if forma_pgto == "DINHEIRO" and valor_pago > 0 and valor_pago < valor_bruto:
                    messagebox.showwarning("Conversão", "Valor pago menor que o total bruto da venda.")
                    return

                cursor.execute(
                    """
                    INSERT INTO vendas (valor_total, valor_impostos_retidos, valor_liquido, forma_pagamento)
                    VALUES (?, ?, ?, ?)
                    """,
                    (valor_bruto, valor_impostos, valor_liquido, forma_pgto),
                )
                venda_id = cursor.lastrowid

                cursor.execute(
                    """
                    INSERT INTO vendas_dia (valor_total, valor_impostos_retidos, valor_liquido, forma_pagamento)
                    VALUES (?, ?, ?, ?)
                    """,
                    (valor_bruto, valor_impostos, valor_liquido, forma_pgto),
                )

                descricao_fin = f"Venda convertida de orçamento #{orcamento_id} ({forma_pgto})"
                cursor.execute(
                    """
                    INSERT INTO financeiro (valor, tipo, valor_bruto, valor_impostos_retidos, taxa_aplicada, descricao)
                    VALUES (?, 'Entrada', ?, ?, ?, ?)
                    """,
                    (valor_liquido, valor_bruto, valor_impostos, taxa_aplicada, descricao_fin),
                )

                for item in itens_fiscais:
                    produto_id = item.get("id")
                    quantidade = int(item.get("quantidade", 0) or 0)
                    if not produto_id or quantidade <= 0:
                        continue

                    cursor.execute(
                        """
                        INSERT INTO itens_venda (venda_id, produto_id, quantidade, subtotal)
                        VALUES (?, ?, ?, ?)
                        """,
                        (venda_id, int(produto_id), quantidade, float(item.get("total", 0.0))),
                    )
                    cursor.execute(
                        """
                        UPDATE produtos
                        SET quantidade_atual = CASE
                            WHEN quantidade_atual - ? < 0 THEN 0
                            ELSE quantidade_atual - ?
                        END
                        WHERE id = ?
                        """,
                        (quantidade, quantidade, int(produto_id)),
                    )

                cursor.execute(
                    """
                    UPDATE orcamentos
                    SET status = 'VENDA', forma_pagamento = ?, convertido_venda_id = ?,
                        valor_impostos_retidos = ?, valor_liquido = ?
                    WHERE id = ?
                    """,
                    (forma_pgto, venda_id, valor_impostos, valor_liquido, orcamento_id),
                )

            self.fiscal.exportar_venda(venda_id, itens_fiscais, forma_pgto, valor_bruto, dados_cliente=str(cab[3] or "Consumidor Final"))
            registrar_log(None, "Conversão Orçamento", "Sucesso", f"Orçamento {orcamento_id} convertido em venda {venda_id}")
            messagebox.showinfo(
                "Conversão concluída",
                f"Orçamento #{orcamento_id} convertido em venda #{venda_id}.\n"
                f"Bruto: R$ {valor_bruto:.2f}\nImpostos: R$ {valor_impostos:.2f}\nLíquido: R$ {valor_liquido:.2f}",
            )
            self.carregar_orcamentos()
            self.exibir_itens_orcamento(orcamento_id)
        except Exception as e:
            registrar_log(None, "Conversão Orçamento", "Falha", f"Erro: {e}")
            messagebox.showerror("Erro", f"Falha ao converter orçamento: {e}")

    def exportar_orcamento_pdf(self, orcamento_id):
        try:
            with get_db_connection() as conn:
                cab = conn.execute(
                    """
                    SELECT o.id, o.data_orcamento, c.nome, o.status, o.valor_total
                    FROM orcamentos o
                    JOIN clientes c ON c.id = o.cliente_id
                    WHERE o.id = ?
                    """,
                    (orcamento_id,),
                ).fetchone()
                if not cab:
                    messagebox.showwarning("Exportação", "Orçamento não encontrado.")
                    return

                itens = conn.execute(
                    """
                    SELECT descricao_produto, ncm, quantidade, valor_unitario, subtotal
                    FROM orcamento_itens
                    WHERE orcamento_id = ?
                    ORDER BY id
                    """,
                    (orcamento_id,),
                ).fetchall()

            nome_arquivo = f"orcamento_{orcamento_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            caminho_pdf = Path(obter_caminho_dados("orcamentos", nome_arquivo)).resolve()
            caminho_pdf.parent.mkdir(parents=True, exist_ok=True)

            doc = SimpleDocTemplate(str(caminho_pdf), pagesize=A4)
            styles = getSampleStyleSheet()
            elements = []

            elements.append(Paragraph("<b>Orçamento Comercial</b>", styles["Title"]))
            elements.append(Paragraph(f"Número: {cab[0]}", styles["Normal"]))
            elements.append(Paragraph(f"Data: {str(cab[1])[:19]}", styles["Normal"]))
            elements.append(Paragraph(f"Cliente: {cab[2]}", styles["Normal"]))
            elements.append(Paragraph(f"Status: {'Orçamento' if str(cab[3]) == 'ORCAMENTO' else 'Concluído/Venda'}", styles["Normal"]))
            elements.append(Spacer(1, 16))

            tabela = [["Item", "NCM", "Qtd", "Unit.", "Total"]]
            for item in itens:
                tabela.append(
                    [
                        str(item[0] or ""),
                        str(item[1] or "-"),
                        str(item[2] or 0),
                        f"R$ {float(item[3] or 0):.2f}",
                        f"R$ {float(item[4] or 0):.2f}",
                    ]
                )

            tb = Table(tabela, colWidths=[240, 80, 50, 80, 80])
            tb.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                        ("GRID", (0, 0), (-1, -1), 1, colors.black),
                    ]
                )
            )
            elements.append(tb)
            elements.append(Spacer(1, 12))
            elements.append(Paragraph(f"<b>Total do Orçamento: R$ {float(cab[4] or 0):.2f}</b>", styles["Heading3"]))

            doc.build(elements)
            registrar_log(None, "Orçamento PDF", "Sucesso", f"PDF gerado: {caminho_pdf}")
            messagebox.showinfo("Exportação", f"PDF do orçamento gerado com sucesso:\n{caminho_pdf}")
        except Exception as e:
            registrar_log(None, "Orçamento PDF", "Falha", f"Erro: {e}")
            messagebox.showerror("Erro", f"Falha ao gerar PDF: {e}")
