import sys
import os
import json
import time
import sqlite3
import threading
import traceback
import webbrowser
from pathlib import Path
from datetime import datetime
import customtkinter as ctk
from tkinter import messagebox
from urllib.parse import quote
from database_manager import get_db_connection, get_db_path, obter_caminho_dados, registrar_log
from modulo_config import carregar_configuracoes
from updater import Updater
from system_monitor import SystemMonitor


def _log_debug(contexto: str, erro: Exception | None = None) -> None:
    log_path = Path(obter_caminho_dados("log_debug.txt"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {contexto}\n")
        if erro is not None:
            f.write(f"Erro: {erro}\n")
            f.write(traceback.format_exc())
        f.write("\n" + ("-" * 80) + "\n")

class AppPrincipal(ctk.CTk):
    def __init__(self, usuario_sessao=None):
        super().__init__()
        self.title("Sistema PDV - Mercado FRS")
        self.geometry("1100x700")
        self.sistema_pronto = False
        self.admin_cadastrado = False
        self._mentoria_agendada = False
        self._mentora_exibida_sessao = False
        self._poll_venda_ativa = False
        self._vendas_iniciais = 0
        self._watchdog_janela_ativo = True
        self._modulos_abertos = {}
        self._modulos_em_abertura = set()
        self._erro_abertura_em_exibicao = set()
        self._erro_permissao_em_exibicao = set()
        self._ia_gestao = None
        self._modulo_financeiro = None
        self._janela_pdv = None
        self.last_activity_time = time.time()
        self.pode_rodar_backup = False
        self._backup_em_execucao = False
        self._backup_executado_no_ciclo_ocioso = False
        self._monitor_ociosidade_ativo = False
        self._updater = Updater(parent=self)
        self._system_monitor = None
        self._fiscal_alerta_em_exibicao = False
        self._license_status = {"expired": False, "warning": False, "message": "Licença: Verificando...", "color": "#f1c40f", "renewal_url": ""}
        self._license_block_alert_open = False
        self._resgatar_janela()
        
        # Define o protocolo para encerramento total do processo ao fechar a janela
        self.protocol("WM_DELETE_WINDOW", self.fechar_sistema)
        
        self.usuario_atual = usuario_sessao or {}
        self._after_ids = [] # Lista para rastrear loops (IA, Dashboard, etc)
        # Lazy loading: evita consulta ao banco durante o __init__.
        self.admin_cadastrado = False

        # Fluxo de login passa a ser exclusivo do main.py.
        if not self.usuario_atual:
            aviso = "Sessão vazia recebida na transição Login -> Main. Inicialização abortada."
            print(f"[SESSAO] {aviso}")
            _log_debug(aviso)
            messagebox.showerror(
                "Sessão inválida",
                "Não foi possível iniciar sem autenticação. Reinicie pelo inicializador principal.",
            )
            self.fechar_sistema()
            return

        # Se já veio logado, inicializa direto.
        self.login_concluido(self.usuario_atual)

    def _get_ia_gestao(self):
        if self._ia_gestao is None:
            import ia_gestao as _ia_gestao

            self._ia_gestao = _ia_gestao
        return self._ia_gestao

    def _get_modulo_financeiro(self):
        if self._modulo_financeiro is None:
            import modulo_financeiro as _modulo_financeiro

            self._modulo_financeiro = _modulo_financeiro
        return self._modulo_financeiro

    def _resgatar_janela(self):
        """Garante que a janela principal apareça no foreground quando já estiver em execução."""
        try:
            self.deiconify()
        except Exception:
            pass

    def _registrar_atividade_usuario(self, _event=None):
        self.last_activity_time = time.time()
        self.pode_rodar_backup = False
        self._backup_executado_no_ciclo_ocioso = False

    def _iniciar_monitor_ociosidade(self):
        if self._monitor_ociosidade_ativo:
            return

        self._monitor_ociosidade_ativo = True
        self.bind_all("<KeyPress>", self._registrar_atividade_usuario, add="+")
        self.bind_all("<Button>", self._registrar_atividade_usuario, add="+")
        self._registrar_after(1000, self.verificar_ociosidade)
        self._registrar_after(5000, self._loop_backup_ocioso)

    def verificar_ociosidade(self):
        """Sinaliza backup quando houver 30s sem clique ou tecla do operador."""
        if not self.winfo_exists():
            return

        inativo_ha = time.time() - self.last_activity_time
        if inativo_ha >= 30 and not self._backup_executado_no_ciclo_ocioso:
            self.pode_rodar_backup = True
        else:
            self.pode_rodar_backup = False

        self._registrar_after(1000, self.verificar_ociosidade)

    def _loop_backup_ocioso(self):
        if not self.winfo_exists():
            return
        self.backup_se_ocioso()
        self._registrar_after(5000, self._loop_backup_ocioso)

    def backup_se_ocioso(self):
        """Executa backup no Drive somente quando o monitor de ociosidade liberar."""
        if not self.pode_rodar_backup or self._backup_em_execucao:
            return

        self._backup_em_execucao = True
        self.pode_rodar_backup = False
        self._backup_executado_no_ciclo_ocioso = True

        def _executar_backup_drive():
            try:
                from modulo_config import carregar_configuracoes, obter_status_backup_local
                from modulo_relatorio import ModuloRelatorio

                status_backup = obter_status_backup_local()
                if not status_backup.get("configurado"):
                    registrar_log(None, "Backup Drive", "Aviso", status_backup.get("mensagem", "Backup não configurado."))
                    _log_debug(status_backup.get("mensagem", "Backup não configurado."))
                    return

                config = carregar_configuracoes()
                folder_id = config.get("drive_backup_folder_id")
                pasta_backup = Path(obter_caminho_dados("backups"))
                pasta_backup.mkdir(parents=True, exist_ok=True)

                origem_db = get_db_path()
                nome_arquivo = f"backup_db_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                destino_backup = pasta_backup / nome_arquivo

                origem_conn = sqlite3.connect(origem_db)
                try:
                    destino_conn = sqlite3.connect(str(destino_backup))
                    try:
                        origem_conn.backup(destino_conn)
                    finally:
                        destino_conn.close()
                finally:
                    origem_conn.close()

                ModuloRelatorio.upload_para_drive(str(destino_backup), folder_id)
                registrar_log(None, "Backup Drive", "Sucesso", f"Backup enviado ao Drive: {nome_arquivo}")
                _log_debug(f"Backup Drive concluido: {nome_arquivo}")
            except Exception as e:
                registrar_log(None, "Backup Drive", "Falha", f"Falha no backup ocioso: {e}")
                _log_debug("Falha no backup ocioso", e)
            finally:
                self._backup_em_execucao = False

        threading.Thread(target=_executar_backup_drive, daemon=True).start()

    def _watchdog_visibilidade_janela(self):
        """Reexibe a janela principal caso seja minimizada/ocultada por engano."""
        if not self._watchdog_janela_ativo or not self.winfo_exists():
            return

        try:
            estado = self.state()
            if estado in ("iconic", "withdrawn"):
                self.deiconify()
                self.lift()
                self.focus_force()
        except Exception:
            pass

        self._registrar_after(2000, self._watchdog_visibilidade_janela)

    def _mostrar_erro_modulo(self, titulo, mensagem):
        """Mostra erro garantindo prioridade visual para a janela principal."""
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

        try:
            messagebox.showerror(titulo, mensagem, parent=self)
        except Exception:
            messagebox.showerror(titulo, mensagem)

    def _atualizar_painel_navegacao(self, modulo_nome):
        """Atualiza conteúdo central sem alterar visibilidade da janela principal."""
        if not hasattr(self, "lbl_modulo_ativo"):
            return
        agora = datetime.now().strftime("%H:%M:%S")
        self.lbl_modulo_ativo.configure(text=f"Módulo selecionado: {modulo_nome}")
        self.lbl_modulo_hora.configure(text=f"Ação registrada às {agora}")

    def _modulo_bloqueado_por_licenca(self, modulo_nome: str) -> bool:
        if not bool(self._license_status.get("expired", False)):
            return False
        nome = str(modulo_nome or "").upper()
        if nome == "PDV":
            return True
        return "FISCAL" in nome

    def _exibir_bloqueio_licenca(self):
        if self._license_block_alert_open:
            return

        self._license_block_alert_open = True
        msg = (
            "Licença expirada. Os módulos de Venda e Emissão Fiscal estão bloqueados até a renovação.\n\n"
            "Deseja abrir o link de renovação agora?"
        )
        renewal_url = str(self._license_status.get("renewal_url") or "").strip()
        try:
            abrir = messagebox.askyesno("Licença Expirada", msg, parent=self)
            if abrir and renewal_url:
                webbrowser.open(renewal_url, new=2)
        except Exception:
            pass
        finally:
            self._license_block_alert_open = False

    def _abrir_modulo_seguro(self, modulo_nome, opener):
        """Abre módulo com proteção de foco e tratamento de exceções sem esconder a raiz."""
        if self._modulo_bloqueado_por_licenca(modulo_nome):
            self._exibir_bloqueio_licenca()
            return

        self._atualizar_painel_navegacao(modulo_nome)
        self._resgatar_janela()

        janela_existente = self._modulos_abertos.get(modulo_nome)
        try:
            if janela_existente is not None and janela_existente.winfo_exists():
                janela_existente.deiconify()
                janela_existente.lift()
                janela_existente.focus_force()
                return
        except Exception:
            self._modulos_abertos.pop(modulo_nome, None)

        if modulo_nome in self._modulos_em_abertura:
            return

        self._modulos_em_abertura.add(modulo_nome)
        try:
            janela = opener()
            if janela is None:
                return
            if not isinstance(janela, ctk.CTkToplevel):
                raise TypeError(f"{modulo_nome} não retornou uma janela Toplevel válida.")

            self._modulos_abertos[modulo_nome] = janela
            self._erro_abertura_em_exibicao.discard(modulo_nome)
            self._erro_permissao_em_exibicao.discard(modulo_nome)
        except PermissionError as e:
            _log_debug(f"PermissionError ao abrir módulo: {modulo_nome}", e)
            if modulo_nome not in self._erro_permissao_em_exibicao:
                self._erro_permissao_em_exibicao.add(modulo_nome)
                self._mostrar_erro_modulo(
                    "Erro de Permissão",
                    f"Não foi possível abrir {modulo_nome} por falta de permissão.\nResumo: {e}",
                )

                def _liberar_erro_permissao():
                    self._erro_permissao_em_exibicao.discard(modulo_nome)

                self._registrar_after(1200, _liberar_erro_permissao)
            return
        except Exception as e:
            _log_debug(f"Falha ao abrir módulo: {modulo_nome}", e)
            if modulo_nome not in self._erro_abertura_em_exibicao:
                self._erro_abertura_em_exibicao.add(modulo_nome)
                self._mostrar_erro_modulo(
                    "Erro ao Abrir Módulo",
                    f"Não foi possível abrir {modulo_nome}.\nResumo: {e}",
                )

                def _liberar_erro():
                    self._erro_abertura_em_exibicao.discard(modulo_nome)

                self._registrar_after(1200, _liberar_erro)
        finally:
            self._modulos_em_abertura.discard(modulo_nome)

    def _verificar_admin_cadastrado(self):
        """Confirma se o cadastro de administrador já foi persistido no banco."""
        try:
            with get_db_connection() as conn:
                total_admin = conn.execute(
                    "SELECT COUNT(*) FROM usuarios WHERE permissao = 'Administrador'"
                ).fetchone()[0]
            return total_admin > 0
        except Exception as e:
            print(f"[ERRO MAIN] Falha ao verificar admin cadastrado: {e}")
            return False

    def _agendar_mentoria_ia(self):
        """Agenda a mentoria com atraso mínimo e somente quando o sistema estiver pronto."""
        if self._mentoria_agendada:
            return
        if not self.sistema_pronto or not self.admin_cadastrado:
            return
        self._mentoria_agendada = True
        self._vendas_iniciais = self._obter_total_vendas_dia()
        self._poll_venda_ativa = True
        self._registrar_after(30000, self._monitorar_primeira_venda)
        self._registrar_after(600000, self._gatilho_tempo_mentora)

    def _get_mentora_config_path(self):
        """Retorna o caminho do arquivo de configuração da mentora por usuário."""
        try:
            db_path = Path(get_db_path())
            app_dir = db_path.parent
        except Exception:
            app_dir = Path(__file__).resolve().parent
        app_dir.mkdir(parents=True, exist_ok=True)
        return app_dir / "config_mentora.json"

    def _carregar_config_mentora(self):
        config_path = self._get_mentora_config_path()
        if not config_path.exists():
            return {}
        try:
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _salvar_config_mentora(self, data):
        config_path = self._get_mentora_config_path()
        with config_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, indent=2)

    def _obter_total_vendas_dia(self):
        try:
            with get_db_connection() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM vendas WHERE date(data_venda) = date('now', 'localtime')"
                ).fetchone()[0]
                if total == 0:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM vendas_dia WHERE date(data_venda) = date('now', 'localtime')"
                    ).fetchone()[0]
                return total
        except Exception:
            return 0

    def _pode_exibir_mentora_hoje(self):
        """Aplica trava por sessão e janela de 24 horas para exibição da mentora."""
        if self._mentora_exibida_sessao:
            return False

        config = self._carregar_config_mentora()
        last_shown = config.get("last_shown_date")
        if not last_shown:
            return True

        try:
            last_dt = datetime.fromisoformat(last_shown)
        except ValueError:
            return True

        return (datetime.now() - last_dt).total_seconds() >= 86400

    def _registrar_exibicao_mentora(self):
        self._mentora_exibida_sessao = True
        config = self._carregar_config_mentora()
        config["last_shown_date"] = datetime.now().isoformat(timespec="seconds")
        self._salvar_config_mentora(config)

    def _monitorar_primeira_venda(self):
        """Dispara a mentora quando detectar a primeira venda da sessão."""
        if not self.winfo_exists() or not self._poll_venda_ativa:
            return

        vendas_atuais = self._obter_total_vendas_dia()
        if vendas_atuais > self._vendas_iniciais:
            self._poll_venda_ativa = False
            self.checar_mentoria_ia(origem="primeira_venda")
            return

        self._registrar_after(30000, self._monitorar_primeira_venda)

    def _gatilho_tempo_mentora(self):
        """Dispara a mentora após 10 minutos com o sistema em operação."""
        self.checar_mentoria_ia(origem="timer_10min")

    def _registrar_after(self, ms, command):
        """Agenda uma tarefa e armazena o ID para cancelamento seguro."""
        if self.winfo_exists():
            id_after = self.after(ms, command)
            self._after_ids.append(id_after)
            return id_after

    def fechar_sistema(self):
        """Realiza um encerramento limpo e forçado do processo Python no Windows."""
        self._watchdog_janela_ativo = False
        try:
            if self._system_monitor is not None:
                self._system_monitor.stop()
        except Exception:
            pass
        self.cancelar_loops()
        try:
            if self.winfo_exists():
                self.quit()
        except Exception:
            pass
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass
        # Medidas de segurança final para matar o processo no SO
        sys.exit()
        os._exit(0)

    def cancelar_loops(self):
        """Cancela todos os loops de IA e interface pendentes."""
        for after_id in self._after_ids:
            try: self.after_cancel(after_id)
            except: pass
        self._after_ids.clear()

    def login_concluido(self, usuario):
        self.usuario_atual = usuario
        self._resgatar_janela()
        self.deiconify() # Mostra a tela principal
        
        # Variáveis de controle do Alerta
        self.alertas_pendentes = []
        self.cor_index = 0
        self.pulsando = False
        
        # Cores para o efeito pulsar (Gradiante de tons de alerta)
        self.cores_pulso = ["#2b2b2b", "#3b1a1a", "#5e1919", "#8b1a1a", "#b31b1b", "#8b1a1a", "#5e1919", "#3b1a1a"]
        
        self.configurar_interface()
        self._iniciar_monitor_ociosidade()
        self.sistema_pronto = True
        self._watchdog_visibilidade_janela()
        self._registrar_after(120, self._inicializar_recursos_pos_login)

    def _inicializar_recursos_pos_login(self):
        """Carrega tarefas pesadas após a UI estar visível."""
        if not self.winfo_exists():
            return

        self.atualizar_dashboard()
        self.admin_cadastrado = self._verificar_admin_cadastrado()
        self._agendar_mentoria_ia()
        self.verificar_ia_loop()
        self._iniciar_system_monitor()
        self._iniciar_auto_update_silencioso()

    def _iniciar_system_monitor(self):
        try:
            if self._system_monitor is None:
                self._system_monitor = SystemMonitor(
                    on_status=self._on_system_status,
                    interval_seconds=6,
                )
            self._system_monitor.start()
        except Exception as e:
            _log_debug("Falha ao iniciar monitor de sistema", e)

    def _on_system_status(self, status):
        if not self.winfo_exists():
            return

        def _apply():
            if not self.winfo_exists():
                return

            self._license_status = {
                "expired": bool(status.get("license_expired", False)),
                "warning": bool(status.get("license_warning", False)),
                "message": str(status.get("license_text") or "Licença: Indisponível"),
                "color": str(status.get("license_color") or "#f1c40f"),
                "renewal_url": str(status.get("renewal_url") or ""),
            }

            try:
                self.lbl_status_licenca_sidebar.configure(
                    text=self._license_status["message"],
                    text_color=self._license_status["color"],
                )
            except Exception:
                pass

            try:
                self.lbl_status_fiscal.configure(
                    text=status.get("status_text", "Fiscal: Desativado"),
                    text_color=status.get("status_color", "#2ecc71"),
                )
            except Exception:
                pass

            try:
                self.lbl_status_header.configure(
                    text=status.get("header_text", "Licença/Fiscal: Indisponível"),
                    text_color=status.get("header_color", "#f1c40f"),
                )
                self.frame_navegacao.configure(border_color=status.get("header_color", "#f1c40f"))
            except Exception:
                pass

            alerta = str(status.get("alerta") or "").strip()
            if alerta and not self._fiscal_alerta_em_exibicao:
                self._fiscal_alerta_em_exibicao = True
                try:
                    messagebox.showwarning("Status Fiscal", alerta)
                except Exception:
                    pass
                finally:
                    self._registrar_after(30000, self._liberar_alerta_fiscal)

        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _liberar_alerta_fiscal(self):
        self._fiscal_alerta_em_exibicao = False

    def _iniciar_auto_update_silencioso(self):
        """Verifica atualização em background sem bloquear operação do caixa."""
        try:
            config = carregar_configuracoes()
            enabled = bool(config.get("auto_update_enabled", True))
            repo = str(config.get("auto_update_repo", "") or "").strip()
            remind_hours = int(config.get("auto_update_remind_hours", 24) or 24)
            self._updater.start_silent_check(
                repo=repo,
                enabled=enabled,
                remind_hours=remind_hours,
            )
        except Exception as e:
            _log_debug("Falha na inicialização do auto-update (ignorado)", e)

    def configurar_interface(self):
        # Limpar widgets se houver (para logout/re-login)
        self.cancelar_loops()
        
        for widget in self.winfo_children():
            widget.destroy()

        # --- BARRA LATERAL (Monitoramento) ---
        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        
        ctk.CTkLabel(self.sidebar, text="MONITORAMENTO", font=("Roboto", 14, "bold"), text_color="gray").pack(pady=(20, 10))

        self.lbl_status_fiscal = ctk.CTkLabel(
            self.sidebar,
            text="Fiscal: Verificando...",
            font=("Roboto", 11, "bold"),
            text_color="#f1c40f",
        )
        self.lbl_status_fiscal.pack(pady=(0, 6))

        self.lbl_status_licenca_sidebar = ctk.CTkLabel(
            self.sidebar,
            text="Licença: Verificando...",
            font=("Roboto", 10, "bold"),
            text_color="#f1c40f",
            wraplength=195,
            justify="left",
        )
        self.lbl_status_licenca_sidebar.pack(pady=(0, 10), padx=8)

        # Card Vendas
        self.card_vendas = ctk.CTkFrame(self.sidebar, width=200, height=300, corner_radius=15, fg_color="#1a1a1a")
        self.card_vendas.pack(padx=10, pady=10)
        self.card_vendas.pack_propagate(False)
        ctk.CTkLabel(self.card_vendas, text="Fluxo de Caixa (Hoje)", font=("Roboto", 11, "bold")).pack(pady=(8, 2))

        linha_headers = ctk.CTkFrame(self.card_vendas, fg_color="transparent")
        linha_headers.pack(fill="x", padx=8)
        ctk.CTkLabel(linha_headers, text="Bruto", width=60, font=("Roboto", 10, "bold"), text_color="#9ec5ff").pack(side="left")
        ctk.CTkLabel(linha_headers, text="Impostos", width=70, font=("Roboto", 10, "bold"), text_color="#ffb3b3").pack(side="left")
        ctk.CTkLabel(linha_headers, text="Líquido", width=60, font=("Roboto", 10, "bold"), text_color="#a6f4c5").pack(side="left")

        linha_valores = ctk.CTkFrame(self.card_vendas, fg_color="transparent")
        linha_valores.pack(fill="x", padx=8, pady=(2, 8))
        self.label_valor_bruto_dashboard = ctk.CTkLabel(linha_valores, text="R$ 0,00", width=60, font=("Roboto", 10, "bold"), text_color="#9ec5ff")
        self.label_valor_bruto_dashboard.pack(side="left")
        self.label_valor_impostos_dashboard = ctk.CTkLabel(linha_valores, text="R$ 0,00", width=70, font=("Roboto", 10, "bold"), text_color="#ffb3b3")
        self.label_valor_impostos_dashboard.pack(side="left")
        self.label_valor_liquido_dashboard = ctk.CTkLabel(linha_valores, text="R$ 0,00", width=60, font=("Roboto", 10, "bold"), text_color="#a6f4c5")
        self.label_valor_liquido_dashboard.pack(side="left")

        self.lbl_origem_ifood = ctk.CTkLabel(self.card_vendas, text="iFood: R$ 0,00", font=("Roboto", 9, "bold"), text_color="#b7d9ff")
        self.lbl_origem_ifood.pack(anchor="w", padx=10)
        self.lbl_origem_app = ctk.CTkLabel(self.card_vendas, text="App Próprio: R$ 0,00", font=("Roboto", 9, "bold"), text_color="#b7d9ff")
        self.lbl_origem_app.pack(anchor="w", padx=10)
        self.lbl_origem_loja = ctk.CTkLabel(self.card_vendas, text="Loja Física: R$ 0,00", font=("Roboto", 9, "bold"), text_color="#b7d9ff")
        self.lbl_origem_loja.pack(anchor="w", padx=10)

        ctk.CTkLabel(self.card_vendas, text="Tributos (Segregado)", font=("Roboto", 9, "bold"), text_color="#cccccc").pack(anchor="w", padx=10, pady=(8, 2))
        self.lbl_tributo_icms = ctk.CTkLabel(self.card_vendas, text="ICMS: R$ 0,00", font=("Roboto", 9), text_color="#ffd166")
        self.lbl_tributo_icms.pack(anchor="w", padx=10)
        self.lbl_tributo_pis = ctk.CTkLabel(self.card_vendas, text="PIS: R$ 0,00", font=("Roboto", 9), text_color="#ffd166")
        self.lbl_tributo_pis.pack(anchor="w", padx=10)
        self.lbl_tributo_cofins = ctk.CTkLabel(self.card_vendas, text="COFINS: R$ 0,00", font=("Roboto", 9), text_color="#ffd166")
        self.lbl_tributo_cofins.pack(anchor="w", padx=10)
        self.lbl_tributo_ibs = ctk.CTkLabel(self.card_vendas, text="IBS: R$ 0,00", font=("Roboto", 9), text_color="#a9f0ff")
        self.lbl_tributo_ibs.pack(anchor="w", padx=10)
        self.lbl_tributo_cbs = ctk.CTkLabel(self.card_vendas, text="CBS: R$ 0,00", font=("Roboto", 9), text_color="#a9f0ff")
        self.lbl_tributo_cbs.pack(anchor="w", padx=10)

        # Card Promoções
        self.card_promo = ctk.CTkFrame(self.sidebar, width=200, height=70, corner_radius=15, fg_color="#f1c40f")
        self.card_promo.pack(padx=10, pady=10)
        self.card_promo.pack_propagate(False)
        ctk.CTkLabel(self.card_promo, text="Promoções", text_color="black", font=("Roboto", 10, "bold")).pack(pady=(5, 0))
        self.lbl_promo_count = ctk.CTkLabel(self.card_promo, text="0 itens", text_color="black", font=("Roboto", 16, "bold"))
        self.lbl_promo_count.pack()

        # Card IA FRS
        self.card_ia = ctk.CTkFrame(self.sidebar, width=200, height=90, corner_radius=15, border_width=2)
        self.card_ia.pack(padx=10, pady=10)
        self.card_ia.pack_propagate(False)
        self.label_ia_titulo = ctk.CTkLabel(self.card_ia, text="IA FRS", font=("Roboto", 14, "bold"))
        self.label_ia_titulo.pack(pady=(15, 0))
        self.label_ia_status = ctk.CTkLabel(self.card_ia, text="Normal", font=("Roboto", 10))
        self.label_ia_status.pack()

        # Espaçador e Fechamento
        ctk.CTkLabel(self.sidebar, text="").pack(expand=True)
        self.btn_fechar_caixa = ctk.CTkButton(
            self.sidebar, text="FECHAR CAIXA", fg_color="#c0392b", hover_color="#e74c3c",
            command=self.executar_fechamento, height=45
        )
        self.btn_fechar_caixa.pack(padx=20, pady=20, fill="x")
        if self.usuario_atual.get("permissao") != "Administrador":
            self.btn_fechar_caixa.configure(state="disabled")

        # --- ÁREA CENTRAL (Menu de Ações) ---
        self.main_container = ctk.CTkFrame(self, fg_color="black", corner_radius=0)
        self.main_container.pack(side="right", fill="both", expand=True)

        ctk.CTkLabel(self.main_container, text="MENU PRINCIPAL", font=("Roboto", 24, "bold")).pack(pady=30)

        self.frame_navegacao = ctk.CTkFrame(self.main_container, fg_color="#111111", border_width=1, border_color="#333333")
        self.frame_navegacao.pack(fill="x", padx=40, pady=(0, 10))
        self.lbl_modulo_ativo = ctk.CTkLabel(
            self.frame_navegacao,
            text="Módulo selecionado: Nenhum",
            font=("Roboto", 14, "bold"),
        )
        self.lbl_modulo_ativo.pack(anchor="w", padx=14, pady=(10, 0))
        self.lbl_modulo_hora = ctk.CTkLabel(
            self.frame_navegacao,
            text="Aguardando ação do usuário",
            font=("Roboto", 11),
            text_color="gray",
        )
        self.lbl_modulo_hora.pack(anchor="w", padx=14, pady=(2, 10))

        self.lbl_status_header = ctk.CTkLabel(
            self.frame_navegacao,
            text="Licença/Fiscal: Verificando...",
            font=("Roboto", 11, "bold"),
            text_color="#f1c40f",
            wraplength=700,
            justify="left",
        )
        self.lbl_status_header.pack(anchor="w", padx=14, pady=(0, 10))

        # Grid Centralizado com Scroll
        self.scroll_menu = ctk.CTkScrollableFrame(self.main_container, fg_color="transparent")
        self.scroll_menu.pack(fill="both", expand=True, padx=40, pady=10)
        self.scroll_menu.grid_columnconfigure((0, 1), weight=1)

        self._criar_botoes_menu()

    def _criar_botoes_menu(self):
        """Gera os cards grandes de ação no centro."""
        botoes = [
            ("🚀 ABRIR PDV", "#27ae60", self.abrir_pdv),
            ("📦 ESTOQUE", "#2980b9", self.abrir_estoque),
            ("📊 RELATÓRIOS", "#16a085", self.abrir_relatorios),
            ("🧾 ORÇAMENTOS", "#8d6e63", self.abrir_orcamentos),
            ("👥 CLIENTES", "#0f766e", self.abrir_clientes),
            ("🏭 FORNECEDORES", "#6d28d9", self.abrir_fornecedores),
            ("👤 USUÁRIOS", "#e67e22", self.abrir_usuarios),
            ("⚙️ CONFIGS", "#7f8c8d", self.abrir_configuracoes),
            ("💰 TAXAS", "#8e44ad", self.abrir_financeiro),
            ("📱 APP DE CELULAR", "#0b7285", self.abrir_fluxo_app_celular),
            ("⬆️ VERIFICAR ATUALIZAÇÕES", "#2c3e50", self.verificar_atualizacoes_manual),
        ]

        for i, (texto, cor, cmd) in enumerate(botoes):
            row = i // 2
            col = i % 2
            btn = ctk.CTkButton(
                self.scroll_menu, text=texto, font=("Roboto", 18, "bold"),
                fg_color=cor, hover_color=cor, height=120, corner_radius=15,
                command=cmd
            )
            btn.grid(row=row, column=col, padx=15, pady=15, sticky="nsew")

    def abrir_pdv(self):
        self._abrir_modulo_seguro("PDV", self._abrir_pdv_impl)

    def _abrir_pdv_impl(self):
        from modulo_pdv import ModuloPDV

        try:
            if self._janela_pdv is not None and self._janela_pdv.winfo_exists():
                self._janela_pdv.deiconify()
                self._janela_pdv.lift()
                self._janela_pdv.focus_force()
                return self._janela_pdv
        except Exception:
            self._janela_pdv = None

        janela = ModuloPDV(self)
        self._janela_pdv = janela

        def _limpar_referencia_pdv(_event=None):
            self._janela_pdv = None

        try:
            janela.bind("<Destroy>", _limpar_referencia_pdv, add="+")
        except Exception:
            pass

        return janela

    def abrir_estoque(self):
        self._abrir_modulo_seguro("ESTOQUE", self._abrir_estoque_impl)

    def _abrir_estoque_impl(self):
        from modulo_estoque import ModuloEstoque
        janela = ModuloEstoque(self)
        if janela is not None and hasattr(janela, "carregar_produtos_ao_abrir_aba"):
            janela.carregar_produtos_ao_abrir_aba()
        return janela

    def abrir_relatorios(self):
        self._abrir_modulo_seguro("RELATÓRIOS", self._abrir_relatorios_impl)

    def _abrir_relatorios_impl(self):
        from modulo_relatorio import ModuloRelatorio
        return ModuloRelatorio(self)

    def abrir_usuarios(self):
        self._abrir_modulo_seguro("USUÁRIOS", self._abrir_usuarios_impl)

    def _abrir_usuarios_impl(self):
        from modulo_usuario import ModuloUsuario
        return ModuloUsuario(self, is_admin_user=(self.usuario_atual.get("permissao") == "Administrador"))

    def abrir_clientes(self):
        self._abrir_modulo_seguro("CLIENTES", self._abrir_clientes_impl)

    def _abrir_clientes_impl(self):
        from modulo_cliente import ModuloCliente
        return ModuloCliente(self)

    def abrir_fornecedores(self):
        self._abrir_modulo_seguro("FORNECEDORES", self._abrir_fornecedores_impl)

    def _abrir_fornecedores_impl(self):
        from modulo_fornecedor import ModuloFornecedor
        return ModuloFornecedor(self)

    def abrir_orcamentos(self):
        self._abrir_modulo_seguro("ORÇAMENTOS", self._abrir_orcamentos_impl)

    def _abrir_orcamentos_impl(self):
        from modulo_orcamento import ModuloOrcamento
        return ModuloOrcamento(self)

    def abrir_configuracoes(self):
        self._abrir_modulo_seguro("CONFIGURAÇÕES", self._abrir_configuracoes_impl)

    def _abrir_configuracoes_impl(self):
        from modulo_config import exibir_configuracoes
        return exibir_configuracoes(self)

    def abrir_financeiro(self):
        self._abrir_modulo_seguro("TAXAS", self._abrir_financeiro_impl)

    def _abrir_financeiro_impl(self):
        from modulo_financeiro import JanelaConfigTaxas
        return JanelaConfigTaxas(self, self.usuario_atual)

    def _resolver_apk_embutido(self) -> Path | None:
        candidatos = [
            Path(sys.executable).resolve().parent / "mobile" / "mercado.apk" if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "mobile" / "mercado.apk",
            Path(__file__).resolve().parent / "release_final" / "mercado.apk",
            Path(__file__).resolve().parent / "mobile_app" / "build" / "apk" / "mercado.apk",
        ]
        for apk in candidatos:
            if apk.exists() and apk.is_file():
                return apk
        return None

    def abrir_fluxo_app_celular(self):
        apk_path = self._resolver_apk_embutido()
        if apk_path is None:
            messagebox.showwarning(
                "APK não encontrado",
                "Não foi possível localizar o mercado.apk nesta instalação. Reinstale usando o setup completo all-in-one.",
            )
            return

        pasta_apk = apk_path.parent
        try:
            os.startfile(str(pasta_apk))
        except Exception as exc:
            _log_debug("Falha ao abrir pasta do APK", exc)

        msg = (
            "APK local pronto para envio.\\n\\n"
            f"Arquivo: {apk_path.name}\\n"
            f"Pasta: {pasta_apk}\\n\\n"
            "A pasta será aberta agora para anexar o arquivo no WhatsApp Desktop.\\n"
            "Deseja também abrir o WhatsApp Web com mensagem pronta?"
        )
        abrir_web = messagebox.askyesno("APP de celular", msg)
        if abrir_web:
            texto = quote(
                "Segue o APK oficial do FRS Mercado. "
                "Anexe o arquivo mercado.apk que está na pasta aberta no seu computador."
            )
            webbrowser.open(f"https://wa.me/?text={texto}")

    def verificar_atualizacoes_manual(self):
        """Gatilho manual para checagem imediata de atualização no GitHub."""
        try:
            config = carregar_configuracoes()
            repo = str(config.get("auto_update_repo", "") or "").strip()
            if not repo:
                messagebox.showwarning("Atualização", "Repositório de atualização não configurado.")
                return

            release = self._updater.checar_atualizacao(repo)
            if release is None:
                messagebox.showinfo("Atualização", "Você já está na versão mais recente.")
                return

            confirmar = messagebox.askyesno(
                "Atualização disponível",
                f"Nova versão encontrada: {release.version}. Deseja atualizar agora?",
            )
            if confirmar:
                self._updater.aplicar_atualizacao(repo)
        except Exception as e:
            messagebox.showwarning("Atualização", f"Falha ao iniciar verificação manual: {e}")

    def verificar_ia_loop(self):
        """Verifica alertas e atualiza faturamento a cada 60 segundos."""
        if not self.winfo_exists():
            return

        try:
            ia_gestao = self._get_ia_gestao()
            ia_gestao.restaurar_precos_originais() # Limpa promoções vencidas
            self.alertas_pendentes = ia_gestao.verificar_alertas()
        except Exception as e:
            self.alertas_pendentes = []
            self.label_ia_status.configure(text="Falha na IA", text_color="#ff6666")
            print(f"[ERRO IA] Falha no monitoramento: {e}")
        
        if self.alertas_pendentes:
            self.label_ia_status.configure(text=f"⚠️ {len(self.alertas_pendentes)} ALERTAS", text_color="#ffcc00")
            if not self.pulsando:
                self.pulsando = True
                self.animar_pulso()
        else:
            self.pulsando = False
            self.label_ia_status.configure(text="SISTEMA NORMAL", text_color="white")
            self.card_ia.configure(fg_color="#2b2b2b", border_color="#404040")
            
        self.atualizar_dashboard()
        # Agenda a próxima verificação para daqui a 60 segundos
        self._registrar_after(60000, self.verificar_ia_loop)

    def atualizar_dashboard(self):
        """Atualiza os valores financeiros exibidos nos cards."""
        try:
            modulo_financeiro = self._get_modulo_financeiro()
            resumo = modulo_financeiro.obter_resumo_fluxo_caixa_dia()
            origem = modulo_financeiro.obter_resumo_origem_dia()
            self.label_valor_bruto_dashboard.configure(text=f"R$ {resumo['valor_bruto']:.2f}")
            self.label_valor_impostos_dashboard.configure(text=f"R$ {resumo['valor_impostos']:.2f}")
            self.label_valor_liquido_dashboard.configure(text=f"R$ {resumo['valor_liquido']:.2f}")
            self.lbl_origem_ifood.configure(text=f"iFood: R$ {origem.get('IFOOD', 0.0):.2f}")
            self.lbl_origem_app.configure(text=f"App Próprio: R$ {origem.get('APP_PROPRIO', 0.0):.2f}")
            self.lbl_origem_loja.configure(text=f"Loja Física: R$ {origem.get('LOJA_FISICA', 0.0):.2f}")
            self.lbl_tributo_icms.configure(text=f"ICMS: R$ {resumo.get('valor_icms', 0.0):.2f}")
            self.lbl_tributo_pis.configure(text=f"PIS: R$ {resumo.get('valor_pis', 0.0):.2f}")
            self.lbl_tributo_cofins.configure(text=f"COFINS: R$ {resumo.get('valor_cofins', 0.0):.2f}")
            self.lbl_tributo_ibs.configure(text=f"IBS: R$ {resumo.get('valor_ibs', 0.0):.2f}")
            self.lbl_tributo_cbs.configure(text=f"CBS: R$ {resumo.get('valor_cbs', 0.0):.2f}")
        except Exception as e:
            self.label_valor_bruto_dashboard.configure(text="R$ 0,00")
            self.label_valor_impostos_dashboard.configure(text="R$ 0,00")
            self.label_valor_liquido_dashboard.configure(text="R$ 0,00")
            self.lbl_origem_ifood.configure(text="iFood: R$ 0,00")
            self.lbl_origem_app.configure(text="App Próprio: R$ 0,00")
            self.lbl_origem_loja.configure(text="Loja Física: R$ 0,00")
            self.lbl_tributo_icms.configure(text="ICMS: R$ 0,00")
            self.lbl_tributo_pis.configure(text="PIS: R$ 0,00")
            self.lbl_tributo_cofins.configure(text="COFINS: R$ 0,00")
            self.lbl_tributo_ibs.configure(text="IBS: R$ 0,00")
            self.lbl_tributo_cbs.configure(text="CBS: R$ 0,00")
            print(f"[ERRO DASHBOARD] Falha ao obter total de vendas: {e}")

        # Atualiza contagem de promoções
        try:
            with get_db_connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM produtos WHERE preco_base IS NOT NULL").fetchone()[0]
            self.lbl_promo_count.configure(text=f"{count} itens")
        except Exception as e:
            self.lbl_promo_count.configure(text="indisponível")
            print(f"[ERRO DASHBOARD] Falha ao obter promoções: {e}")

    def exibir_lista_promocoes(self):
        """Mostra janela com itens em promoção."""
        janela = ctk.CTkToplevel(self)
        janela.title("Itens em Promoção")
        janela.geometry("400x300")

        try:
            with get_db_connection() as conn:
                promos = conn.execute("SELECT nome, preco_venda, preco_base FROM produtos WHERE preco_base IS NOT NULL").fetchall()
        except Exception as e:
            messagebox.showerror("Erro", "Não foi possível carregar promoções no momento.")
            print(f"[ERRO PROMO] Falha ao consultar promoções: {e}")
            janela.destroy()
            return
        
        scroll = ctk.CTkScrollableFrame(janela)
        scroll.pack(fill="both", expand=True, padx=10, pady=10)
        
        for p in promos:
            desc = f"{p[0]}: R$ {p[1]:.2f} (Era: R$ {p[2]:.2f})"
            ctk.CTkLabel(scroll, text=desc, anchor="w").pack(fill="x")

    def executar_fechamento(self):
        """Chama a lógica de fechamento de caixa."""
        if messagebox.askyesno("Fechamento", "Deseja realmente fechar o caixa agora?"):
            modulo_financeiro = self._get_modulo_financeiro()
            sucesso, msg = modulo_financeiro.fechar_caixa()
            if sucesso:
                messagebox.showinfo("Sucesso", msg)
                self.atualizar_dashboard()
            else:
                messagebox.showwarning("Aviso", msg)

    def animar_pulso(self):
        """Cria o efeito visual de pulsação suave."""
        if not self.pulsando or not self.winfo_exists():
            return
            
        # Alterna a cor de fundo com base na lista de gradiente
        cor_atual = self.cores_pulso[self.cor_index]
        self.card_ia.configure(fg_color=cor_atual, border_color="#ff4444" if self.cor_index > 3 else "#404040")
        
        self.cor_index = (self.cor_index + 1) % len(self.cores_pulso)
        
        # Velocidade da pulsação (150ms para suavidade)
        self._registrar_after(150, self.animar_pulso)

    def destroy(self):
        if hasattr(self, "_after_ids"):
            self.cancelar_loops()
        super().destroy()

    def abrir_detalhes_ia(self):
        """Abre uma janela modal com os alertas detalhados."""
        if not self.alertas_pendentes:
            messagebox.showinfo("IA FRS", "Tudo sob controle! Nenhum problema detectado no estoque.")
            return

        janela_ia = ctk.CTkToplevel(self)
        janela_ia.title("Relatório de Inteligência FRS")
        janela_ia.geometry("500x400")
        janela_ia.grab_set()

        ctk.CTkLabel(janela_ia, text="Alertas da Inteligência", font=("Arial", 18, "bold")).pack(pady=15)
        
        scroll = ctk.CTkScrollableFrame(janela_ia, width=450, height=280)
        scroll.pack(padx=20, pady=10, fill="both", expand=True)

        for alerta in self.alertas_pendentes:
            cor_tipo = "#FF5555" if "Validade" in alerta['tipo'] else "#FFAA00"
            item_frame = ctk.CTkFrame(scroll)
            item_frame.pack(fill="x", pady=5)
            
            ctk.CTkLabel(item_frame, text=alerta['tipo'], text_color=cor_tipo, font=("Arial", 11, "bold")).pack(side="left", padx=10)
            ctk.CTkLabel(item_frame, text=f"{alerta['produto']} ({alerta['detalhe']})", wraplength=250).pack(side="left", padx=10)

        ctk.CTkButton(janela_ia, text="Entendido", command=janela_ia.destroy).pack(pady=15)

    def checar_mentoria_ia(self, origem="manual"):
        """Se o usuário for admin e passaram 15 dias, exibe o relatório da mentora."""
        if not self.sistema_pronto:
            return

        # Revalida estado no banco para impedir mentoria em base zerada.
        self.admin_cadastrado = self._verificar_admin_cadastrado()
        if not self.admin_cadastrado:
            print("[MENTORA] Ignorada: administrador ainda não cadastrado no banco.")
            return

        if self.usuario_atual.get("permissao") != "Administrador":
            return

        if not self._pode_exibir_mentora_hoje():
            print("[MENTORA] Ignorada: já exibida nesta sessão ou nas últimas 24h.")
            return

        try:
            self.exibir_relatorio_mentoria()
            self._registrar_exibicao_mentora()
            self._poll_venda_ativa = False
            print(f"[MENTORA] Exibida com sucesso. Origem: {origem}")
        except Exception as e:
            print(f"[ERRO IA] Falha ao checar mentoria: {e}")

    def pop_up_mentora(self):
        """Compatibilidade com gatilhos antigos: obedece o mesmo bloqueio da mentoria."""
        self.checar_mentoria_ia()

    def exibir_relatorio_mentoria(self):
        """Gera e exibe a janela de mentoria consultiva."""
        if not self.sistema_pronto or not self.admin_cadastrado:
            return

        try:
            ia_gestao = self._get_ia_gestao()
            conselho = ia_gestao.analisar_performance_15_dias()
        except Exception as e:
            messagebox.showwarning("Mentoria", "Não foi possível gerar o relatório de mentoria agora.")
            print(f"[ERRO IA] Falha ao gerar relatório de mentoria: {e}")
            return
        
        janela_m = ctk.CTkToplevel(self)
        janela_m.title("IA Mentora FRS - Consultoria Estratégica")
        janela_m.geometry("600x500")
        janela_m.transient(self)
        janela_m.protocol("WM_DELETE_WINDOW", janela_m.destroy)
        
        ctk.CTkLabel(janela_m, text="💡 IA Mentora FRS", font=("Roboto", 22, "bold"), text_color="#1da1f2").pack(pady=20)
        
        txt_box = ctk.CTkTextbox(janela_m, width=550, height=350, font=("Arial", 13))
        txt_box.pack(padx=20, pady=10)
        txt_box.insert("0.0", conselho)
        txt_box.configure(state="disabled") # Somente leitura
        
        btn = ctk.CTkButton(janela_m, text="Entendi", command=janela_m.destroy)
        btn.pack(pady=15)

def iniciar_sistema(usuario=None):
    """Função centralizadora para iniciar a aplicação."""
    app = AppPrincipal(usuario_sessao=usuario)
    try:
        app.deiconify()
        app.focus_force()
    except Exception:
        pass
    app.mainloop()

if __name__ == "__main__":
    iniciar_sistema()