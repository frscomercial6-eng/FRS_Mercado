import customtkinter as ctk
from tkinter import messagebox

from database_manager import get_db_connection, registrar_log
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero


class ModuloFornecedor(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Cadastro de Fornecedores")
        self.geometry("1120x700")
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar fornecedores.")
            self.destroy()
            return

        self.fornecedor_editando_id = None
        self.mapa_produtos = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(4, weight=1)

        self._montar_formulario()
        self._montar_lista_fornecedores()
        self._montar_vinculo_produtos()

        self.carregar_fornecedores()
        self.carregar_produtos()
        self.carregar_vinculos()

    def _montar_formulario(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")

        ctk.CTkLabel(frame, text="Cadastro de Fornecedores", font=("Arial", 18, "bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(10, 14), sticky="w"
        )

        self.ent_nome = ctk.CTkEntry(frame, width=300, placeholder_text="Nome / Razão Social")
        self.ent_nome.grid(row=1, column=0, padx=8, pady=6, sticky="w")

        self.ent_documento = ctk.CTkEntry(frame, width=180, placeholder_text="CNPJ/CPF")
        self.ent_documento.grid(row=1, column=1, padx=8, pady=6, sticky="w")

        self.ent_telefone = ctk.CTkEntry(frame, width=170, placeholder_text="Telefone")
        self.ent_telefone.grid(row=1, column=2, padx=8, pady=6, sticky="w")

        self.ent_email = ctk.CTkEntry(frame, width=230, placeholder_text="E-mail")
        self.ent_email.grid(row=1, column=3, padx=8, pady=6, sticky="w")

        self.ent_endereco = ctk.CTkEntry(frame, width=740, placeholder_text="Endereço")
        self.ent_endereco.grid(row=2, column=0, columnspan=3, padx=8, pady=6, sticky="w")

        botoes = ctk.CTkFrame(frame, fg_color="transparent")
        botoes.grid(row=2, column=3, padx=8, pady=6, sticky="e")

        ctk.CTkButton(botoes, text="Salvar", fg_color="#2e7d32", command=self.salvar_fornecedor).pack(side="left", padx=4)
        ctk.CTkButton(botoes, text="Limpar", fg_color="#555", command=self.limpar_formulario).pack(side="left", padx=4)

    def _montar_lista_fornecedores(self):
        header = ctk.CTkFrame(self, fg_color="#2a2a2a")
        header.grid(row=1, column=0, padx=16, pady=(8, 0), sticky="ew")
        colunas = [
            ("ID", 45),
            ("Nome", 270),
            ("Documento", 140),
            ("Telefone", 140),
            ("E-mail", 220),
            ("Ações", 140),
        ]
        for texto, largura in colunas:
            ctk.CTkLabel(header, text=texto, width=largura, font=("Arial", 11, "bold")).pack(side="left", padx=4, pady=6)

        self.scroll_fornecedores = ctk.CTkScrollableFrame(self)
        self.scroll_fornecedores.grid(row=2, column=0, padx=16, pady=(0, 12), sticky="nsew")

    def _montar_vinculo_produtos(self):
        bloco = ctk.CTkFrame(self)
        bloco.grid(row=3, column=0, padx=16, pady=(0, 6), sticky="ew")

        ctk.CTkLabel(bloco, text="Vincular Itens Comprados ao Fornecedor", font=("Arial", 14, "bold")).pack(
            anchor="w", padx=10, pady=(10, 8)
        )

        linha = ctk.CTkFrame(bloco, fg_color="transparent")
        linha.pack(fill="x", padx=8, pady=(0, 10))

        self.combo_produto = ctk.CTkOptionMenu(linha, values=["Sem produtos cadastrados"], width=360)
        self.combo_produto.pack(side="left", padx=6)

        self.ent_codigo_fornecedor = ctk.CTkEntry(linha, width=170, placeholder_text="Código no fornecedor")
        self.ent_codigo_fornecedor.pack(side="left", padx=6)

        self.ent_custo_padrao = ctk.CTkEntry(linha, width=150, placeholder_text="Custo padrão")
        self.ent_custo_padrao.pack(side="left", padx=6)
        aplicar_padrao_entrada_numerica(self.ent_custo_padrao, inteiro=False, casas_decimais=2)

        ctk.CTkButton(linha, text="Vincular", fg_color="#1565C0", command=self.vincular_produto).pack(side="left", padx=6)

        self.scroll_vinculos = ctk.CTkScrollableFrame(self)
        self.scroll_vinculos.grid(row=4, column=0, padx=16, pady=(0, 16), sticky="nsew")

    def carregar_fornecedores(self):
        for w in self.scroll_fornecedores.winfo_children():
            w.destroy()

        try:
            with get_db_connection() as conn:
                fornecedores = conn.execute(
                    """
                    SELECT id, nome, cnpj_cpf, telefone, email, endereco
                    FROM fornecedores
                    ORDER BY nome COLLATE NOCASE ASC
                    """
                ).fetchall()
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao carregar fornecedores: {e}")
            return

        for fornecedor in fornecedores:
            self._adicionar_linha_fornecedor(fornecedor)

    def _adicionar_linha_fornecedor(self, fornecedor):
        row = ctk.CTkFrame(self.scroll_fornecedores, fg_color="transparent")
        row.pack(fill="x", pady=2)

        ctk.CTkLabel(row, text=str(fornecedor[0]), width=45).pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(fornecedor[1] or ""), width=270, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(fornecedor[2] or ""), width=140, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(fornecedor[3] or ""), width=140, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(fornecedor[4] or ""), width=220, anchor="w").pack(side="left", padx=4)

        acoes = ctk.CTkFrame(row, fg_color="transparent", width=140)
        acoes.pack(side="left", padx=4)
        ctk.CTkButton(acoes, text="Editar", width=65, fg_color="#3B8ED0", command=lambda f=fornecedor: self.editar_fornecedor(f)).pack(side="left", padx=2)
        ctk.CTkButton(acoes, text="Excluir", width=65, fg_color="#B53A3A", command=lambda fid=fornecedor[0]: self.excluir_fornecedor(fid)).pack(side="left", padx=2)

    def carregar_produtos(self):
        try:
            with get_db_connection() as conn:
                produtos = conn.execute("SELECT id, nome FROM produtos ORDER BY nome COLLATE NOCASE ASC").fetchall()
        except Exception:
            produtos = []

        self.mapa_produtos = {}
        labels = []
        for pid, nome in produtos:
            label = f"{pid} - {nome}"
            self.mapa_produtos[label] = pid
            labels.append(label)

        if not labels:
            labels = ["Sem produtos cadastrados"]

        self.combo_produto.configure(values=labels)
        self.combo_produto.set(labels[0])

    def carregar_vinculos(self):
        for w in self.scroll_vinculos.winfo_children():
            w.destroy()

        try:
            with get_db_connection() as conn:
                vinculos = conn.execute(
                    """
                    SELECT fp.id, f.nome, p.nome, fp.codigo_fornecedor, fp.custo_compra_padrao
                    FROM fornecedor_produtos fp
                    JOIN fornecedores f ON f.id = fp.fornecedor_id
                    JOIN produtos p ON p.id = fp.produto_id
                    ORDER BY f.nome, p.nome
                    """
                ).fetchall()
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao carregar vínculos: {e}")
            return

        header = ctk.CTkFrame(self.scroll_vinculos, fg_color="#2a2a2a")
        header.pack(fill="x", pady=(0, 4))
        for texto, largura in [("Fornecedor", 280), ("Produto", 300), ("Cód. Fornecedor", 180), ("Custo", 120), ("Ações", 100)]:
            ctk.CTkLabel(header, text=texto, width=largura, font=("Arial", 11, "bold")).pack(side="left", padx=4, pady=6)

        for vinculo in vinculos:
            row = ctk.CTkFrame(self.scroll_vinculos, fg_color="transparent")
            row.pack(fill="x", pady=2)

            ctk.CTkLabel(row, text=str(vinculo[1]), width=280, anchor="w").pack(side="left", padx=4)
            ctk.CTkLabel(row, text=str(vinculo[2]), width=300, anchor="w").pack(side="left", padx=4)
            ctk.CTkLabel(row, text=str(vinculo[3] or ""), width=180, anchor="w").pack(side="left", padx=4)
            ctk.CTkLabel(row, text=f"R$ {float(vinculo[4] or 0):.2f}", width=120).pack(side="left", padx=4)
            ctk.CTkButton(
                row,
                text="Remover",
                width=90,
                fg_color="#B53A3A",
                command=lambda vid=vinculo[0]: self.remover_vinculo(vid),
            ).pack(side="left", padx=4)

    def editar_fornecedor(self, fornecedor):
        self.fornecedor_editando_id = fornecedor[0]
        self.ent_nome.delete(0, "end")
        self.ent_nome.insert(0, str(fornecedor[1] or ""))
        self.ent_documento.delete(0, "end")
        self.ent_documento.insert(0, str(fornecedor[2] or ""))
        self.ent_telefone.delete(0, "end")
        self.ent_telefone.insert(0, str(fornecedor[3] or ""))
        self.ent_email.delete(0, "end")
        self.ent_email.insert(0, str(fornecedor[4] or ""))

        # Endereço não está visível na grade, por isso recarrega no banco para edição completa.
        try:
            with get_db_connection() as conn:
                linha = conn.execute("SELECT endereco FROM fornecedores WHERE id = ?", (self.fornecedor_editando_id,)).fetchone()
                endereco = linha[0] if linha else ""
        except Exception:
            endereco = ""

        self.ent_endereco.delete(0, "end")
        self.ent_endereco.insert(0, str(endereco or ""))

    def limpar_formulario(self):
        self.fornecedor_editando_id = None
        for ent in [self.ent_nome, self.ent_documento, self.ent_telefone, self.ent_email, self.ent_endereco]:
            ent.delete(0, "end")

    def salvar_fornecedor(self):
        nome = self.ent_nome.get().strip()
        documento = self.ent_documento.get().strip()
        telefone = self.ent_telefone.get().strip()
        email = self.ent_email.get().strip()
        endereco = self.ent_endereco.get().strip()

        if not nome:
            messagebox.showwarning("Validação", "Informe o nome do fornecedor.")
            return

        try:
            with get_db_connection() as conn:
                if self.fornecedor_editando_id:
                    conn.execute(
                        """
                        UPDATE fornecedores
                        SET nome = ?, cnpj_cpf = ?, telefone = ?, email = ?, endereco = ?
                        WHERE id = ?
                        """,
                        (nome, documento, telefone, email, endereco, self.fornecedor_editando_id),
                    )
                    registrar_log(None, "Cadastro Fornecedor", "Sucesso", f"Fornecedor {self.fornecedor_editando_id} atualizado")
                else:
                    conn.execute(
                        """
                        INSERT INTO fornecedores (nome, cnpj_cpf, telefone, email, endereco)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (nome, documento, telefone, email, endereco),
                    )
                    registrar_log(None, "Cadastro Fornecedor", "Sucesso", f"Fornecedor criado: {nome}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao salvar fornecedor: {e}")
            return

        self.limpar_formulario()
        self.carregar_fornecedores()

    def excluir_fornecedor(self, fornecedor_id):
        if not messagebox.askyesno("Confirmação", "Deseja excluir este fornecedor?"):
            return

        try:
            with get_db_connection() as conn:
                conn.execute("DELETE FROM fornecedores WHERE id = ?", (fornecedor_id,))
                registrar_log(None, "Cadastro Fornecedor", "Sucesso", f"Fornecedor excluído: {fornecedor_id}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao excluir fornecedor: {e}")
            return

        self.carregar_fornecedores()
        self.carregar_vinculos()
        if self.fornecedor_editando_id == fornecedor_id:
            self.limpar_formulario()

    def vincular_produto(self):
        if not self.fornecedor_editando_id:
            messagebox.showwarning("Vínculo", "Selecione e edite um fornecedor antes de vincular produtos.")
            return

        produto_label = self.combo_produto.get()
        produto_id = self.mapa_produtos.get(produto_label)
        if not produto_id:
            messagebox.showwarning("Vínculo", "Selecione um produto válido para vincular.")
            return

        codigo_fornecedor = self.ent_codigo_fornecedor.get().strip()
        custo_txt = self.ent_custo_padrao.get().strip()
        try:
            custo = parse_numero(custo_txt, "Custo padrão", permitir_vazio=True, default=0.0, minimo=0)
        except ValueError as e:
            messagebox.showwarning("Vínculo", str(e))
            return

        try:
            with get_db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO fornecedor_produtos (fornecedor_id, produto_id, codigo_fornecedor, custo_compra_padrao)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(fornecedor_id, produto_id)
                    DO UPDATE SET codigo_fornecedor = excluded.codigo_fornecedor,
                                  custo_compra_padrao = excluded.custo_compra_padrao,
                                  data_vinculo = CURRENT_TIMESTAMP
                    """,
                    (self.fornecedor_editando_id, produto_id, codigo_fornecedor, custo),
                )
                conn.execute(
                    """
                    UPDATE entradas
                    SET fornecedor_id = ?
                    WHERE produto_id = ? AND (fornecedor_id IS NULL OR fornecedor_id = 0)
                    """,
                    (self.fornecedor_editando_id, produto_id),
                )
                registrar_log(
                    None,
                    "Vinculo Fornecedor-Produto",
                    "Sucesso",
                    f"Fornecedor {self.fornecedor_editando_id} vinculado ao produto {produto_id}",
                )
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao vincular produto: {e}")
            return

        self.ent_codigo_fornecedor.delete(0, "end")
        self.ent_custo_padrao.delete(0, "end")
        self.carregar_vinculos()

    def remover_vinculo(self, vinculo_id):
        if not messagebox.askyesno("Confirmação", "Deseja remover este vínculo fornecedor-produto?"):
            return

        try:
            with get_db_connection() as conn:
                conn.execute("DELETE FROM fornecedor_produtos WHERE id = ?", (vinculo_id,))
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao remover vínculo: {e}")
            return

        self.carregar_vinculos()
