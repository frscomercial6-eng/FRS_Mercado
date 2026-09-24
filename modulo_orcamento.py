from datetime import datetime
from tkinter import filedialog, messagebox
from xml.sax.saxutils import escape

import customtkinter as ctk
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas as pdf_canvas

import modulo_financeiro
from database_manager import get_db_connection, registrar_log
from modulo_config import carregar_configuracoes
from modulo_estoque import aplicar_baixa_fefo
from modulo_fiscal import ModuloExportacaoFiscal
from modulo_pdv import calcular_impostos_liquidos
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero


OBSERVACOES_PADRAO_ORCAMENTO = "Aceitamos PIX, débito, crédito e boleto.\nPrazo de entrega: sete dias úteis."


def solicitar_observacoes_orcamento(parent, valor_inicial=None):
    """Pequeno editor livre para a observação comercial do orçamento."""
    modal = ctk.CTkToplevel(parent)
    modal.title("OBSERVAÇÕES DO ORÇAMENTO")
    modal.geometry("620x300")
    modal.transient(parent)
    modal.grab_set()
    ctk.CTkLabel(modal, text="OBSERVAÇÕES", font=("Arial", 15, "bold")).pack(pady=(18, 8))
    editor = ctk.CTkTextbox(modal, width=560, height=150, wrap="word")
    editor.pack(fill="both", expand=True, padx=20, pady=(0, 12))
    editor.insert("1.0", str(valor_inicial or OBSERVACOES_PADRAO_ORCAMENTO))
    editor.focus_set()
    resultado = {"texto": None}

    def confirmar():
        resultado["texto"] = editor.get("1.0", "end-1c").strip()
        modal.grab_release()
        modal.destroy()

    def cancelar():
        modal.grab_release()
        modal.destroy()

    botoes = ctk.CTkFrame(modal, fg_color="transparent")
    botoes.pack(pady=(0, 18))
    ctk.CTkButton(botoes, text="CONFIRMAR", width=130, command=confirmar).pack(side="left", padx=6)
    ctk.CTkButton(botoes, text="CANCELAR", width=130, fg_color="#666666", command=cancelar).pack(side="left", padx=6)
    modal.protocol("WM_DELETE_WINDOW", cancelar)
    modal.wait_window()
    return resultado["texto"]


