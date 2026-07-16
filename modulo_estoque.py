import sqlite3
import customtkinter as ctk
from tkinter import StringVar, messagebox
from datetime import datetime, timedelta
from pathlib import Path
from database_manager import get_db_connection, registrar_log
import os
import threading
from database_manager import obter_caminho_dados
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero


def calcular_preco_venda(preco_custo, margem_lucro):
    """Calcula preço de venda com base no custo e margem percentual."""
    custo = float(preco_custo)
    margem = float(margem_lucro)
    return round(custo * (1 + (margem / 100.0)), 2)


def formatar_percentual_inteiro(valor):
    """Formata percentuais para exibição com menor carga cognitiva (ex.: 100%)."""
    try:
        numero = float(valor)
    except Exception:
        numero = 0.0
    return f"{int(round(numero))}%"

class ModuloEstoque(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Gestão de Estoque - PDV Mercado")
        self.geometry("1180x640")
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar o estoque.")
            self.destroy()
            return

        self.current_editing_id = None
        self.temp_image_path = ""
        self.page_size = 50
        self.current_offset = 0
        self.total_produtos = 0
        self._estoque_carregado = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        
        # Pasta de imagens graváveis por usuário (evita Program Files).
        self.pasta_imagens = obter_caminho_dados("assets", "produtos")
        os.makedirs(self.pasta_imagens, exist_ok=True)

        # --- Cabeçalho e Busca ---
        self.frame_top = ctk.CTkFrame(self)
        self.frame_top.grid(row=0, column=0, padx=20, pady=10, sticky="ew")

        ctk.CTkLabel(self.frame_top, text="Código de Barras (Entrada Rápida):", font=("Arial", 12, "bold")).pack(side="left", padx=10)
        self.entry_barcode = ctk.CTkEntry(self.frame_top, width=250, placeholder_text="Escaneie o produto...")
        self.entry_barcode.pack(side="left", padx=10, pady=10)
        self.entry_barcode.bind("<Return>", self.buscar_por_barcode)

        self.btn_importar_nfe = ctk.CTkButton(
            self.frame_top,
            text="IMPORTAR NF-e",
            width=150,
            height=34,
            fg_color="#14532d",
            hover_color="#166534",
            command=self.alternar_campo_importar_nfe,
        )
        self.btn_importar_nfe.pack(side="left", padx=6)

        self.frame_importar_nfe = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        ctk.CTkLabel(
            self.frame_importar_nfe,
            text="Chave NF-e:",
            font=("Arial", 11, "bold"),
        ).pack(side="left", padx=(8, 4))
        self.entry_chave_nfe = ctk.CTkEntry(
            self.frame_importar_nfe,
            width=320,
            placeholder_text="Cole a chave de 44 dígitos",
        )
        self.entry_chave_nfe.pack(side="left", padx=4, pady=10)
        self.entry_chave_nfe.bind("<Return>", self.importar_nfe_por_chave)
        self.btn_buscar_nfe = ctk.CTkButton(
            self.frame_importar_nfe,
            text="Buscar",
            width=90,
            command=self.importar_nfe_por_chave,
        )
        self.btn_buscar_nfe.pack(side="left", padx=(4, 0))
        
        self.btn_refresh = ctk.CTkButton(self.frame_top, text="Atualizar Lista", command=self.recarregar_primeira_pagina)
        self.btn_refresh.pack(side="right", padx=10)

        # --- Painel de Cadastro Profissional ---
        self.frame_cadastro = ctk.CTkFrame(self)
        self.frame_cadastro.grid(row=1, column=0, padx=20, pady=5, sticky="ew")

        # Campos de Entrada
        campos_frame = ctk.CTkFrame(self.frame_cadastro, fg_color="transparent")
        campos_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        self.var_preco_custo = StringVar()
        self.var_margem_lucro = StringVar()
        self.var_preco_venda = StringVar()
        self._atualizando_precificacao = False
        self._margem_ajustada_manual = False
        self._cor_margem_padrao = ["#F9F9FA", "#343638"]
        self._cor_borda_margem_padrao = ["#979DA2", "#565B5E"]
        self._cor_texto_margem_padrao = ["gray10", "#DCE4EE"]

        self.ent_nome = ctk.CTkEntry(campos_frame, placeholder_text="Nome do Produto", width=300)
        self.ent_nome.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        self.ent_variacao = ctk.CTkEntry(campos_frame, placeholder_text="Variação (cor, tamanho, etc.)", width=300)
        self.ent_variacao.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(campos_frame, text="Código NCM", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=2, column=0, padx=5, pady=(0, 2), sticky="w")
        self.ent_ncm = ctk.CTkEntry(campos_frame, placeholder_text="NCM (somente números)", width=145)
        self.ent_ncm.grid(row=3, column=0, padx=5, pady=5, sticky="w")
        aplicar_padrao_entrada_numerica(self.ent_ncm, inteiro=True)

        # Labels explícitos para manter legibilidade independentemente do placeholder.
        ctk.CTkLabel(campos_frame, text="Custo", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=2, column=1, padx=5, pady=(0, 2), sticky="w")
        ctk.CTkLabel(campos_frame, text="Margem", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=4, column=0, padx=5, pady=(0, 2), sticky="w")

        self.ent_preco_custo = ctk.CTkEntry(campos_frame, placeholder_text="Preço Custo R$", width=145, textvariable=self.var_preco_custo)
        self.ent_preco_custo.grid(row=3, column=1, padx=5, pady=5, sticky="w")

        self.ent_margem_lucro = ctk.CTkEntry(campos_frame, placeholder_text="Margem Lucro %", width=145, textvariable=self.var_margem_lucro)
        self.ent_margem_lucro.grid(row=5, column=0, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(campos_frame, text="Preço Venda", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=4, column=1, padx=5, pady=(0, 2), sticky="w")
        ctk.CTkLabel(campos_frame, text="QTD", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=6, column=0, padx=5, pady=(0, 2), sticky="w")

        self.ent_preco_venda = ctk.CTkEntry(campos_frame, placeholder_text="Preço Venda R$", width=145, textvariable=self.var_preco_venda)
        self.ent_preco_venda.grid(row=5, column=1, padx=5, pady=5, sticky="w")

        self.ent_qtd = ctk.CTkEntry(campos_frame, placeholder_text="Qtd Atual", width=145)
        self.ent_qtd.grid(row=7, column=0, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(campos_frame, text="Validade", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=6, column=1, padx=5, pady=(0, 2), sticky="w")
        ctk.CTkLabel(campos_frame, text="QTD Mínima", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=8, column=0, padx=5, pady=(0, 2), sticky="w")

        self.ent_val = ctk.CTkEntry(campos_frame, placeholder_text="Validade (AAAA-MM-DD)", width=145)
        self.ent_val.grid(row=7, column=1, padx=5, pady=5, sticky="w")

        self.ent_qtd_min = ctk.CTkEntry(campos_frame, placeholder_text="Qtd Mínima", width=145)
        self.ent_qtd_min.grid(row=9, column=0, padx=5, pady=5, sticky="w")

        aplicar_padrao_entrada_numerica(self.ent_preco_custo, inteiro=False, casas_decimais=2)
        aplicar_padrao_entrada_numerica(self.ent_margem_lucro, inteiro=False, casas_decimais=2)
        aplicar_padrao_entrada_numerica(self.ent_preco_venda, inteiro=False, casas_decimais=2)
        aplicar_padrao_entrada_numerica(self.ent_qtd, inteiro=True)
        aplicar_padrao_entrada_numerica(self.ent_qtd_min, inteiro=True)

        self.var_preco_custo.trace_add("write", self._atualizar_preco_venda_automatico)
        self.var_margem_lucro.trace_add("write", self._atualizar_preco_venda_automatico)
        self.var_preco_venda.trace_add("write", self._atualizar_margem_por_preco_manual)

        # Gerenciamento de Imagem
        self.img_frame = ctk.CTkFrame(self.frame_cadastro, width=120, height=120)
        self.img_frame.pack(side="left", padx=10)
        self.img_frame.pack_propagate(False)
        
        self.lbl_preview_img = ctk.CTkLabel(self.img_frame, text="Sem Imagem", font=("Arial", 10))
        self.lbl_preview_img.pack(expand=True)

        self.btn_upload = ctk.CTkButton(self.frame_cadastro, text="Carregar Foto", width=100, command=self.selecionar_imagem_manual)
        self.btn_upload.pack(side="left", padx=5)

        # Barra de Ações do Cadastro
        self.actions_frame = ctk.CTkFrame(self.frame_cadastro, fg_color="transparent")
        self.actions_frame.pack(side="right", padx=10)

        self.btn_save = ctk.CTkButton(self.actions_frame, text="Salvar Novo", fg_color="#27ae60", command=self.salvar_produto)
        self.btn_save.pack(pady=5, fill="x")

        self.btn_edit_sel = ctk.CTkButton(self.actions_frame, text="Atualizar", fg_color="#2980b9", command=self.salvar_produto)
        self.btn_edit_sel.pack(pady=5, fill="x")

        self.lbl_badge_margem_manual = ctk.CTkLabel(
            self.actions_frame,
            text="Margem Ajustada Manualmente",
            fg_color="#c27c0e",
            text_color="white",
            corner_radius=10,
            font=("Arial", 10, "bold"),
        )

        self.btn_limpar = ctk.CTkButton(self.actions_frame, text="Limpar", fg_color="gray40", command=self.limpar_campos)
        self.btn_limpar.pack(pady=5, fill="x")

        # --- Alerta de Validade + Legenda de Código ---
        self.frame_info_topo = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_info_topo.grid(row=2, column=0, sticky="ew", padx=30)

        self.lbl_alerta = ctk.CTkLabel(self.frame_info_topo, text="", text_color="orange", font=("Arial", 11, "italic"))
        self.lbl_alerta.pack(side="left", pady=(0, 2))

        self.lbl_legenda_codigo = ctk.CTkLabel(
            self.frame_info_topo,
            text="Azul = Interno, Verde = Real",
            text_color="#bfc7d5",
            font=("Arial", 10, "bold"),
        )
        self.lbl_legenda_codigo.pack(side="right", pady=(0, 2))

        # --- Tabela de Produtos (Header) ---
        self.frame_header = ctk.CTkFrame(self, fg_color="gray20")
        self.frame_header.grid(row=3, column=0, padx=20, pady=(10, 0), sticky="ew")
        
        headers = ["ID", "Tipo Código", "Código", "Nome", "Variação", "NCM", "Custo", "Margem", "Preço", "QTD", "Validade", "Ações"]
        widths = [40, 95, 95, 140, 110, 85, 70, 65, 75, 55, 90, 110]
        for i, text in enumerate(headers):
            lbl = ctk.CTkLabel(self.frame_header, text=text, width=widths[i], font=("Arial", 11, "bold"), text_color="#F5F5F5")
            lbl.pack(side="left", padx=5)

        # --- Tabela de Produtos (Corpo Scrollable) ---
        self.scroll_estoque = ctk.CTkScrollableFrame(self)
        self.scroll_estoque.grid(row=4, column=0, padx=20, pady=(0, 20), sticky="nsew")

        self.frame_paginacao = ctk.CTkFrame(self)
        self.frame_paginacao.grid(row=5, column=0, padx=20, pady=(0, 10), sticky="ew")
        self.btn_prev = ctk.CTkButton(
            self.frame_paginacao,
            text="<< Anterior",
            width=110,
            command=self.carregar_pagina_anterior,
            state="disabled",
        )
        self.btn_prev.pack(side="left", padx=8, pady=8)
        self.lbl_paginacao = ctk.CTkLabel(self.frame_paginacao, text="Aguardando abertura da aba de estoque")
        self.lbl_paginacao.pack(side="left", padx=10)
        self.btn_next = ctk.CTkButton(
            self.frame_paginacao,
            text="Próxima >>",
            width=110,
            command=self.carregar_proxima_pagina,
            state="disabled",
        )
        self.btn_next.pack(side="right", padx=8, pady=8)

        self._configurar_navegacao_tab()
        self._safe_focus(self.entry_barcode)

    def _normalizar_chave_nfe(self, chave: str) -> str:
        return "".join(ch for ch in str(chave or "") if ch.isdigit())

    def alternar_campo_importar_nfe(self):
        if self.frame_importar_nfe.winfo_ismapped():
            self.frame_importar_nfe.pack_forget()
            self._safe_focus(self.entry_barcode)
            return
        self.frame_importar_nfe.pack(side="left", padx=6)
        self._safe_focus(self.entry_chave_nfe)

    def _listar_xml_nfe_candidatos(self):
        candidatos_dirs = [
            Path(obter_caminho_dados("fiscal_in")),
            Path(obter_caminho_dados("exportacao_fiscal")),
            Path(__file__).resolve().parent / "fiscal_in",
            Path(__file__).resolve().parent / "exportacao_fiscal",
        ]

        try:
            from modulo_config import carregar_configuracoes

            cfg = carregar_configuracoes() or {}
            pasta_in = str(cfg.get("pasta_entrada_fiscal") or "").strip()
            pasta_exp = str(cfg.get("pasta_exportacao_fiscal") or "").strip()
            if pasta_in:
                candidatos_dirs.append(Path(pasta_in))
            if pasta_exp:
                candidatos_dirs.append(Path(pasta_exp))
        except Exception:
            pass

        vistos = set()
        arquivos = []
        for pasta in candidatos_dirs:
            try:
                pasta_norm = str(pasta.resolve())
            except Exception:
                pasta_norm = str(pasta)
            if pasta_norm in vistos or not pasta.exists() or not pasta.is_dir():
                continue
            vistos.add(pasta_norm)
            for arq in pasta.glob("*.xml"):
                if arq.is_file():
                    arquivos.append(arq)

        arquivos.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return arquivos

    def _buscar_nfe_por_chave(self, chave_nfe: str):
        from modulo_fiscal import FiscalManager

        fiscal = FiscalManager()
        for caminho_xml in self._listar_xml_nfe_candidatos():
            try:
                dados = fiscal.processar_xml_entrada(caminho_xml)
            except Exception:
                continue

            chave_xml = self._normalizar_chave_nfe(dados.get("chave_nfe", ""))
            if chave_xml == chave_nfe:
                return dados

        return None

    def importar_nfe_por_chave(self, event=None):
        chave = self._normalizar_chave_nfe(self.entry_chave_nfe.get())
        if len(chave) != 44:
            messagebox.showwarning("Chave NF-e inválida", "Informe uma chave NF-e com 44 dígitos.")
            self._safe_focus(self.entry_chave_nfe)
            return "break"

        self.lbl_alerta.configure(text="🔎 Buscando NF-e pelos XMLs locais...", text_color="cyan")

        def _worker():
            dados = self._buscar_nfe_por_chave(chave)
            if not self.winfo_exists():
                return
            self.after(0, lambda: self._aplicar_importacao_nfe(chave, dados))

        threading.Thread(target=_worker, daemon=True).start()
        return "break"

    def _aplicar_importacao_nfe(self, chave_nfe: str, dados_nfe: dict | None):
        if not dados_nfe or not dados_nfe.get("itens"):
            self.lbl_alerta.configure(text="NF-e não encontrada nos XMLs locais.", text_color="orange")
            return

        itens = list(dados_nfe.get("itens") or [])
        cadastrados = 0

        with get_db_connection() as conn:
            cursor = conn.cursor()
            for item in itens:
                ean = str(item.get("ean") or "").strip()
                descricao = str(item.get("descricao") or "").strip() or "Produto sem descrição"
                ncm = str(item.get("ncm") or "").strip()
                preco = float(item.get("preco") or 0.0)

                codigo = ean if ean else self._gerar_codigo_interno_sequencial()

                cursor.execute("SELECT id FROM produtos WHERE codigo_barras = ? LIMIT 1", (codigo,))
                existente = cursor.fetchone()

                if existente:
                    cursor.execute(
                        """
                        UPDATE produtos
                        SET nome = ?,
                            variacao = COALESCE(NULLIF(variacao, ''), 'NF-e'),
                            ncm = ?,
                            preco_custo = ?,
                            preco_venda = ?,
                            margem_lucro = COALESCE(margem_lucro, 0.0)
                        WHERE id = ?
                        """,
                        (descricao, ncm, preco, preco, existente[0]),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO produtos (
                            codigo_barras, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda,
                            quantidade_atual, quantidade_minima, validade, imagem_path
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (codigo, descricao, "NF-e", ncm, preco, 0.0, preco, 0, 0, "", ""),
                    )
                    cadastrados += 1

        if itens:
            primeiro = itens[0]
            self.entry_barcode.delete(0, "end")
            self.entry_barcode.insert(0, str(primeiro.get("ean") or "").strip())
            self.ent_nome.delete(0, "end")
            self.ent_nome.insert(0, str(primeiro.get("descricao") or "").strip())
            self.ent_ncm.delete(0, "end")
            self.ent_ncm.insert(0, str(primeiro.get("ncm") or "").strip())
            preco_primeiro = float(primeiro.get("preco") or 0.0)
            self._preencher_precificacao(
                custo=f"{preco_primeiro:.2f}".replace(".", ","),
                margem="0",
                preco=f"{preco_primeiro:.2f}".replace(".", ","),
                margem_manual=False,
            )
            self.btn_save.configure(state="normal")
            self.btn_edit_sel.configure(state="disabled")

        self.recarregar_primeira_pagina()
        self._safe_focus(self.entry_barcode)

        msg = f"Importação concluída com sucesso: {cadastrados} produtos cadastrados"
        self.lbl_alerta.configure(text=msg, text_color="#66ff99")
        messagebox.showinfo("Importação NF-e", msg)
        registrar_log(None, "Importação NF-e", "Sucesso", f"Chave {chave_nfe} | novos={cadastrados} | total_itens={len(itens)}")

    def _widgets_tab_order(self):
        ordem = [
            self.entry_barcode,
            self.entry_chave_nfe if self.frame_importar_nfe.winfo_ismapped() else None,
            self.ent_nome,
            self.ent_variacao,
            self.ent_ncm,
            self.ent_preco_custo,
            self.ent_margem_lucro,
            self.ent_preco_venda,
            self.ent_qtd,
            self.ent_val,
            self.ent_qtd_min,
            self.btn_save,
            self.btn_edit_sel,
            self.btn_limpar,
        ]
        return [w for w in ordem if w is not None and w.winfo_exists()]

    def _navegar_tab(self, event, step=1):
        widgets = self._widgets_tab_order()
        if not widgets:
            return "break"

        atual = event.widget
        if atual not in widgets:
            self._safe_focus(widgets[0])
            return "break"

        idx = widgets.index(atual)
        prox = widgets[(idx + step) % len(widgets)]
        self._safe_focus(prox)
        return "break"

    def _configurar_navegacao_tab(self):
        for widget in [
            self.entry_barcode,
            self.entry_chave_nfe,
            self.ent_nome,
            self.ent_variacao,
            self.ent_ncm,
            self.ent_preco_custo,
            self.ent_margem_lucro,
            self.ent_preco_venda,
            self.ent_qtd,
            self.ent_val,
            self.ent_qtd_min,
            self.btn_save,
            self.btn_edit_sel,
            self.btn_limpar,
        ]:
            widget.bind("<Tab>", lambda e: self._navegar_tab(e, 1), add="+")
            widget.bind("<Shift-Tab>", lambda e: self._navegar_tab(e, -1), add="+")

    def carregar_produtos_ao_abrir_aba(self):
        """Dispara a consulta de produtos somente quando a aba de estoque for aberta."""
        if self._estoque_carregado:
            return
        self.recarregar_primeira_pagina()

    def recarregar_primeira_pagina(self):
        self.current_offset = 0
        self.carregar_produtos()

    def carregar_pagina_anterior(self):
        if self.current_offset <= 0:
            return
        self.current_offset = max(0, self.current_offset - self.page_size)
        self.carregar_produtos()

    def carregar_proxima_pagina(self):
        proximo_offset = self.current_offset + self.page_size
        if proximo_offset >= self.total_produtos:
            return
        self.current_offset = proximo_offset
        self.carregar_produtos()

    def _consultar_produtos_paginados(self):
        """Executa a leitura paginada do estoque com conexão curta ao banco."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM produtos")
            total_produtos = cursor.fetchone()[0]
            cursor.execute(
                """
                  SELECT id, codigo_barras, nome, variacao, preco_venda, quantidade_atual, validade,
                       preco_custo, margem_lucro, quantidade_minima, ncm
                FROM produtos
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (self.page_size, self.current_offset),
            )
            produtos = cursor.fetchall()

        return produtos, total_produtos

    def _atualizar_controles_paginacao(self):
        if self.total_produtos <= 0:
            self.lbl_paginacao.configure(text="Nenhum produto cadastrado")
            self.btn_prev.configure(state="disabled")
            self.btn_next.configure(state="disabled")
            return

        inicio = self.current_offset + 1
        fim = min(self.current_offset + self.page_size, self.total_produtos)
        self.lbl_paginacao.configure(text=f"Exibindo {inicio}-{fim} de {self.total_produtos} produtos")
        self.btn_prev.configure(state="normal" if self.current_offset > 0 else "disabled")
        self.btn_next.configure(
            state="normal" if (self.current_offset + self.page_size) < self.total_produtos else "disabled"
        )

    def _safe_focus(self, widget):
        try:
            if self.winfo_exists() and widget is not None and widget.winfo_exists():
                widget.focus_set()
        except Exception:
            pass

    def _parse_numero(self, texto, nome_campo, permitir_vazio=False, default=0.0, inteiro=False, minimo=0):
        """Converte texto para número com validação amigável para o usuário."""
        return parse_numero(
            texto,
            nome_campo,
            permitir_vazio=permitir_vazio,
            default=default,
            inteiro=inteiro,
            minimo=minimo,
        )

    def _set_preco_venda_texto(self, valor_texto):
        self.var_preco_venda.set(valor_texto)

    def _set_margem_lucro_texto(self, valor_texto):
        self.var_margem_lucro.set(valor_texto)

    def _set_badge_margem_manual(self, ativo):
        self._margem_ajustada_manual = ativo
        if ativo:
            self.lbl_badge_margem_manual.pack(pady=(4, 5), fill="x")
        else:
            self.lbl_badge_margem_manual.pack_forget()

        self.ent_margem_lucro.configure(
            fg_color=["#FFF3D6", "#5B4210"] if ativo else self._cor_margem_padrao,
            border_color=["#D28A00", "#F0B64D"] if ativo else self._cor_borda_margem_padrao,
            text_color=["#7A4B00", "#FFE6A8"] if ativo else self._cor_texto_margem_padrao,
        )

    def _preencher_precificacao(self, custo=None, margem=None, preco=None, margem_manual=False):
        self._atualizando_precificacao = True
        try:
            if custo is not None:
                self.var_preco_custo.set(custo)
            if margem is not None:
                margem_num = self._parse_numero(margem, "Margem", permitir_vazio=True, default=0.0)
                self.var_margem_lucro.set(str(int(round(margem_num))))
            if preco is not None:
                self.var_preco_venda.set(preco)
        finally:
            self._atualizando_precificacao = False
        self._set_badge_margem_manual(margem_manual)

    def _atualizar_preco_venda_automatico(self, *args):
        if self._atualizando_precificacao:
            return

        custo_texto = self.ent_preco_custo.get().strip()
        margem_texto = self.ent_margem_lucro.get().strip()

        if not custo_texto:
            self._set_preco_venda_texto("")
            self._set_badge_margem_manual(False)
            return

        try:
            preco_custo = self._parse_numero(custo_texto, "Preço de custo", permitir_vazio=False)
            margem_lucro = self._parse_numero(margem_texto, "Margem de lucro", permitir_vazio=True, default=0.0)
        except ValueError:
            return

        preco_venda = calcular_preco_venda(preco_custo, margem_lucro)
        self._atualizando_precificacao = True
        try:
            self._set_preco_venda_texto(f"{preco_venda:.2f}".replace('.', ','))
        finally:
            self._atualizando_precificacao = False
        self._set_badge_margem_manual(False)

    def _atualizar_margem_por_preco_manual(self, *args):
        if self._atualizando_precificacao:
            return

        custo_texto = self.ent_preco_custo.get().strip()
        preco_texto = self.ent_preco_venda.get().strip()
        margem_atual_texto = self.ent_margem_lucro.get().strip()

        if not custo_texto or not preco_texto:
            self._set_badge_margem_manual(False)
            return

        try:
            preco_custo = self._parse_numero(custo_texto, "Preço de custo", permitir_vazio=False)
            preco_venda = self._parse_numero(preco_texto, "Preço de venda", permitir_vazio=False)
            margem_atual = self._parse_numero(margem_atual_texto, "Margem de lucro", permitir_vazio=True, default=0.0)
        except ValueError:
            return

        if preco_custo <= 0:
            self._set_badge_margem_manual(False)
            return

        margem_calculada = round(((preco_venda / preco_custo) - 1) * 100, 2)
        margem_manual = abs(margem_calculada - margem_atual) > 0.009

        self._atualizando_precificacao = True
        try:
            self._set_margem_lucro_texto(str(int(round(margem_calculada))))
        finally:
            self._atualizando_precificacao = False

        self._set_badge_margem_manual(margem_manual)

    def _gerar_codigo_interno_sequencial(self):
        """Gera código interno numérico único para produtos sem código de barras informado."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS config_sistema (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                )
                """
            )

            cursor.execute("SELECT valor FROM config_sistema WHERE chave = 'proximo_codigo_interno'")
            seq_row = cursor.fetchone()

            cursor.execute(
                """
                SELECT MAX(CAST(codigo_barras AS INTEGER))
                FROM produtos
                WHERE codigo_barras GLOB '[0-9]*'
                  AND LENGTH(codigo_barras) <= 6
                  AND CAST(codigo_barras AS INTEGER) >= 1000
                """
            )
            max_row = cursor.fetchone()
            max_curto = int(max_row[0]) if max_row and max_row[0] is not None else 999

            if seq_row and str(seq_row[0]).strip().isdigit():
                candidato = max(1000, int(str(seq_row[0]).strip()))
            else:
                candidato = max(1000, max_curto + 1)

            while True:
                codigo = str(candidato)
                cursor.execute("SELECT 1 FROM produtos WHERE codigo_barras = ? LIMIT 1", (codigo,))
                if not cursor.fetchone():
                    break
                candidato += 1

            cursor.execute(
                """
                INSERT INTO config_sistema (chave, valor) VALUES ('proximo_codigo_interno', ?)
                ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
                """,
                (str(candidato + 1),),
            )

            return str(candidato)

    def _classificar_tipo_codigo(self, codigo_barras):
        """Classifica código para badge visual na listagem do estoque."""
        codigo = str(codigo_barras or "").strip()
        if not codigo:
            return "SEM CÓDIGO", "#6c757d"

        if codigo.isdigit():
            try:
                numero = int(codigo)
                if len(codigo) <= 6 and numero >= 1000:
                    return "INTERNO", "#1f6aa5"
            except ValueError:
                pass

        return "REAL", "#2e7d32"

    def selecionar_imagem_manual(self):
        from tkinter import filedialog
        caminho = filedialog.askopenfilename(filetypes=[("Imagens", "*.jpg *.png *.jpeg")])
        if caminho:
            self.temp_image_path = caminho
            self.lbl_preview_img.configure(text="Imagem\nSelecionada", text_color="cyan")

    def preencher_campos_cadastro(self, prod):
        """Preenche o painel de cadastro com dados de um produto existente."""
        self.current_editing_id = prod[0]
        self.entry_barcode.delete(0, 'end')
        self.entry_barcode.insert(0, prod[1])
        self.ent_nome.delete(0, 'end')
        self.ent_nome.insert(0, prod[2])
        self.ent_variacao.delete(0, 'end')
        self.ent_variacao.insert(0, str(prod[3] or ""))
        self.ent_ncm.delete(0, 'end')
        self.ent_ncm.insert(0, str(prod[10] or ""))
        custo = float(prod[7] if prod[7] is not None else 0.0)
        margem = float(prod[8] if prod[8] is not None else 0.0)
        preco = float(prod[4] if prod[4] is not None else 0.0)
        preco_regra = calcular_preco_venda(custo, margem)
        margem_manual = abs(preco - preco_regra) > 0.009
        self._preencher_precificacao(
            custo=f"{custo:.2f}".replace('.', ','),
            margem=str(int(round(margem))),
            preco=f"{preco:.2f}".replace('.', ','),
            margem_manual=margem_manual,
        )
        self.ent_qtd.delete(0, 'end')
        self.ent_qtd.insert(0, str(prod[5]))
        self.ent_val.delete(0, 'end')
        self.ent_val.insert(0, str(prod[6]) if prod[6] else "")
        self.ent_qtd_min.delete(0, 'end')
        self.ent_qtd_min.insert(0, str(prod[9] if prod[9] is not None else 0))
        
        self.btn_save.configure(state="disabled")
        self.btn_edit_sel.configure(state="normal")
        self.lbl_preview_img.configure(text="Produto\nCarregado")

    def limpar_campos(self):
        self.current_editing_id = None
        self.temp_image_path = ""
        self.entry_barcode.delete(0, 'end')
        self.ent_nome.delete(0, 'end')
        self.ent_variacao.delete(0, 'end')
        self.ent_ncm.delete(0, 'end')
        self._preencher_precificacao(custo="", margem="", preco="", margem_manual=False)
        self.ent_qtd.delete(0, 'end')
        self.ent_val.delete(0, 'end')
        self.ent_qtd_min.delete(0, 'end')
        self.lbl_preview_img.configure(text="Sem Imagem")
        self.btn_save.configure(state="normal")

    def carregar_produtos(self):
        """Carrega uma página de produtos e renderiza na interface."""
        for widget in self.scroll_estoque.winfo_children():
            widget.destroy()

        try:
            produtos, self.total_produtos = self._consultar_produtos_paginados()
            self._estoque_carregado = True

            hoje = datetime.now()
            alerta_count = 0

            for prod in produtos:
                # Lógica de Alerta de Validade (30 dias)
                cor_texto = "white"
                try:
                    data_validade = datetime.strptime(prod[6], "%Y-%m-%d")
                    if data_validade < hoje + timedelta(days=30):
                        cor_texto = "#FF5555" # Vermelho claro/alerta
                        alerta_count += 1
                except: pass

                row_frame = ctk.CTkFrame(self.scroll_estoque, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)

                tipo_codigo, cor_badge = self._classificar_tipo_codigo(prod[1])

                ctk.CTkLabel(row_frame, text=str(prod[0]), width=40).pack(side="left", padx=5)
                ctk.CTkLabel(
                    row_frame,
                    text=tipo_codigo,
                    width=95,
                    fg_color=cor_badge,
                    corner_radius=10,
                    font=("Arial", 10, "bold"),
                ).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=prod[1], width=105).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=prod[2], width=140, anchor="w", text_color=cor_texto).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=str(prod[3] or "-"), width=120, anchor="w").pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=str(prod[10] or "-"), width=85).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=f"R$ {float(prod[7] or 0):.2f}", width=75).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=formatar_percentual_inteiro(prod[8]), width=70).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=f"R$ {prod[4]:.2f}", width=75).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=str(prod[5]), width=60).pack(side="left", padx=5)
                ctk.CTkLabel(row_frame, text=str(prod[6]), width=95).pack(side="left", padx=5)

                # Botões de Ação
                btn_edit = ctk.CTkButton(row_frame, text="✎", width=30, fg_color="#3B8ED0", 
                                         command=lambda p=prod: self.preencher_campos_cadastro(p))
                btn_edit.pack(side="left", padx=2)
                
                btn_del = ctk.CTkButton(row_frame, text="X", width=30, fg_color="#D35B58", 
                                        command=lambda p=prod: self.deletar_produto(p[0]))
                btn_del.pack(side="left", padx=2)

            if alerta_count > 0:
                self.lbl_alerta.configure(text=f"⚠️ Atenção: {alerta_count} produtos com validade próxima ou vencidos!")
            else:
                self.lbl_alerta.configure(text="")

            self._atualizar_controles_paginacao()

        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao carregar estoque: {e}")

    def buscar_por_barcode(self, event=None):
        """Filtra ou destaca o produto pelo código de barras."""
        code = self.entry_barcode.get().strip()
        if not code:
            return "break"
        self.lbl_alerta.configure(text="")

        produto = None
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                          SELECT id, codigo_barras, nome, variacao, preco_venda, quantidade_atual, validade,
                           preco_custo, margem_lucro, quantidade_minima, ncm
                    FROM produtos
                    WHERE codigo_barras = ?
                    """,
                    (code,),
                )
                produto = cursor.fetchone()
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao consultar produto no banco: {e}")
            registrar_log(None, "Busca de Produto por Código de Barras", "Falha", f"Erro: {e}")
            return

        if produto:
            registrar_log(None, "Busca de Produto por Código de Barras", "Sucesso", f"Produto {code} encontrado.")
            self.preencher_campos_cadastro(produto)
        else:
            # Produto novo: abre cadastro imediatamente e segue com consulta inteligente em background.
            self.current_editing_id = None
            self.ent_nome.delete(0, "end")
            self.ent_variacao.delete(0, "end")
            self.ent_ncm.delete(0, "end")
            self._preencher_precificacao(custo="", margem="", preco="", margem_manual=False)
            self.ent_qtd.delete(0, "end")
            self.ent_qtd_min.delete(0, "end")
            self.ent_val.delete(0, "end")
            self.btn_save.configure(state="normal")
            self.btn_edit_sel.configure(state="disabled")
            self._safe_focus(self.ent_nome)

            self.lbl_alerta.configure(text=f"Produto não encontrado no banco. Cadastro aberto para o código {code}.", text_color="orange")
            threading.Thread(target=self.consultar_api_inteligente, args=(code,), daemon=True).start()

        return "break"

    def consultar_api_inteligente(self, barcode):
        """Consulta a API Open Food Facts e abre o cadastro pré-preenchido."""
        import requests

        url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == 1:
                    p = data["product"]
                    nome = p.get("product_name", "")
                    marca = p.get("brands", "Fabricante Desconhecido")
                    img_url = p.get("image_front_url")
                    
                    path_local = ""
                    if img_url:
                        path_local = self.baixar_imagem(img_url, barcode)
                    
                    if self.winfo_exists():
                        self.after(0, lambda: self._preencher_api(nome, marca, path_local))
                    return
        except Exception as e:
            print(f"Erro na API: {e}")
        
        # Se falhar ou não encontrar, abre manual
        if self.winfo_exists():
            self.after(0, lambda: self.lbl_alerta.configure(text="Produto não encontrado na API. Cadastre manualmente.", text_color="orange"))

    def _preencher_api(self, nome, marca, img_path):
        self.ent_nome.delete(0, 'end')
        self.ent_nome.insert(0, f"{nome} ({marca})")
        self._preencher_precificacao(custo="0", margem="0", preco="0,00", margem_manual=False)
        self.temp_image_path = img_path
        self.lbl_preview_img.configure(text="Imagem API", text_color="green")

    def baixar_imagem(self, url, barcode):
        """Faz o download da imagem e salva na pasta de dados do usuário."""
        import requests

        try:
            ext = url.split(".")[-1]
            nome_img = f"{barcode}.{ext}"
            caminho_completo = os.path.join(self.pasta_imagens, nome_img)
            
            img_data = requests.get(url, timeout=8).content
            with open(caminho_completo, 'wb') as handler:
                handler.write(img_data)
            return caminho_completo
        except:
            return ""

    def salvar_produto(self):
        """Salva novo produto ou atualiza o existente usando dados do painel."""
        try:
            barcode = self.entry_barcode.get().strip()
            nome = self.ent_nome.get().strip()
            variacao = self.ent_variacao.get().strip()
            barcode_gerado = False

            if not barcode:
                barcode = self._gerar_codigo_interno_sequencial()
                barcode_gerado = True
                self.entry_barcode.insert(0, barcode)

            preco_custo = self._parse_numero(self.ent_preco_custo.get(), "Preço de custo", permitir_vazio=False)
            margem_lucro = self._parse_numero(self.ent_margem_lucro.get(), "Margem de lucro", permitir_vazio=True, default=0.0)
            preco_venda_digitado = self.ent_preco_venda.get().strip()
            preco_venda = self._parse_numero(
                preco_venda_digitado,
                "Preço de venda",
                permitir_vazio=not bool(preco_venda_digitado),
                default=calcular_preco_venda(preco_custo, margem_lucro),
            )
            ncm = self.ent_ncm.get().strip()
            qtd = self._parse_numero(self.ent_qtd.get(), "Estoque", permitir_vazio=False, inteiro=True)
            qtd_min = self._parse_numero(self.ent_qtd_min.get(), "Quantidade mínima", permitir_vazio=True, default=0, inteiro=True)
            validade = self.ent_val.get().strip()

            self.ent_preco_venda.delete(0, 'end')
            self.ent_preco_venda.insert(0, f"{preco_venda:.2f}")

            with get_db_connection() as conn:
                cursor = conn.cursor()
                if self.current_editing_id:
                    cursor.execute("""
                        UPDATE produtos 
                        SET codigo_barras = ?, nome = ?, variacao = ?, ncm = ?, preco_custo = ?, margem_lucro = ?, preco_venda = ?,
                            quantidade_atual = ?, quantidade_minima = ?, validade = ? 
                        WHERE id = ?
                    """, (barcode, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda, qtd, qtd_min, validade, self.current_editing_id))
                    registrar_log(None, "Edição Produto", "Sucesso", f"ID {self.current_editing_id} atualizado.")
                else:
                    cursor.execute("""
                        INSERT INTO produtos (
                            codigo_barras, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda,
                            quantidade_atual, quantidade_minima, validade, imagem_path
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (barcode, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda, qtd, qtd_min, validade, self.temp_image_path))
                    registrar_log(None, "Novo Produto", "Sucesso", f"Barcode {barcode} cadastrado.")

            if barcode_gerado:
                messagebox.showinfo("Sucesso", f"Produto processado com sucesso! Código interno gerado: {barcode}")
            else:
                messagebox.showinfo("Sucesso", "Produto processado com sucesso!")
            self.limpar_campos()
            self.recarregar_primeira_pagina()
            self._safe_focus(self.entry_barcode)
        except ValueError as e:
            messagebox.showwarning("Campos numéricos inválidos", str(e))
            self._safe_focus(self.ent_preco_custo)
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao salvar: {e}")

    def deletar_produto(self, id_produto):
        """Remove o produto com confirmação."""
        if messagebox.askyesno("Confirmação", "Tem certeza que deseja excluir este produto?\nEsta ação não pode ser desfeita."):
            try:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM produtos WHERE id = ?", (id_produto,))
                self.recarregar_primeira_pagina()
                registrar_log(None, "Exclusão de Produto", "Sucesso", f"Produto ID {id_produto} excluído.")
            except sqlite3.IntegrityError:
                messagebox.showerror("Erro", "Não é possível deletar: produto possui histórico de vendas/entradas.")
                registrar_log(None, "Exclusão de Produto", "Falha", f"Produto ID {id_produto} não pode ser excluído devido a FK.")
            except Exception as e:
                messagebox.showerror("Erro", f"Erro ao deletar: {e}")
                registrar_log(None, "Exclusão de Produto", "Falha", f"Erro inesperado ao excluir produto ID {id_produto}: {e}")

if __name__ == "__main__":
    # Script de teste
    root = ctk.CTk()
    def abrir(): ModuloEstoque()
    ctk.CTkButton(root, text="Abrir Estoque", command=abrir).pack(pady=50, padx=50)
    root.mainloop()