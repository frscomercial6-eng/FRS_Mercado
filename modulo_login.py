import customtkinter as ctk
import hashlib
import os
import webbrowser
from datetime import datetime
from tkinter import messagebox

from PIL import Image

from database_manager import get_db_connection, registrar_log
from license_manager import LicenseManager

# Identificacao visual da versao (somente leitura da versao oficial gerada
# pelo release_manager). Nao altera nenhuma logica de funcionamento.
try:
    from release_info import APP_VERSION as _FRS_APP_VERSION
except Exception:
    _FRS_APP_VERSION = "1.0.20"

COMPRAR_LICENCA_URL = "https://www.frssolutions.com.br/planos"


_USUARIO_LOGADO = None

class ModuloLogin(ctk.CTkToplevel):
    def __init__(self, parent, callback_sucesso, auto_update_repo: str | None = None):
        super().__init__(parent)
        self.parent = parent
        self.callback_sucesso = callback_sucesso
        # Parâmetro mantido por compatibilidade. O login nunca consulta o
        # updater; a verificação ocorre somente no sistema principal.
        self.auto_update_repo = ""
        ctk.set_appearance_mode("Dark")
        
        self.title("Autenticação - Mercado FRS")
        self.geometry("420x470")
        
        # Garante que fechar o login use o método de encerramento total do sistema
        self.protocol("WM_DELETE_WINDOW", 
                      getattr(self.parent, "fechar_sistema", self.parent.destroy))
        self.grab_set() # Bloqueia interação com janelas atrás
        
        # Lista para rastrear tarefas agendadas e evitar erros de "invalid command name"
        self._after_ids = []
        self.backup_google_autenticado = self._verificar_token_backup_local()
        self._system_monitor = None
        self._logo_image = None

        # Centralizar janela
        self._registrar_after(10, self._centralizar)



        # Título do Sistema
        self.lbl_titulo = ctk.CTkLabel(self, text="SISTEMA DE GESTAO", font=("Roboto", 20, "bold"))
        self.lbl_titulo.pack(pady=(20, 10))

        self.frame_login = ctk.CTkFrame(self)
        self.frame_setup = ctk.CTkFrame(self)
        self.frame_ativacao = ctk.CTkFrame(self)

        self._verificar_estado_sistema()

    def _verificar_token_backup_local(self):
        """Consulta somente o estado local; não acessa Google/Firebase."""
        from app_paths import obter_caminho_dados

        token_path = obter_caminho_dados("token.pickle")
        token_ok = os.path.exists(token_path)
        if token_ok:
            print("[BACKUP] token.pickle encontrado localmente. Backup considerado configurado.")
        else:
            print("[BACKUP] token.pickle ausente. Backup não configurado (não bloqueia vendas).")
        return token_ok

    def _registrar_after(self, ms, command):
        """Registra uma tarefa e armazena seu ID para cancelamento futuro."""
        if self.winfo_exists():
            id_after = self.after(ms, command)
            self._after_ids.append(id_after)
            return id_after

    def destroy(self):
        """Limpa callbacks pendentes antes de destruir a janela."""
        try:
            if self._system_monitor is not None:
                self._system_monitor.stop()
        except Exception:
            pass

        # Cancela todas as tarefas agendadas
        for after_id in self._after_ids:
            try:
                self.after_cancel(after_id)
            except:
                pass
        self._after_ids.clear()
        super().destroy()

    def _iniciar_system_monitor(self):
        # Mantido como ponto de compatibilidade; o monitor só é iniciado no
        # sistema principal, depois da autenticação local.
        return None

    def _on_system_status(self, status):
        return None

    def _encerrar_aplicacao_segura(self):
        """Encerra login e aplicação sem acionar operações de foco em janelas já destruídas."""
        try:
            if self.winfo_exists():
                self.grab_release()
        except Exception:
            pass

        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass

        try:
            if self.parent and self.parent.winfo_exists():
                self.parent.after(10, self.parent.destroy)
        except Exception:
            pass

    def debug_usuarios(self):
        """Função temporária para dump de usuários no terminal."""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, nome, permissao FROM usuarios")
                usuarios = cursor.fetchall()
                print("\n=== [DEBUG] DUMP DE USUÁRIOS NO BANCO ===")
                if not usuarios:
                    print("A tabela 'usuarios' está completamente VAZIA.")
                for u in usuarios:
                    print(f"ID: {u[0]} | Usuário: {u[1]} | Permissão: {u[2]}")
                print("==========================================\n")
        except Exception as e:
            print(f"[ERRO DEBUG] Falha ao ler tabela de usuários: {e}")

    def _status_licenca(self):
        """Status da licença (Trial/ativa/vencida). Nunca interrompe o login."""
        try:
            return LicenseManager().get_status()
        except Exception as exc:
            print(f"[LICENCA] Falha ao consultar status local: {exc}")
            return {
                "message": "Licença: indisponível",
                "is_expired": False,
                "is_warning": False,
                "color": "#f1c40f",
                "days_left": None,
            }

    def _verificar_estado_sistema(self):
        """Escolhe setup/login. A licença nunca impede a entrada no sistema.

        Autenticação é estritamente local: o login abre inclusive com licença
        vencida (modo restrito), permitindo exportar/recuperar dados, comprar e
        ativar a licença. A verificação de licença é feita pelo próprio sistema.
        """
        try:
            with get_db_connection() as conn:
                tem_usuarios = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0
            if not tem_usuarios:
                print("[SISTEMA] Banco sem usuários. Redirecionando para Setup Inicial.")
                self._configurar_setup_inicial()
                return

            status = self._status_licenca()
            dias_restantes = status.get("days_left")
            self._configurar_tela_login(aviso_vencimento=dias_restantes)
        except Exception as exc:
            messagebox.showerror("Erro Crítico", f"Erro ao verificar o estado local: {exc}")
            if self.winfo_exists():
                self._encerrar_aplicacao_segura()

    def _centralizar(self):
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f'{width}x{height}+{x}+{y}')

    def _configurar_setup_inicial(self):
        self.lbl_titulo.configure(text="SETUP DE PRIMEIRO ACESSO")
        self.frame_setup.pack(padx=30, pady=10, fill="both", expand=True)

        ctk.CTkLabel(self.frame_setup, text="Cadastre o Administrador Master:", font=("Arial", 12, "italic")).pack(pady=10)
        self.setup_user = ctk.CTkEntry(self.frame_setup, width=300, placeholder_text="Login do Admin")
        self.setup_user.pack(pady=5)
        self.setup_pass = ctk.CTkEntry(self.frame_setup, width=300, show="*", placeholder_text="Senha")
        self.setup_pass.pack(pady=5)
        self.setup_pass_confirm = ctk.CTkEntry(self.frame_setup, width=300, show="*", placeholder_text="Confirmar senha")
        self.setup_pass_confirm.pack(pady=5)
        
        def realizar_setup():
            u = self.setup_user.get().strip()
            p = self.setup_pass.get()
            p2 = self.setup_pass_confirm.get()

            if not u:
                return messagebox.showwarning("Erro", "Informe o login do administrador.")
            if len(p) < 4:
                return messagebox.showwarning("Erro", "Senha muito curta")
            if p != p2:
                return messagebox.showwarning("Erro", "Senha e confirmação não conferem.")
            
            senha_hash = hashlib.sha256(p.encode()).hexdigest()

            try:
                with get_db_connection() as conn:
                    conn.execute(
                        "INSERT INTO usuarios (nome, senha_hash, permissao) VALUES (?, ?, 'Administrador')",
                        (u, senha_hash),
                    )
            except Exception:
                messagebox.showerror("Erro", "Não foi possível concluir o setup inicial. Tente novamente.")
                return

            # Trial de 30 dias criado no setup inicial (fluxo histórico).
            try:
                data_trial = LicenseManager().iniciar_trial()
                print(f"[LICENCA] Trial de 30 dias criado ate {data_trial.isoformat()}.")
            except Exception as exc:
                print(f"[LICENCA] Falha ao criar o Trial de 30 dias: {exc}")

            messagebox.showinfo("Sucesso", "Sistema inicializado com 30 dias de licença trial.")
            self.frame_setup.pack_forget()
            self._verificar_estado_sistema()

        ctk.CTkButton(self.frame_setup, text="FINALIZAR SETUP", command=realizar_setup).pack(pady=20)

    def _configurar_tela_ativacao(self, msg=None):
        """Ativação pelo fluxo histórico: campo de digitação da chave.

        Nenhuma emissão, geração de chave, challenge, pasta privada ou tooling
        do FRS aparece para o cliente.
        """
        self.geometry("520x560")
        self.frame_login.pack_forget()
        self.frame_setup.pack_forget()
        self.lbl_titulo.configure(text="ATIVAÇÃO DE LICENÇA", text_color="#F1C40F")
        for widget in self.frame_ativacao.winfo_children():
            widget.destroy()
        self.frame_ativacao.pack(padx=30, pady=10, fill="both", expand=True)

        if msg:
            ctk.CTkLabel(
                self.frame_ativacao, text=msg, text_color="#FFCC00",
                font=("Arial", 11, "bold"), wraplength=440,
            ).pack(pady=(10, 4))

        ctk.CTkLabel(
            self.frame_ativacao,
            text="Insira a Chave de Ativação:",
            font=("Roboto", 12, "bold"),
        ).pack(anchor="w", padx=34, pady=(12, 0))

        self.ent_codigo = ctk.CTkEntry(
            self.frame_ativacao, width=420, placeholder_text="FRS-AAAAMMDD-XXXXXXXXXXXX",
        )
        self.ent_codigo.pack(padx=34, pady=(6, 10))
        self.ent_codigo.bind("<Return>", lambda _evento: self._validar_ativacao_chave())

        self.lbl_feedback_ativacao = ctk.CTkLabel(
            self.frame_ativacao, text="", text_color="#f1c40f",
            font=("Roboto", 11, "bold"), wraplength=440,
        )
        self.lbl_feedback_ativacao.pack(anchor="w", padx=34)

        ctk.CTkLabel(
            self.frame_ativacao,
            text="A chave é enviada pelo FRS após a contratação do plano.",
            wraplength=440,
        ).pack(pady=(12, 4))

        ctk.CTkButton(
            self.frame_ativacao,
            text="ATIVAR SISTEMA", fg_color="#15803d", hover_color="#116b32",
            command=self._validar_ativacao_chave,
        ).pack(fill="x", padx=34, pady=8)
        ctk.CTkButton(
            self.frame_ativacao,
            text="COMPRAR LICENÇA", fg_color="#1d4ed8", hover_color="#1740ad",
            command=self._abrir_comprar_licenca,
        ).pack(fill="x", padx=34, pady=8)
        ctk.CTkButton(
            self.frame_ativacao, text="VOLTAR AO LOGIN", fg_color="#555555",
            command=lambda: self._configurar_tela_login(aviso_vencimento=None),
        ).pack(fill="x", padx=34, pady=8)

    def _abrir_comprar_licenca(self):
        try:
            webbrowser.open(COMPRAR_LICENCA_URL, new=2)
        except Exception as exc:
            messagebox.showerror("Licença", f"Não foi possível abrir a página de planos: {exc}", parent=self)

    def _validar_ativacao_chave(self):
        """Valida a chave digitada pelo fluxo histórico e persiste a licença."""
        chave = self.ent_codigo.get().strip() if hasattr(self, "ent_codigo") else ""
        try:
            ok, mensagem, _status = LicenseManager().ativar(chave)
        except Exception as exc:
            ok, mensagem = False, f"Não foi possível ativar a licença: {exc}"

        if not ok:
            registrar_log(None, "Ativação de Licença", "Falha", str(mensagem))
            try:
                self.lbl_feedback_ativacao.configure(text=str(mensagem), text_color="#ff6666")
            except Exception:
                messagebox.showerror("Erro", str(mensagem), parent=self)
            return

        registrar_log(None, "Ativação de Licença", "Sucesso", str(mensagem))
        try:
            self.lbl_feedback_ativacao.configure(text=str(mensagem), text_color="#66ff99")
        except Exception:
            pass
        messagebox.showinfo("Ativado", str(mensagem), parent=self)
        self.frame_ativacao.pack_forget()
        self._verificar_estado_sistema()

    def _configurar_tela_login(self, aviso_vencimento=None):
        """Tela de login local.

        ``aviso_vencimento`` recebe os dias restantes da licença apenas para
        contexto; a entrada nunca é bloqueada por licença (modo restrito).
        """
        self.frame_setup.pack_forget()
        self.frame_ativacao.pack_forget()
        self.frame_login.pack(padx=30, pady=10, fill="both", expand=True)
        self._update_notice_label = None

        for widget in self.frame_login.winfo_children():
            widget.destroy()

        def abrir_ativacao():
            self.frame_login.pack_forget()
            self._configurar_tela_ativacao()

        logo_path = os.path.join(os.getcwd(), "assets", "logo.ico")
        if os.path.exists(logo_path):
            try:
                logo_img = Image.open(logo_path)
                self._logo_image = ctk.CTkImage(light_image=logo_img, dark_image=logo_img, size=(72, 72))
                ctk.CTkLabel(self.frame_login, text="", image=self._logo_image).pack(pady=(4, 6))
            except Exception:
                pass

        ctk.CTkLabel(self.frame_login, text="FRS MERCADO", font=("Roboto", 18, "bold")).pack(pady=(0, 10))
        # Identificacao visual da versao (somente rotulo, sem logica).
        ctk.CTkLabel(
            self.frame_login,
            text=f"FRS Mercado v{_FRS_APP_VERSION}",
            font=("Roboto", 11),
            text_color="gray",
        ).pack(pady=(0, 6))

        status = self._status_licenca()
        ctk.CTkLabel(
            self.frame_login,
            text=str(status.get("message") or "Licença: indisponível"),
            text_color=str(status.get("color") or "#f1c40f"),
            font=("Roboto", 11, "bold"),
            wraplength=300,
            justify="center",
        ).pack(pady=(0, 6), padx=20)
        if status.get("is_expired"):
            ctk.CTkLabel(
                self.frame_login,
                text=(
                    "Modo restrito: PDV, vendas, estoque e financeiro bloqueados. "
                    "Exportação, relatórios, compra e ativação continuam liberados."
                ),
                text_color="#ffcc00",
                font=("Roboto", 10, "italic"),
                wraplength=300,
                justify="center",
            ).pack(pady=(0, 6), padx=20)

        ctk.CTkLabel(self.frame_login, text="Usuário:").pack(pady=(10, 0), padx=20, anchor="w")
        self.ent_usuario = ctk.CTkEntry(self.frame_login, width=300)
        self.ent_usuario.pack(pady=5, padx=20)

        ctk.CTkLabel(self.frame_login, text="Senha:").pack(pady=(10, 0), padx=20, anchor="w")
        self.ent_senha = ctk.CTkEntry(self.frame_login, width=300, show="*")
        self.ent_senha.pack(pady=5, padx=20)
        self.ent_senha.bind("<Return>", lambda e: self.tentar_entrar())

        self.btn_entrar = ctk.CTkButton(self.frame_login, text="ENTRAR", fg_color="green", command=self.tentar_entrar)
        self.btn_entrar.pack(pady=(22, 12), padx=20)

        acoes_licenca = ctk.CTkFrame(self.frame_login, fg_color="transparent")
        acoes_licenca.pack(pady=(4, 6), padx=20, fill="x")

        ctk.CTkButton(
            acoes_licenca,
            text="COMPRAR LICENÇA",
            fg_color="#1f6aa5",
            hover_color="#144870",
            command=self._abrir_comprar_licenca,
        ).pack(fill="x", pady=(0, 8))

        ctk.CTkButton(
            acoes_licenca,
            text="ATIVAR LICENÇA",
            fg_color="#7f8c8d",
            hover_color="#5d6d74",
            command=abrir_ativacao,
        ).pack(fill="x")


    def tentar_entrar(self):
        usuario = self.ent_usuario.get()
        senha = self.ent_senha.get()
        
        if not usuario or not senha:
            messagebox.showwarning("Aviso", "Preencha todos os campos.")
            return

        print(f"Tentando validar usuário: [{usuario}]")
        senha_hash = hashlib.sha256(senha.encode()).hexdigest()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, nome, permissao FROM usuarios WHERE nome = ? AND senha_hash = ?", 
                             (usuario, senha_hash))
                resultado = cursor.fetchone()

            if resultado:
                if self.winfo_exists():
                    print(f"Resultado da busca no banco: [SUCESSO] - Usuário ID {resultado[0]} autenticado.")
                    user_info = {"id": resultado[0], "nome": resultado[1], "permissao": resultado[2]}
                    registrar_log(user_info["id"], "Login", "Sucesso", f"Usuário {user_info['nome']} iniciou sessão.")

                    # Ordem crítica para evitar 'grab failed':
                    # 1. Desativa interação, 2. Libera o foco, 3. Oculta, 4. Inicia próximo módulo
                    try:
                        if self.winfo_exists():
                            self.btn_entrar.configure(state="disabled")
                        if self.winfo_exists():
                            self.grab_release()
                        if self.winfo_exists():
                            self.withdraw()
                    except Exception:
                        pass

                    try:
                        if self.winfo_exists():
                            self.destroy()
                    except Exception:
                        pass
                    
                    if self.callback_sucesso:
                        # Notifica a tela principal após a destruição do login.
                        try:
                            if self.parent and self.parent.winfo_exists():
                                self.parent.after(10, lambda: self.callback_sucesso(user_info))
                            else:
                                self.callback_sucesso(user_info)
                        except Exception:
                            self.callback_sucesso(user_info)
                    
                    print(f"Sessão iniciada: {user_info['nome']} ({user_info['permissao']})")

            else:
                print("Resultado da busca no banco: [FALHA] - Credenciais não encontradas ou incorretas.")
                registrar_log(None, "Login", "Falha", f"Tentativa inválida para o usuário: {usuario}")
                messagebox.showerror("Erro", "Usuário ou senha inválidos.")
                self.ent_senha.delete(0, 'end')
                
        except Exception as e:
            messagebox.showerror("Erro", f"Erro de conexão: {e}")

