import customtkinter as ctk
from tkinter import messagebox

from database_manager import get_db_connection, registrar_log


class ModuloCliente(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Cadastro de Clientes")
        self.geometry("980x620")
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar clientes.")
            self.destroy()
            return

        self.cliente_editando_id = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._montar_formulario()
        self._montar_lista()
        self.carregar_clientes()

    def _montar_formulario(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")

        ctk.CTkLabel(frame, text="Cadastro de Clientes", font=("Arial", 18, "bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(10, 14), sticky="w"
        )

        self.ent_nome = ctk.CTkEntry(frame, width=300, placeholder_text="Nome / Razão Social")
        self.ent_nome.grid(row=1, column=0, padx=8, pady=6, sticky="w")

        self.ent_documento = ctk.CTkEntry(frame, width=180, placeholder_text="CPF/CNPJ")
        self.ent_documento.grid(row=1, column=1, padx=8, pady=6, sticky="w")

        self.ent_telefone = ctk.CTkEntry(frame, width=170, placeholder_text="Telefone")
        self.ent_telefone.grid(row=1, column=2, padx=8, pady=6, sticky="w")

        self.ent_email = ctk.CTkEntry(frame, width=230, placeholder_text="E-mail")
        self.ent_email.grid(row=1, column=3, padx=8, pady=6, sticky="w")

        self.ent_endereco = ctk.CTkEntry(frame, width=740, placeholder_text="Endereço")
        self.ent_endereco.grid(row=2, column=0, columnspan=3, padx=8, pady=6, sticky="w")

        botoes = ctk.CTkFrame(frame, fg_color="transparent")
        botoes.grid(row=2, column=3, padx=8, pady=6, sticky="e")

        ctk.CTkButton(botoes, text="Salvar", fg_color="#2e7d32", command=self.salvar_cliente).pack(side="left", padx=4)
        ctk.CTkButton(botoes, text="Limpar", fg_color="#555", command=self.limpar_formulario).pack(side="left", padx=4)

    def _montar_lista(self):
        header = ctk.CTkFrame(self, fg_color="#2a2a2a")
        header.grid(row=1, column=0, padx=16, pady=(8, 0), sticky="ew")
        colunas = [
            ("ID", 45),
            ("Nome", 250),
            ("Documento", 130),
            ("Telefone", 130),
            ("E-mail", 210),
            ("Endereço", 220),
            ("Ações", 120),
        ]
        for texto, largura in colunas:
            ctk.CTkLabel(header, text=texto, width=largura, font=("Arial", 11, "bold")).pack(side="left", padx=4, pady=6)

        self.scroll_clientes = ctk.CTkScrollableFrame(self)
        self.scroll_clientes.grid(row=2, column=0, padx=16, pady=(0, 16), sticky="nsew")

    def carregar_clientes(self):
        for w in self.scroll_clientes.winfo_children():
            w.destroy()

        try:
            with get_db_connection() as conn:
                clientes = conn.execute(
                    """
                    SELECT id, nome, documento, telefone, email, endereco
                    FROM clientes
                    ORDER BY nome COLLATE NOCASE ASC
                    """
                ).fetchall()
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao carregar clientes: {e}")
            return

        for cliente in clientes:
            self._adicionar_linha(cliente)

    def _adicionar_linha(self, cliente):
        row = ctk.CTkFrame(self.scroll_clientes, fg_color="transparent")
        row.pack(fill="x", pady=2)

        ctk.CTkLabel(row, text=str(cliente[0]), width=45).pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(cliente[1] or ""), width=250, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(cliente[2] or ""), width=130, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(cliente[3] or ""), width=130, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(cliente[4] or ""), width=210, anchor="w").pack(side="left", padx=4)
        ctk.CTkLabel(row, text=str(cliente[5] or ""), width=220, anchor="w").pack(side="left", padx=4)

        acoes = ctk.CTkFrame(row, fg_color="transparent", width=120)
        acoes.pack(side="left", padx=4)
        ctk.CTkButton(acoes, text="Editar", width=55, fg_color="#3B8ED0", command=lambda c=cliente: self.editar_cliente(c)).pack(side="left", padx=2)
        ctk.CTkButton(acoes, text="Excluir", width=55, fg_color="#B53A3A", command=lambda cid=cliente[0]: self.excluir_cliente(cid)).pack(side="left", padx=2)

    def editar_cliente(self, cliente):
        self.cliente_editando_id = cliente[0]
        self.ent_nome.delete(0, "end")
        self.ent_nome.insert(0, str(cliente[1] or ""))
        self.ent_documento.delete(0, "end")
        self.ent_documento.insert(0, str(cliente[2] or ""))
        self.ent_telefone.delete(0, "end")
        self.ent_telefone.insert(0, str(cliente[3] or ""))
        self.ent_email.delete(0, "end")
        self.ent_email.insert(0, str(cliente[4] or ""))
        self.ent_endereco.delete(0, "end")
        self.ent_endereco.insert(0, str(cliente[5] or ""))

    def limpar_formulario(self):
        self.cliente_editando_id = None
        for ent in [self.ent_nome, self.ent_documento, self.ent_telefone, self.ent_email, self.ent_endereco]:
            ent.delete(0, "end")

    def salvar_cliente(self):
        nome = self.ent_nome.get().strip()
        documento = self.ent_documento.get().strip()
        telefone = self.ent_telefone.get().strip()
        email = self.ent_email.get().strip()
        endereco = self.ent_endereco.get().strip()

        if not nome:
            messagebox.showwarning("Validação", "Informe o nome do cliente.")
            return

        try:
            with get_db_connection() as conn:
                if self.cliente_editando_id:
                    conn.execute(
                        """
                        UPDATE clientes
                        SET nome = ?, documento = ?, telefone = ?, email = ?, endereco = ?
                        WHERE id = ?
                        """,
                        (nome, documento, telefone, email, endereco, self.cliente_editando_id),
                    )
                    registrar_log(None, "Cadastro Cliente", "Sucesso", f"Cliente {self.cliente_editando_id} atualizado")
                else:
                    conn.execute(
                        """
                        INSERT INTO clientes (nome, documento, telefone, email, endereco)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (nome, documento, telefone, email, endereco),
                    )
                    registrar_log(None, "Cadastro Cliente", "Sucesso", f"Cliente criado: {nome}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao salvar cliente: {e}")
            return

        self.limpar_formulario()
        self.carregar_clientes()

    def excluir_cliente(self, cliente_id):
        if not messagebox.askyesno("Confirmação", "Deseja excluir este cliente?"):
            return
        try:
            with get_db_connection() as conn:
                conn.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))
                registrar_log(None, "Cadastro Cliente", "Sucesso", f"Cliente excluído: {cliente_id}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao excluir cliente: {e}")
            return

        self.carregar_clientes()
        if self.cliente_editando_id == cliente_id:
            self.limpar_formulario()
