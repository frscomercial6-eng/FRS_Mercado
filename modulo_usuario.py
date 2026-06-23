import sqlite3
import customtkinter as ctk
from tkinter import messagebox
import hashlib # Para hashing de senhas
from database_manager import get_db_connection, registrar_log
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero


def formatar_percentual_inteiro(valor):
    try:
        return str(int(round(float(valor))))
    except Exception:
        return "0"

class ModuloUsuario(ctk.CTkToplevel):
    def __init__(self, master=None, is_admin_user=False):
        super().__init__(master)
        self.title("Gestão de Usuários - PDV Mercado")
        self.geometry("1200x700")
        self.grab_set() # Torna a janela modal

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar usuários.")
            self.destroy()
            return

        self.db_path = 'mercado.db'
        self.editing_user_id = None # Armazena o ID do usuário sendo editado

        # Verificação de acesso de administrador
        if not is_admin_user:
            messagebox.showerror("Acesso Negado", "Você não tem permissão para acessar a gestão de usuários.")
            registrar_log(None, "Acesso Módulo Usuário", "Falha", "Tentativa de acesso sem permissão de administrador.")
            self.destroy()
            return

        self._create_widgets()
        self._load_users()
        self._clear_form() # Limpa o formulário ao iniciar

    def _create_widgets(self):
        # Configura o grid principal da janela
        self.grid_columnconfigure(0, weight=1) # Coluna do formulário
        self.grid_columnconfigure(1, weight=2) # Coluna da tabela
        self.grid_rowconfigure(0, weight=1)

        # --- Frame Esquerdo: Formulário de Usuário ---
        self.form_frame = ctk.CTkFrame(self)
        self.form_frame.grid(row=0, column=0, padx=20, pady=20, sticky="nsew")
        self.form_frame.grid_columnconfigure(0, weight=1)
        self.form_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.form_frame, text="Cadastro/Edição de Usuário", font=("Arial", 18, "bold")).grid(row=0, column=0, columnspan=2, pady=15)

        # Nome
        ctk.CTkLabel(self.form_frame, text="Nome:").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        self.entry_nome = ctk.CTkEntry(self.form_frame, width=250)
        self.entry_nome.grid(row=1, column=1, padx=10, pady=5, sticky="ew")

        # CPF
        ctk.CTkLabel(self.form_frame, text="CPF:").grid(row=2, column=0, padx=10, pady=5, sticky="w")
        self.entry_cpf = ctk.CTkEntry(self.form_frame, width=250)
        self.entry_cpf.grid(row=2, column=1, padx=10, pady=5, sticky="ew")

        # Salário
        ctk.CTkLabel(self.form_frame, text="Salário:").grid(row=3, column=0, padx=10, pady=5, sticky="w")
        self.entry_salario = ctk.CTkEntry(self.form_frame, width=250)
        self.entry_salario.grid(row=3, column=1, padx=10, pady=5, sticky="ew")
        aplicar_padrao_entrada_numerica(self.entry_salario, inteiro=False, casas_decimais=2)

        # Nível de Acesso
        ctk.CTkLabel(self.form_frame, text="Nível de Acesso:").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        self.optionmenu_permissao = ctk.CTkOptionMenu(self.form_frame, values=["Administrador", "Operador"], width=250)
        self.optionmenu_permissao.grid(row=4, column=1, padx=10, pady=5, sticky="ew")

        # Recebe Comissão Checkbox
        self.checkbox_comissao = ctk.CTkCheckBox(self.form_frame, text="Recebe Comissão", command=self._toggle_comissao_field)
        self.checkbox_comissao.grid(row=5, column=0, columnspan=2, padx=10, pady=10, sticky="w")

        # Porcentagem Comissão (inicialmente oculto)
        self.label_porcentagem_comissao = ctk.CTkLabel(self.form_frame, text="% Comissão:")
        self.entry_porcentagem_comissao = ctk.CTkEntry(self.form_frame, width=250)
        aplicar_padrao_entrada_numerica(self.entry_porcentagem_comissao, inteiro=False, casas_decimais=2)
        # Estes serão adicionados/removidos do grid por _toggle_comissao_field

        # Senha
        ctk.CTkLabel(self.form_frame, text="Senha:").grid(row=7, column=0, padx=10, pady=5, sticky="w")
        self.entry_senha = ctk.CTkEntry(self.form_frame, width=250, show="*")
        self.entry_senha.grid(row=7, column=1, padx=10, pady=5, sticky="ew")

        ctk.CTkLabel(self.form_frame, text="Confirmar Senha:").grid(row=8, column=0, padx=10, pady=5, sticky="w")
        self.entry_confirmar_senha = ctk.CTkEntry(self.form_frame, width=250, show="*")
        self.entry_confirmar_senha.grid(row=8, column=1, padx=10, pady=5, sticky="ew")

        # Botões
        self.btn_salvar = ctk.CTkButton(self.form_frame, text="Salvar Usuário", fg_color="green", command=self._save_user)
        self.btn_salvar.grid(row=9, column=0, columnspan=2, pady=15, sticky="ew", padx=10)

        self.btn_limpar = ctk.CTkButton(self.form_frame, text="Limpar Campos", fg_color="gray", command=self._clear_form)
        self.btn_limpar.grid(row=10, column=0, columnspan=2, pady=5, sticky="ew", padx=10)

        # --- Frame Direito: Lista de Usuários ---
        self.list_frame = ctk.CTkFrame(self)
        self.list_frame.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")
        self.list_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.list_frame, text="Usuários Cadastrados", font=("Arial", 18, "bold")).pack(pady=15)

        # Cabeçalho da Tabela
        self.table_header_frame = ctk.CTkFrame(self.list_frame, fg_color="gray20")
        self.table_header_frame.pack(fill="x", padx=10, pady=(0, 5))
        headers = ["ID", "Nome", "CPF", "Salário", "Nível", "Comissão", "%Comissão", "Ações"]
        widths = [30, 150, 100, 80, 80, 60, 80, 120]
        for i, text in enumerate(headers):
            lbl = ctk.CTkLabel(self.table_header_frame, text=text, width=widths[i], font=("Arial", 10, "bold"))
            lbl.pack(side="left", padx=2)

        # Frame Rolável para a Lista de Usuários
        self.scrollable_users = ctk.CTkScrollableFrame(self.list_frame)
        self.scrollable_users.pack(fill="both", expand=True, padx=10, pady=5)

        self._toggle_comissao_field() # Define o estado inicial dos campos de comissão

    def _toggle_comissao_field(self):
        if self.checkbox_comissao.get() == 1: # Checkbox marcado
            self.label_porcentagem_comissao.grid(row=6, column=0, padx=10, pady=5, sticky="w")
            self.entry_porcentagem_comissao.grid(row=6, column=1, padx=10, pady=5, sticky="ew")
        else: # Checkbox desmarcado
            self.label_porcentagem_comissao.grid_forget()
            self.entry_porcentagem_comissao.grid_forget()

    def _hash_password(self, password):
        """Gera um hash SHA256 da senha."""
        return hashlib.sha256(password.encode()).hexdigest()

    def _load_users(self):
        """Carrega todos os usuários do banco de dados e os exibe na tabela."""
        for widget in self.scrollable_users.winfo_children(): # Limpa a lista atual
            widget.destroy()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, nome, cpf, salario, recebe_comissao, porcentagem_comissao, permissao FROM usuarios")
                users = cursor.fetchall()

            for user in users:
                user_id, nome, cpf, salario, recebe_comissao, porcentagem_comissao, permissao = user
                
                row_frame = ctk.CTkFrame(self.scrollable_users, fg_color="transparent")
                row_frame.pack(fill="x", pady=2)

                ctk.CTkLabel(row_frame, text=str(user_id), width=30).pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text=nome, width=150, anchor="w").pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text=cpf if cpf else "N/A", width=100).pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text=f"R$ {salario:.2f}", width=80).pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text=permissao, width=80).pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text="Sim" if recebe_comissao else "Não", width=60).pack(side="left", padx=2)
                ctk.CTkLabel(row_frame, text=f"{formatar_percentual_inteiro(porcentagem_comissao)}%" if recebe_comissao else "N/A", width=80).pack(side="left", padx=2)

                btn_edit = ctk.CTkButton(row_frame, text="✎", width=30, fg_color="#3B8ED0", command=lambda u=user: self._edit_user(u))
                btn_edit.pack(side="left", padx=2)
                
                btn_del = ctk.CTkButton(row_frame, text="X", width=30, fg_color="#D35B58", command=lambda uid=user_id: self._delete_user(uid))
                btn_del.pack(side="left", padx=2)

        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao carregar usuários: {e}")
            registrar_log(None, "Carregar Usuários", "Falha", f"Erro: {e}")

    def _clear_form(self):
        """Limpa todos os campos do formulário."""
        self.entry_nome.delete(0, "end")
        self.entry_cpf.delete(0, "end")
        self.entry_salario.delete(0, "end")
        self.entry_porcentagem_comissao.delete(0, "end")
        self.entry_senha.delete(0, "end")
        self.entry_confirmar_senha.delete(0, "end")
        self.optionmenu_permissao.set("Operador")
        self.checkbox_comissao.deselect()
        self._toggle_comissao_field()
        self.editing_user_id = None
        self.btn_salvar.configure(text="Salvar Usuário")

    def _edit_user(self, user_data):
        """Preenche o formulário com os dados do usuário para edição."""
        self._clear_form()
        self.editing_user_id = user_data[0]
        self.entry_nome.insert(0, user_data[1])
        self.entry_cpf.insert(0, user_data[2] if user_data[2] else "")
        self.entry_salario.insert(0, str(user_data[3]))
        self.optionmenu_permissao.set(user_data[6])
        if user_data[4]: # recebe_comissao
            self.checkbox_comissao.select()
            self.entry_porcentagem_comissao.insert(0, formatar_percentual_inteiro(user_data[5]))
        self._toggle_comissao_field()
        self.btn_salvar.configure(text="Atualizar Usuário")

    def _save_user(self):
        """Salva ou atualiza um usuário no banco de dados."""
        nome = self.entry_nome.get()
        cpf = self.entry_cpf.get()
        salario_str = self.entry_salario.get()
        permissao = self.optionmenu_permissao.get()
        recebe_comissao = self.checkbox_comissao.get() == 1
        porcentagem_comissao_str = self.entry_porcentagem_comissao.get() if recebe_comissao else "0.0"
        senha = self.entry_senha.get()
        confirmar_senha = self.entry_confirmar_senha.get()

        if not nome or not permissao:
            messagebox.showerror("Erro", "Nome e Nível de Acesso são obrigatórios.")
            return
        
        if not self.editing_user_id and (not senha or not confirmar_senha):
            messagebox.showerror("Erro", "Para novos usuários, a senha e a confirmação de senha são obrigatórias.")
            return

        if senha != confirmar_senha:
            messagebox.showerror("Erro", "As senhas não coincidem.")
            return

        try:
            salario = parse_numero(salario_str, "Salário", minimo=0)
            porcentagem_comissao = parse_numero(porcentagem_comissao_str, "% Comissão", minimo=0)
            senha_hash = self._hash_password(senha) if senha else None # Só atualiza se uma nova senha for fornecida

            with get_db_connection() as conn:
                cursor = conn.cursor()
                if self.editing_user_id:
                    # Atualizar usuário
                    query = "UPDATE usuarios SET nome = ?, cpf = ?, salario = ?, recebe_comissao = ?, porcentagem_comissao = ?, permissao = ?"
                    params = [nome, cpf, salario, recebe_comissao, porcentagem_comissao, permissao]
                    if senha_hash:
                        query += ", senha_hash = ?"
                        params.append(senha_hash)
                    query += " WHERE id = ?"
                    params.append(self.editing_user_id)
                    cursor.execute(query, tuple(params))
                    messagebox.showinfo("Sucesso", "Usuário atualizado com sucesso!")
                    registrar_log(None, "Atualização de Usuário", "Sucesso", f"Usuário ID {self.editing_user_id} atualizado.")
                else:
                    # Inserir novo usuário
                    cursor.execute("INSERT INTO usuarios (nome, cpf, senha_hash, salario, recebe_comissao, porcentagem_comissao, permissao) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (nome, cpf, senha_hash, salario, recebe_comissao, porcentagem_comissao, permissao))
                    messagebox.showinfo("Sucesso", "Usuário cadastrado com sucesso!")
                    registrar_log(None, "Cadastro de Usuário", "Sucesso", f"Novo usuário '{nome}' cadastrado.")
            
            self._clear_form()
            self._load_users()
        except ValueError:
            messagebox.showerror("Erro", "Salário e Porcentagem de Comissão devem ser números válidos.")
            registrar_log(None, "Cadastro/Atualização de Usuário", "Falha", "Erro de valor em salário/comissão.")
        except sqlite3.IntegrityError:
            messagebox.showerror("Erro", "CPF já cadastrado para outro usuário.")
            registrar_log(None, "Cadastro/Atualização de Usuário", "Falha", f"CPF '{cpf}' duplicado.")
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao salvar usuário: {e}")
            registrar_log(None, "Cadastro/Atualização de Usuário", "Falha", f"Erro inesperado: {e}")

    def _delete_user(self, user_id):
        """Deleta um usuário do banco de dados."""
        if messagebox.askyesno("Confirmação", "Tem certeza que deseja deletar este usuário?"):
            try:
                with get_db_connection() as conn:
                    conn.execute("DELETE FROM usuarios WHERE id = ?", (user_id,))
                messagebox.showinfo("Sucesso", "Usuário deletado com sucesso!")
                registrar_log(None, "Exclusão de Usuário", "Sucesso", f"Usuário ID {user_id} excluído.")
                self._load_users()
                self._clear_form()
            except Exception as e:
                messagebox.showerror("Erro", f"Erro ao deletar usuário: {e}")
                registrar_log(None, "Exclusão de Usuário", "Falha", f"Erro ao excluir usuário ID {user_id}: {e}")

if __name__ == "__main__":
    # Exemplo de teste isolado
    ctk.set_appearance_mode("System")  # Modes: "System" (default), "Dark", "Light"
    ctk.set_default_color_theme("blue")  # Themes: "blue" (default), "green", "dark-blue"

    app = ctk.CTk()
    app.geometry("400x300")
    app.title("Teste Módulo Usuário")

    def open_user_module():
        # Para teste, assumimos que o usuário é admin
        ModuloUsuario(app, is_admin_user=True) 

    btn = ctk.CTkButton(app, text="Abrir Gestão de Usuários", command=open_user_module)
    btn.pack(pady=50)

    app.mainloop()