def chamar_tela_principal(user_info):
    """Importa e instancia a interface principal de forma limpa."""
    # Import dinâmico para evitar importação circular
    import modulo_main
    print(f"Lançando interface principal para: {user_info['nome']}")
    modulo_main.iniciar_sistema(user_info)

if __name__ == "__main__":
    print("Iniciando interface de login...")
    try:
        # Cria uma instância da aplicação CustomTkinter (janela raiz)
        app = ctk.CTk()
        app.withdraw() # Esconde a janela raiz, pois a ModuloLogin será a primeira a aparecer
        
        def _ao_logar_com_sucesso(user_info):
            global _USUARIO_LOGADO
            _USUARIO_LOGADO = user_info
            try:
                if login_window.winfo_exists():
                    # A destruição do login é a última etapa antes de iniciar o sistema principal.
                    login_window.destroy()
            except Exception:
                pass
            try:
                if app.winfo_exists():
                    app.quit()
            except Exception:
                pass

        # Ao logar com sucesso, encerra o fluxo de login e inicia o sistema após o mainloop terminar
        login_window = ModuloLogin(app, callback_sucesso=_ao_logar_com_sucesso)
        app.mainloop() # Inicia o loop de eventos da interface gráfica

        if _USUARIO_LOGADO:
            chamar_tela_principal(_USUARIO_LOGADO)

        try:
            if app.winfo_exists():
                app.destroy()
        except Exception:
            pass
    except Exception as e:
        import traceback
        print(f"Erro crítico ao iniciar a aplicação de login: {e}")
        traceback.print_exc()
    print("Interface de login finalizada.")