class _CanvasRodapeUltimaPagina(pdf_canvas.Canvas):
    """Desenha o rodapé somente depois de conhecida a última página."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paginas_salvas = []

    def showPage(self):
        self._paginas_salvas.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._paginas_salvas)
        for indice, estado in enumerate(self._paginas_salvas, start=1):
            self.__dict__.update(estado)
            if indice == total:
                self.setFont("Helvetica", 7)
                self.setFillColor(colors.HexColor("#666666"))
                self.drawCentredString(
                    A4[0] / 2.0,
                    18,
                    "Desenvolvido por FRS Solutions · www.frssolutions.com.br",
                )
            super().showPage()
        super().save()


def _data_documento_br(valor):
    texto = str(valor or "")
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return texto[:16] or datetime.now().strftime("%d/%m/%Y %H:%M")


def _moeda_documento(valor):
    return f"R$ {float(valor or 0.0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _nome_sugerido_orcamento_pdf(cliente_nome, orcamento_id):
    """Monta um nome seguro mantendo o número e a identificação do cliente."""
    nome_cliente = str(cliente_nome or "Orcamento").strip()
    caracteres_invalidos = '<>:"/\\|?*'
    nome_cliente = "".join("_" if char in caracteres_invalidos else char for char in nome_cliente)
    nome_cliente = " ".join(nome_cliente.split()).strip(" .") or "Orcamento"
    return f"{nome_cliente} - Orcamento {int(orcamento_id):06d}.pdf"


def gerar_pdf_orcamento(orcamento_id, config=None, parent=None, notificar=True):
    """Gera PDF A4 do orçamento salvo, sem vender, baixar estoque ou mover caixa."""
    with get_db_connection() as conn:
        cabecalho = conn.execute(
            """
            SELECT o.id, o.data_orcamento, o.status, o.valor_total, o.observacao,
                   c.nome, c.documento, c.telefone, c.email, c.endereco
            FROM orcamentos o
            LEFT JOIN clientes c ON c.id = o.cliente_id
            WHERE o.id = ?
            """,
            (orcamento_id,),
        ).fetchone()
        if not cabecalho:
            raise ValueError(f"Orçamento {orcamento_id} não encontrado.")
        itens = conn.execute(
            """
            SELECT descricao_produto, quantidade, unidade, valor_unitario, subtotal
            FROM orcamento_itens
            WHERE orcamento_id = ?
            ORDER BY id
            """,
            (orcamento_id,),
        ).fetchall()

    cfg = dict(config or carregar_configuracoes() or {})
    nome = str(cfg.get("nome_fantasia") or cfg.get("nome_estabelecimento") or cfg.get("razao_social") or "ESTABELECIMENTO").strip()
    cnpj = str(cfg.get("cnpj") or "").strip()
    cidade_uf = " / ".join(x for x in (str(cfg.get("cidade") or "").strip(), str(cfg.get("uf") or "").strip()) if x)
    endereco = ", ".join(x for x in (
        str(cfg.get("logradouro") or "").strip(), str(cfg.get("numero") or "").strip(),
        str(cfg.get("complemento") or "").strip(), str(cfg.get("bairro") or "").strip(),
        cidade_uf, str(cfg.get("cep") or "").strip(),
    ) if x)
    styles = getSampleStyleSheet()
    dir_style = ParagraphStyle("Dir", parent=styles["Normal"], alignment=TA_RIGHT, fontName="Helvetica-Bold", fontSize=13, leading=16)
    fonte = ParagraphStyle("Fonte", parent=styles["Normal"], fontSize=9, leading=12)
    moeda_style = ParagraphStyle("Moeda", parent=fonte, alignment=TA_RIGHT)
    moeda_total_style = ParagraphStyle("MoedaTotal", parent=moeda_style, fontName="Helvetica-Bold")
    total_label_style = ParagraphStyle("TotalLabel", parent=fonte, alignment=TA_RIGHT, fontName="Helvetica-Bold")
    elementos = [Table([[
        [Paragraph(escape(nome), styles["Heading2"]),
         Paragraph(escape(endereco or "Endereço não configurado"), fonte),
         Paragraph(escape(f"CNPJ: {cnpj}" if cnpj else "CNPJ: não configurado"), fonte)],
        Paragraph(f"<b>ORÇAMENTO Nº {escape(str(cabecalho[0]))}</b>", dir_style),
    ]], colWidths=[350, 170], style=TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])), Spacer(1, 12)]
    cliente = [f"<b>Cliente:</b> {escape(str(cabecalho[5] or 'Não informado'))}"]
    for rotulo, valor in (("Documento", cabecalho[6]), ("Telefone", cabecalho[7]), ("E-mail", cabecalho[8]), ("Endereço", cabecalho[9])):
        if valor:
            cliente.append(f"<b>{rotulo}:</b> {escape(str(valor))}")
    elementos.extend([Paragraph("<br/>".join(cliente), styles["Normal"]), Spacer(1, 6)])
    elementos.append(Paragraph(f"<b>Data:</b> {escape(_data_documento_br(cabecalho[1]))} &nbsp;&nbsp; <b>Status:</b> {'ORÇAMENTO' if str(cabecalho[2]) == 'ORCAMENTO' else 'CONVERTIDO'}", styles["Normal"]))
    observacao = str(cabecalho[4] or "").strip()
    if observacao:
        linhas_obs = [escape(linha) for linha in observacao.splitlines() or [observacao]]
        elementos.append(Paragraph("<b>Observações:</b><br/>" + "<br/>".join(linhas_obs), styles["Normal"]))
    elementos.append(Spacer(1, 14))
    tabela = [["Produto", "Qtd.", "Un.", "Preço unitário", "Subtotal"]]
    for item in itens:
        qtd = f"{float(item[1] or 0.0):.3f}".rstrip("0").rstrip(".")
        tabela.append([
            Paragraph(escape(str(item[0] or "Item")), styles["Normal"]),
            Paragraph(escape(qtd), moeda_style),
            Paragraph(escape(str(item[2] or "UN").upper()), moeda_style),
            Paragraph(escape(_moeda_documento(item[3])), moeda_style),
            Paragraph(escape(_moeda_documento(item[4])), moeda_style),
        ])
    tabela.append([
        "", "", "",
        Paragraph("TOTAL", total_label_style),
        Paragraph(escape(_moeda_documento(cabecalho[3])), moeda_total_style),
    ])
    elementos.append(Table(tabela, colWidths=[245, 55, 45, 85, 90], repeatRows=1, style=TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#404040")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (1, 1), (2, -1), "RIGHT"), ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ])))
    nome_sugerido = _nome_sugerido_orcamento_pdf(cabecalho[5], cabecalho[0])
    caminho_escolhido = filedialog.asksaveasfilename(
        parent=parent,
        title="Salvar orçamento em PDF",
        initialfile=nome_sugerido,
        defaultextension=".pdf",
        filetypes=[("Documento PDF", "*.pdf"), ("Todos os arquivos", "*.*")],
    )
    if not caminho_escolhido:
        registrar_log(None, "Orçamento PDF", "Info", "Geração de PDF cancelada pelo operador; orçamento preservado.")
        return None
    caminho_pdf = str(caminho_escolhido)
    if not caminho_pdf.lower().endswith(".pdf"):
        caminho_pdf += ".pdf"

    SimpleDocTemplate(caminho_pdf, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=32, bottomMargin=36).build(
        elementos,
        canvasmaker=_CanvasRodapeUltimaPagina,
    )
    registrar_log(None, "Orçamento PDF", "Sucesso", f"PDF gerado: {caminho_pdf}")
    if notificar:
        messagebox.showinfo("Orçamento", f"PDF do orçamento salvo com sucesso:\n{caminho_pdf}", parent=parent)
    return caminho_pdf


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

        ctk.CTkLabel(
            frame,
            text="Para converter, abra o orçamento pelo PDV e use o pagamento normal.",
            font=("Arial", 10),
        ).pack(side="left", padx=(20, 6))
        ctk.CTkButton(frame, text="Atualizar", fg_color="#455a64", command=self.carregar_orcamentos).pack(side="right", padx=8)

    def _salvar_observacao_orcamento(self, orcamento_id, texto):
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE orcamentos SET observacao = ? WHERE id = ?",
                (str(texto or "").strip(), int(orcamento_id)),
            )

    def _editar_observacoes(self, orcamento_id):
        try:
            with get_db_connection() as conn:
                atual = conn.execute(
                    "SELECT observacao FROM orcamentos WHERE id = ?", (int(orcamento_id),)
                ).fetchone()
            if atual is None:
                messagebox.showwarning("Observações", "Orçamento não encontrado.", parent=self)
                return
            texto = solicitar_observacoes_orcamento(self, atual[0] or OBSERVACOES_PADRAO_ORCAMENTO)
            if texto is None:
                return
            self._salvar_observacao_orcamento(orcamento_id, texto)
            messagebox.showinfo("Observações", "Observação do orçamento atualizada.", parent=self)
        except Exception as e:
            registrar_log(None, "Orçamento Observações", "Falha", f"Erro: {e}")
            messagebox.showerror("Observações", f"Falha ao atualizar as observações: {e}", parent=self)

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
            ("Ações", 450),
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
                    LEFT JOIN clientes c ON c.id = o.cliente_id
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
        ctk.CTkLabel(row, text=str(orc[2] or "Sem cliente cadastrado"), width=220, anchor="w").pack(side="left", padx=4)
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

        acoes = ctk.CTkFrame(row, fg_color="transparent", width=450)
        acoes.pack(side="left", padx=4)
        ctk.CTkButton(acoes, text="Ver Itens", width=90, fg_color="#455a64", command=lambda oid=orc[0]: self.exibir_itens_orcamento(oid)).pack(side="left", padx=3)
        ctk.CTkButton(acoes, text="2ª VIA / PDF", width=120, fg_color="#6d4c41", command=lambda oid=orc[0]: self.exportar_orcamento_pdf(oid)).pack(side="left", padx=3)
        ctk.CTkButton(acoes, text="Observações", width=105, fg_color="#455a64", command=lambda oid=orc[0]: self._editar_observacoes(oid)).pack(side="left", padx=3)

        if status == "ORCAMENTO":
            ctk.CTkButton(acoes, text="ABRIR NO PDV", width=120, fg_color="#2e7d32", command=lambda oid=orc[0]: self._abrir_orcamento_no_pdv(oid)).pack(side="left", padx=3)

    def _obter_pdv_para_impressao(self):
        """Localiza a instância existente do PDV para reutilizar seu impressor."""
        master = self.master
        candidatos = [master]
        janela_pdv = getattr(master, "_janela_pdv", None) if master is not None else None
        candidatos.append(janela_pdv)
        modulos = getattr(master, "_modulos_abertos", None) if master is not None else None
        if isinstance(modulos, dict):
            candidatos.append(modulos.get("PDV"))
        for candidato in candidatos:
            metodo = getattr(candidato, "imprimir_cupom_orcamento", None)
            if callable(metodo):
                return candidato
        return None

    def _abrir_orcamento_no_pdv(self, orcamento_id):
        """Abre o orçamento no PDV existente; pagamento permanece exclusivamente no PDV."""
        pdv = self._obter_pdv_para_impressao()
        if pdv is None:
            messagebox.showerror("Abrir orçamento", "O PDV não está disponível.", parent=self)
            return
        try:
            # A gestão é modal; libera o foco antes de devolver o controle ao PDV.
            try:
                self.grab_release()
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                pass
            try:
                pdv.deiconify()
                pdv.lift()
                pdv.focus_force()
            except Exception:
                pass
            # Não há conversão paralela aqui: o PDV carrega os itens e o operador
            # usa o mesmo fluxo normal de pagamento/finalização.
            pdv.abrir_orcamento_por_numero(orcamento_id)
        except Exception as e:
            messagebox.showerror("Abrir orçamento", f"Falha ao abrir no PDV: {e}")

    def imprimir_segunda_via(self, orcamento_id):
        """Reimprime o orçamento salvo em PDF, sem criar outro documento."""
        try:
            gerar_pdf_orcamento(orcamento_id, parent=self, notificar=True)
        except Exception as e:
            registrar_log(None, "Orçamento Segunda Via", "Falha", f"Erro: {e}")
            messagebox.showerror("Segunda Via", f"Falha ao gerar PDF do orçamento: {e}", parent=self)


    def exibir_itens_orcamento(self, orcamento_id):
        self.txt_itens.delete("1.0", "end")
        try:
            with get_db_connection() as conn:
                itens = conn.execute(
                    """
                    SELECT descricao_produto, ncm, quantidade, unidade, valor_unitario, subtotal
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
                f"{item[0]} | NCM: {item[1] or '-'} | Qtd: {item[2]} {item[3] or 'UN'} | Unit: R$ {float(item[4] or 0):.2f} | Total: R$ {float(item[5] or 0):.2f}\n",
            )

    def _ler_valor_pago(self):
        txt = self.ent_valor_pago.get().strip()
        if not txt:
            return 0.0
        return parse_numero(txt, "Valor pago", permitir_vazio=True, default=0.0, minimo=0)

    def converter_em_venda(self, orcamento_id):
        """Compatibilidade: conversão deve ocorrer pelo fluxo normal do PDV."""
        return self._abrir_orcamento_no_pdv(orcamento_id)

    def _converter_em_venda_legado_desativado(self, orcamento_id):
        # Método legado mantido apenas para compatibilidade de chamadas internas.
        # A conversão real édelegada ao PDV; não há pagamento paralelo.
        return self._abrir_orcamento_no_pdv(orcamento_id)
        forma_pgto = self.combo_forma_pgto.get().strip() or "DINHEIRO"

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cab = cursor.execute(
                    """
                    SELECT o.id, o.status, o.cliente_id, c.nome
                    FROM orcamentos o
                    LEFT JOIN clientes c ON c.id = o.cliente_id
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
                    SELECT produto_id, codigo_barras, descricao_produto, ncm, quantidade, unidade, valor_unitario, subtotal
                    FROM orcamento_itens
                    WHERE orcamento_id = ?
                    ORDER BY id
                    """,
                    (orcamento_id,),
                ).fetchall()

                if not itens:
                    messagebox.showwarning("Conversão", "Orçamento sem itens não pode ser convertido.")
                    return

                valor_bruto = round(sum(float(i[7] or 0.0) for i in itens), 2)
                valor_impostos = 0.0
                itens_fiscais = []

                for item in itens:
                    resultado = calcular_impostos_liquidos(item[7], item[3])
                    valor_impostos += resultado["valor_imposto"]
                    itens_fiscais.append(
                        {
                            "id": item[0],
                            "barcode": item[1] or "",
                            "nome": item[2],
                            "ncm": item[3] or "",
                            "quantidade": float(item[4] or 0.0),
                            "unidade": str(item[5] or "UN").upper(),
                            "preco": float(item[6] or 0.0),
                            "total": float(item[7] or 0.0),
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

                caixa_row = cursor.execute(
                    """
                    SELECT id, data_abertura
                    FROM caixa_operacao
                    WHERE status = 'ABERTO'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if not caixa_row:
                    messagebox.showwarning(
                        "Conversão",
                        "Não há caixa aberto para o dia atual. Abra o caixa antes de converter o orçamento.",
                    )
                    return

                data_abertura_caixa = str(caixa_row[1] or "")[:10]
                if data_abertura_caixa != datetime.now().strftime("%Y-%m-%d"):
                    messagebox.showwarning(
                        "Conversão",
                        "O caixa aberto não pertence ao dia atual. Feche-o antes de converter o orçamento.",
                    )
                    return
                caixa_operacao_id = int(caixa_row[0])

                for tabela in ("vendas", "vendas_dia", "financeiro"):
                    colunas = {
                        row[1] for row in cursor.execute(f"PRAGMA table_info({tabela})").fetchall()
                    }
                    if "caixa_operacao_id" not in colunas:
                        raise RuntimeError(
                            f"A tabela {tabela} não possui caixa_operacao_id; conversão cancelada."
                        )

                cursor.execute(
                    """
                    INSERT INTO vendas (
                        valor_total, valor_impostos_retidos, valor_liquido,
                        forma_pagamento, caixa_operacao_id
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (valor_bruto, valor_impostos, valor_liquido, forma_pgto, caixa_operacao_id),
                )
                venda_id = cursor.lastrowid

                cursor.execute(
                    """
                    INSERT INTO vendas_dia (
                        valor_total, valor_impostos_retidos, valor_liquido,
                        forma_pagamento, caixa_operacao_id
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (valor_bruto, valor_impostos, valor_liquido, forma_pgto, caixa_operacao_id),
                )

                descricao_fin = f"Venda convertida de orçamento #{orcamento_id} ({forma_pgto})"
                cursor.execute(
                    """
                    INSERT INTO financeiro (
                        valor, tipo, valor_bruto, valor_impostos_retidos,
                        taxa_aplicada, descricao, caixa_operacao_id
                    )
                    VALUES (?, 'Entrada', ?, ?, ?, ?, ?)
                    """,
                    (
                        valor_liquido,
                        valor_bruto,
                        valor_impostos,
                        taxa_aplicada,
                        descricao_fin,
                        caixa_operacao_id,
                    ),
                )

                for item in itens_fiscais:
                    produto_id = item.get("id")
                    quantidade = round(float(item.get("quantidade", 0) or 0), 3)
                    if not produto_id or quantidade <= 0:
                        continue

                    # Baixa FEFO: 1 linha por lote consumido, rateando o
                    # subtotal proporcionalmente (última parcela fecha o total
                    # do item). Sem lotes: caminho legado preservado.
                    lotes_dist = aplicar_baixa_fefo(cursor, produto_id, quantidade)
                    if lotes_dist:
                        total_item = float(item.get("total", 0.0))
                        restante_subtotal = total_item
                        for idx, (lote_id, qtd_parcela) in enumerate(lotes_dist):
                            if idx == len(lotes_dist) - 1:
                                sub_parcela = round(restante_subtotal, 2)
                            else:
                                sub_parcela = round(total_item * (qtd_parcela / quantidade), 2)
                                restante_subtotal -= sub_parcela
                            cursor.execute(
                                """
                                INSERT INTO itens_venda (venda_id, produto_id, quantidade, subtotal, lote_id, quantidade_lote)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (venda_id, int(produto_id), qtd_parcela, sub_parcela, lote_id, float(qtd_parcela)),
                            )
                    else:
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
        """Exporta uma segunda via em PDF, sem criar venda ou novo orçamento."""
        try:
            return gerar_pdf_orcamento(orcamento_id, parent=self, notificar=True)
        except Exception as e:
            registrar_log(None, "Orçamento PDF", "Falha", f"Erro: {e}")
            messagebox.showerror("Erro", f"Falha ao gerar PDF: {e}", parent=self)
            return None
