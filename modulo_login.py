import customtkinter as ctk
from tkinter import messagebox
import hashlib
import uuid
import webbrowser
import os
import sys
from app_paths import obter_caminho_dados
from database_manager import get_db_connection, registrar_log
from datetime import datetime, timedelta
from modulo_config import carregar_configuracoes # Para obter a Razão Social


_USUARIO_LOGADO = None
RENOVACAO_URL = "https://invoice.infinitepay.io/plans/frsoficinadepesca/avka57U38g"

class ModuloLogin(ctk.CTkToplevel):
    def __init__(self, parent, callback_sucesso):
        super().__init__(parent)
        self.parent = parent
        self.callback_sucesso = callback_sucesso
        
        self.title("Autenticação - Mercado FRS")
        self.geometry("400x400")
        
        # Garante que fechar o login use o método de encerramento total do sistema
        self.protocol("WM_DELETE_WINDOW", 
                      getattr(self.parent, "fechar_sistema", self.parent.destroy))
        self.grab_set() # Bloqueia interação com janelas atrás
        
        # Lista para rastrear tarefas agendadas e evitar erros de "invalid command name"
        self._after_ids = []
        self.backup_google_autenticado = self._verificar_token_backup_local()

        # Centralizar janela
        self._registrar_after(10, self._centralizar)

        # A chave secreta deve ser a mesma usada no gerador_licenca.py.
        # Em produção, carregue de forma mais segura (ex: variável de ambiente).
        self.SECRET_SALT = "MinhaChaveSecretaSuperSeguraFRS2024!"
        ctk.set_appearance_mode("Dark")

        # Título do Sistema
        self.lbl_titulo = ctk.CTkLabel(self, text="SISTEMA DE GESTÃO", font=("Roboto", 20, "bold"))
        self.lbl_titulo.pack(pady=(20, 10))

        self.frame_login = ctk.CTkFrame(self)
        self.frame_setup = ctk.CTkFrame(self)
        self.frame_ativacao = ctk.CTkFrame(self)

        self._verificar_estado_sistema()

    def _verificar_token_backup_local(self):
        """No login, valida apenas existência local do token, sem qualquer chamada de rede."""
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
        # Cancela todas as tarefas agendadas
        for after_id in self._after_ids:
            try:
                self.after_cancel(after_id)
            except:
                pass
        self._after_ids.clear()
        super().destroy()

    def _get_hwid(self):
        """Gera um ID único baseado no hardware da máquina."""
        return str(uuid.getnode())

    def _gerar_assinatura_local(self, data_exp, hw1, hw2):
        """Gera uma assinatura para garantir que o banco não foi editado manualmente."""
        conteudo = f"{data_exp}|{hw1}|{hw2}|{self.SECRET_SALT}"
        return hashlib.sha256(conteudo.encode()).hexdigest()

    def _modo_desenvolvedor_ativo(self):
        """Ativa autoassinatura para ambiente de desenvolvimento.

        Regras:
        - Execução não empacotada (script Python) é tratada como desenvolvimento.
        - Em executável, pode ser habilitado com FRS_DEV_TRUST_DB=1.
        """
        if not getattr(sys, "frozen", False):
            return True

        flag = os.environ.get("FRS_DEV_TRUST_DB", "0").strip().lower()
        return flag in {"1", "true", "yes", "on"}

    def _recalcular_e_atualizar_assinatura(self, row_id, data_exp, hw1, hw2, origem="sistema"):
        """Recalcula e persiste assinatura da licença para marcar estado atual como válido."""
        try:
            nova_assinatura = self._gerar_assinatura_local(data_exp, hw1, hw2)
            with get_db_connection() as conn:
                conn.execute("UPDATE licenca SET assinatura = ? WHERE rowid = ?", (nova_assinatura, row_id))
            registrar_log(None, "Integridade de Licença", "Sucesso", f"Assinatura recalculada ({origem}).")
            print(f"[INTEGRIDADE] Assinatura de licença atualizada ({origem}).")
        except Exception as e:
            print(f"[ERRO INTEGRIDADE] Falha ao atualizar assinatura ({origem}): {e}")

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

    def _verificar_estado_sistema(self):
        """Define qual tela exibir com base no banco de dados."""
        conn = None
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM usuarios")
                tem_usuarios = cursor.fetchone()[0] > 0
                
                # Verifica Licença e Integridade
                cursor.execute("SELECT rowid, data_expiracao, hwid1, hwid2, assinatura FROM licenca LIMIT 1")
                res_lic = cursor.fetchone()
                
                current_hwid = self._get_hwid()
                licenca_data = None
                
                if res_lic:
                    lic_rowid, exp_str, hw1, hw2, sig = res_lic
                    assinatura_esperada = self._gerar_assinatura_local(exp_str, hw1, hw2)

                    if sig != assinatura_esperada:
                        aviso = "Assinatura da licença divergente; seguindo com validação por data."
                        print(f"[AVISO INTEGRIDADE] {aviso}")
                        registrar_log(None, "Integridade de Licença", "Aviso", aviso)

                    # Em modo desenvolvedor, o sistema reaprende o estado atual do banco como válido.
                    if self._modo_desenvolvedor_ativo() and sig != assinatura_esperada:
                        self._recalcular_e_atualizar_assinatura(
                            lic_rowid,
                            exp_str,
                            hw1,
                            hw2,
                            origem="bootstrap-dev",
                        )
                    
                    # Verifica se esta máquina está autorizada (Multi-instalação)
                    if hw1 and hw2 and current_hwid not in [hw1, hw2]:
                        self._configurar_tela_ativacao("Limite de 2 computadores atingido.")
                        return
                    licenca_data = datetime.strptime(exp_str, '%Y-%m-%d')

            if not tem_usuarios:
                print("[SISTEMA] Banco vazio detectado. Redirecionando para Setup Inicial.")
                self._configurar_setup_inicial()
            elif licenca_data and datetime.now() > licenca_data:
                self._configurar_tela_ativacao()
            else:
                dias_restantes = (licenca_data - datetime.now()).days if licenca_data else 99
                self._configurar_tela_login(aviso_vencimento=dias_restantes)
        except Exception as e:
            messagebox.showerror("Erro Crítico", f"Erro ao iniciar segurança: {e}")
            # Se houver erro crítico na verificação, garantimos o fechamento seguro
            if self.winfo_exists():
                self._encerrar_aplicacao_segura()
        finally:
            # Evita erro de variável local não associada e garante fechamento seguro.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

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
            data_exp = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')

            try:
                with get_db_connection() as conn:
                    conn.execute("INSERT INTO usuarios (nome, senha_hash, permissao) VALUES (?, ?, 'Administrador')", (u, senha_hash))
                    conn.execute("INSERT INTO licenca (data_expiracao) VALUES (?)", (data_exp,))
            except Exception as e:
                messagebox.showerror("Erro", "Não foi possível concluir o setup inicial. Tente novamente.")
                print(f"[ERRO SETUP] Falha ao gravar setup inicial: {e}")
                return

            messagebox.showinfo("Sucesso", "Sistema inicializado com 30 dias de licença trial.")
            self.frame_setup.pack_forget()
            self._verificar_estado_sistema()

        ctk.CTkButton(self.frame_setup, text="FINALIZAR SETUP", command=realizar_setup).pack(pady=20)

    def _configurar_tela_ativacao(self, msg=None):
        self.frame_login.pack_forget()
        self.frame_setup.pack_forget()
        self.lbl_titulo.configure(text="LICENÇA EXPIRADA", text_color="#FF5555")
        self.frame_ativacao.pack(padx=30, pady=10, fill="both", expand=True)

        if msg:
            ctk.CTkLabel(self.frame_ativacao, text=msg, text_color="#FFCC00", font=("Arial", 11, "bold")).pack(pady=(10, 0))

        ctk.CTkLabel(self.frame_ativacao, text="Insira o Código de Ativação:", text_color="orange").pack(pady=20)
        self.ent_codigo = ctk.CTkEntry(self.frame_ativacao, width=300, placeholder_text="XXXX-XXXX-XXXX")
        self.ent_codigo.pack(pady=10)

        def validar_ativacao():
            entered_code = self.ent_codigo.get().strip()

            # 1. Obter o identificador do cliente (Razão Social)
            try:
                config = carregar_configuracoes()
                client_identifier = config.get("razao_social", "").strip().upper()
            except Exception as e:
                messagebox.showerror("Erro", "Falha ao carregar configurações para ativação.")
                print(f"[ERRO LICENCA] Falha ao carregar configuração: {e}")
                return
            
            if not client_identifier:
                messagebox.showerror("Erro", "Razão Social não configurada. Por favor, configure os dados do mercado em 'Configurações Globais'.")
                registrar_log(None, "Ativação de Licença", "Falha", "Razão Social não configurada.")
                return

            # 2. Calcular a data de expiração esperada (365 dias a partir de HOJE)
            expected_expiration_date = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
            
            # 3. Gerar o hash esperado com base nos dados e no salt
            data_to_hash = f"{client_identifier}-{expected_expiration_date}-{self.SECRET_SALT}"
            expected_hash = hashlib.sha256(data_to_hash.encode()).hexdigest()

            partes_codigo = entered_code.split('-')
            if len(partes_codigo) < 4:
                messagebox.showerror("Erro", "Código de ativação em formato inválido.")
                registrar_log(None, "Ativação de Licença", "Falha", f"Formato inválido para {client_identifier}.")
                return

            hash_parte = partes_codigo[3]
            if expected_hash[:16] == hash_parte[:16]:
                try:
                    with get_db_connection() as conn:
                        conn.execute("UPDATE licenca SET data_expiracao = ?", (expected_expiration_date,))
                except Exception as e:
                    messagebox.showerror("Erro", "Licença válida, porém não foi possível gravar no banco.")
                    print(f"[ERRO LICENCA] Falha ao atualizar licença: {e}")
                    registrar_log(None, "Ativação de Licença", "Falha", "Código válido, mas erro ao gravar no banco.")
                    return
                messagebox.showinfo("Ativado", f"Licença renovada por 365 dias! Nova validade: {expected_expiration_date}")
                registrar_log(None, "Ativação de Licença", "Sucesso", f"Licença renovada para {client_identifier} até {expected_expiration_date}.")
                self.frame_ativacao.pack_forget() # Esconde a tela de ativação
                self._verificar_estado_sistema() # Re-verifica o estado para ir para o login
            else:
                messagebox.showerror("Erro", "Código de ativação inválido.")
                registrar_log(None, "Ativação de Licença", "Falha", f"Código inválido inserido para {client_identifier}.")

        ctk.CTkButton(self.frame_ativacao, text="ATIVAR SISTEMA", command=validar_ativacao).pack(pady=20)

    def _configurar_tela_login(self, aviso_vencimento):
        self.frame_setup.pack_forget()
        self.frame_ativacao.pack_forget()
        self.frame_login.pack(padx=30, pady=10, fill="both", expand=True)

        def abrir_link_renovacao():
            try:
                webbrowser.open(RENOVACAO_URL, new=2)
            except Exception as e:
                messagebox.showerror("Erro", "Não foi possível abrir o link de renovação no navegador.")
                print(f"[ERRO RENOVACAO] Falha ao abrir URL: {e}")

        if aviso_vencimento <= 5:
            lbl_aviso = ctk.CTkLabel(self.frame_login, 
                                     text=f"Sua licença vence em {aviso_vencimento} dias!", 
                                     text_color="#FFCC00", font=("Arial", 11, "bold"))
            lbl_aviso.pack(pady=5)
            ctk.CTkButton(
                self.frame_login,
                text="RENOVAR ASSINATURA",
                fg_color="#1f6aa5",
                hover_color="#144870",
                command=abrir_link_renovacao,
            ).pack(pady=(0, 10))

        ctk.CTkLabel(self.frame_login, text="Usuário:").pack(pady=(10, 0), padx=20, anchor="w")
        self.ent_usuario = ctk.CTkEntry(self.frame_login, width=300)
        self.ent_usuario.pack(pady=5, padx=20)

        ctk.CTkLabel(self.frame_login, text="Senha:").pack(pady=(10, 0), padx=20, anchor="w")
        self.ent_senha = ctk.CTkEntry(self.frame_login, width=300, show="*")
        self.ent_senha.pack(pady=5, padx=20)
        self.ent_senha.bind("<Return>", lambda e: self.tentar_entrar())

        self.btn_entrar = ctk.CTkButton(self.frame_login, text="ENTRAR", fg_color="green", command=self.tentar_entrar)
        self.btn_entrar.pack(pady=30, padx=20)

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

                    # Desenvolvedor pode consolidar a assinatura atual após bootstrap de sucesso.
                    if self._modo_desenvolvedor_ativo():
                        try:
                            with get_db_connection() as conn:
                                lic = conn.execute(
                                    "SELECT rowid, data_expiracao, hwid1, hwid2 FROM licenca LIMIT 1"
                                ).fetchone()
                            if lic:
                                self._recalcular_e_atualizar_assinatura(
                                    lic[0],
                                    lic[1],
                                    lic[2],
                                    lic[3],
                                    origem="login-dev",
                                )
                        except Exception as e:
                            print(f"[ERRO INTEGRIDADE] Falha no recálculo pós-login: {e}")
                    
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