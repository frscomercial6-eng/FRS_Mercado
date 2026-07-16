import json
import os
import pickle
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib import error, request

import customtkinter as ctk

from app_paths import obter_caminho_dados
from app_config import AUTO_UPDATE_REPO
from client_credentials_store import load_client_credentials
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero
from webhook_security import salvar_token_webhook, webhook_token_configurado

# Arquivo de configuração em diretório gravável do usuário
CONFIG_FILE = obter_caminho_dados("config.json")
FISCAL_ERROR_LOG = obter_caminho_dados("logs", "fiscal_config_error.log")


def _path_em_program_files(caminho: str) -> bool:
    caminho_lower = (caminho or "").lower()
    pf = (os.environ.get("ProgramFiles") or "").lower()
    pfx86 = (os.environ.get("ProgramFiles(x86)") or "").lower()
    return bool((pf and caminho_lower.startswith(pf)) or (pfx86 and caminho_lower.startswith(pfx86)))


def _registrar_erro_fiscal(mensagem: str):
    """Registra falhas fiscais em arquivo no APPDATA para diagnóstico seguro."""
    try:
        os.makedirs(os.path.dirname(FISCAL_ERROR_LOG), exist_ok=True)
        with open(FISCAL_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {mensagem}\n")
    except Exception:
        pass


def _garantir_pastas_usuario():
    """Cria pasta de trabalho e subpastas padrão em APPDATA na primeira execução."""
    os.makedirs(obter_caminho_dados("exportacao_fiscal"), exist_ok=True)
    os.makedirs(obter_caminho_dados("fiscal_in"), exist_ok=True)
    os.makedirs(obter_caminho_dados("fiscal_out"), exist_ok=True)


def _config_padrao():
    return {
        "razao_social": "",
        "nome_estabelecimento": "",
        "market_id": "",
        "fiscal_ativo": False,
        "auto_update_enabled": True,
        "auto_update_repo": (AUTO_UPDATE_REPO or "").strip(),
        "auto_update_remind_hours": 24,
        "app_executable_path": "",
        "update_executable_path": "",
        "cnpj": "",
        "email_cliente": "",
        "drive_credentials_path": "",
        "drive_backup_folder_id": "",
        "emissor_fiscal_path": "",
        "pasta_entrada_fiscal": obter_caminho_dados("fiscal_in"),
        "pasta_retorno_fiscal": obter_caminho_dados("fiscal_out"),
        "pasta_exportacao_fiscal": obter_caminho_dados("exportacao_fiscal"),
        "limite_sangria_preventiva": 500.0,
    }


def _resolver_auto_update_repo(repo_salvo):
    repo_fixo = (AUTO_UPDATE_REPO or "").strip()
    if repo_fixo:
        return repo_fixo
    return str(repo_salvo or "").strip()


def _parse_valor_monetario(valor, default=500.0):
    try:
        return parse_numero(valor, "Valor", permitir_vazio=True, default=default, minimo=0)
    except Exception:
        return float(default)


def _normalizar_config(dados):
    """Migra caminhos inseguros para APPDATA e garante diretórios de escrita."""
    cfg = _config_padrao()
    if isinstance(dados, dict):
        cfg.update(dados)

    cfg["auto_update_repo"] = _resolver_auto_update_repo(cfg.get("auto_update_repo"))

    mapa_fiscal = {
        "pasta_entrada_fiscal": "fiscal_in",
        "pasta_retorno_fiscal": "fiscal_out",
        "pasta_exportacao_fiscal": "exportacao_fiscal",
    }

    for chave, subpasta in mapa_fiscal.items():
        atual = str(cfg.get(chave, "") or "").strip()
        if not atual or not os.path.isabs(atual) or _path_em_program_files(atual):
            atual = obter_caminho_dados(subpasta)
        cfg[chave] = atual
        os.makedirs(atual, exist_ok=True)

    cfg["limite_sangria_preventiva"] = _parse_valor_monetario(cfg.get("limite_sangria_preventiva", 500.0))

    return cfg


def carregar_configuracoes():
    """Carrega as configurações do arquivo JSON ou retorna valores padrão."""
    _garantir_pastas_usuario()

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            config = _normalizar_config(dados)
            if config != dados:
                with open(CONFIG_FILE, "w", encoding="utf-8") as fw:
                    json.dump(config, fw, indent=4, ensure_ascii=False)
            return config
        except Exception as e:
            print(f"Erro ao carregar configurações: {e}")

    config = _config_padrao()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception:
        pass
    return config


def salvar_configuracoes(dados, exibir_alerta=True):
    """Salva o dicionário de configurações no arquivo JSON."""
    try:
        dados = _normalizar_config(dados)
        salvar_limite_sangria_preventiva(dados.get("limite_sangria_preventiva", 500.0), exibir_alerta=False)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=4, ensure_ascii=False)
        if exibir_alerta:
            messagebox.showinfo("Sucesso", "Configurações salvas com sucesso!")
        return True, "Configurações gerais salvas."
    except Exception as e:
        if exibir_alerta:
            messagebox.showerror("Erro", f"Erro ao salvar configurações: {e}")
        return False, f"Erro ao salvar configurações gerais: {e}"


def carregar_credenciais_google():
    """
    Localiza e valida os caminhos absolutos dos arquivos de credenciais do Google.
    Garante que a integração encontre as chaves independente da localização da pasta.
    """
    import sys

    diretorio_base = Path(__file__).parent.resolve()
    diretorio_exe = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else diretorio_base

    credenciais_seguras = load_client_credentials()

    valor_creds_seguro = str(
        credenciais_seguras.get("google_oauth_credentials_path")
        or credenciais_seguras.get("drive_credentials_path")
        or ""
    ).strip()
    valor_services_seguro = str(credenciais_seguras.get("google_services_path") or "").strip()
    caminho_creds_seguro = Path(valor_creds_seguro) if valor_creds_seguro else None
    caminho_services_seguro = Path(valor_services_seguro) if valor_services_seguro else None

    caminho_creds = diretorio_exe / "credentials.json"
    caminho_services = diretorio_exe / "google-services.json"

    if caminho_creds_seguro and caminho_creds_seguro.exists():
        caminho_creds = caminho_creds_seguro
    if caminho_services_seguro and caminho_services_seguro.exists():
        caminho_services = caminho_services_seguro

    if not caminho_creds.exists():
        caminho_creds = diretorio_base / "credentials.json"
    if not caminho_services.exists():
        caminho_services = diretorio_base / "google-services.json"

    appdata_creds = Path(obter_caminho_dados("credentials.json"))
    appdata_services = Path(obter_caminho_dados("google-services.json"))
    if appdata_creds.exists():
        caminho_creds = appdata_creds
    if appdata_services.exists():
        caminho_services = appdata_services

    credenciais = {
        "credentials": str(caminho_creds) if caminho_creds.exists() else None,
        "google_services": str(caminho_services) if caminho_services.exists() else None,
    }

    if not credenciais["credentials"] or not credenciais["google_services"]:
        from database_manager import registrar_log

        msg = "Integração Google indisponível: Arquivos 'credentials.json' ou 'google-services.json' não encontrados."
        registrar_log(None, "Segurança/Config", "Falha", msg)
        print(f"Erro de Integração: {msg}")

    return credenciais


def obter_status_backup_local():
    """Valida somente presença/integridade local do token, sem chamadas de rede."""
    token_path = Path(obter_caminho_dados("token.pickle"))
    if not token_path.exists():
        return {
            "configurado": False,
            "mensagem": "Backup não configurado: token OAuth2 ausente.",
            "cor": "#ff6666",
        }

    try:
        with token_path.open("rb") as f:
            pickle.load(f)
    except Exception:
        return {
            "configurado": False,
            "mensagem": "Backup não configurado: token OAuth2 corrompido.",
            "cor": "#ff6666",
        }

    return {
        "configurado": True,
        "mensagem": "Backup configurado: token OAuth2 local disponível.",
        "cor": "#2ecc71",
    }


def provisionar_novo_cliente(email_cliente):
    """Provisiona estrutura inicial no Drive do cliente durante setup em Configurações."""
    from modulo_relatorio import ModuloRelatorio

    resultado = ModuloRelatorio.provisionar_novo_cliente(email_cliente)
    return resultado


def _garantir_tabela_config_fiscal(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_fiscal (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            api_key TEXT NOT NULL DEFAULT '',
            ambiente TEXT NOT NULL DEFAULT 'HOMOLOGACAO',
            webhook_token_hash TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(config_fiscal)").fetchall()]
    if "webhook_token_hash" not in cols:
        conn.execute("ALTER TABLE config_fiscal ADD COLUMN webhook_token_hash TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "INSERT OR IGNORE INTO config_fiscal (id, api_key, ambiente, webhook_token_hash) VALUES (1, '', 'HOMOLOGACAO', '')"
    )


def carregar_config_fiscal():
    """Lê configuração fiscal da tabela config_fiscal."""
    from database_manager import get_db_connection

    try:
        with get_db_connection() as conn:
            _garantir_tabela_config_fiscal(conn)
            row = conn.execute(
                "SELECT api_key, ambiente, webhook_token_hash FROM config_fiscal WHERE id = 1"
            ).fetchone()
            if row:
                return {
                    "api_key": row[0] or "",
                    "ambiente": (row[1] or "HOMOLOGACAO").upper(),
                    "webhook_token_configurado": bool(row[2]),
                }
    except Exception as e:
        _registrar_erro_fiscal(f"Falha ao carregar config_fiscal: {e}")

    return {"api_key": "", "ambiente": "HOMOLOGACAO", "webhook_token_configurado": webhook_token_configurado()}


def salvar_config_fiscal(api_key: str, ambiente: str, webhook_token: str | None = None):
    """Salva configuração fiscal na tabela config_fiscal."""
    from database_manager import get_db_connection

    ambiente_norm = "PRODUCAO" if str(ambiente).upper() == "PRODUCAO" else "HOMOLOGACAO"

    with get_db_connection() as conn:
        _garantir_tabela_config_fiscal(conn)
        conn.execute(
            """
            INSERT INTO config_fiscal (id, api_key, ambiente)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET api_key = excluded.api_key, ambiente = excluded.ambiente
            """,
            ((api_key or "").strip(), ambiente_norm),
        )

    if webhook_token is not None and str(webhook_token).strip():
        salvar_token_webhook(str(webhook_token).strip())


def obter_limite_sangria_preventiva(default=500.0):
    """Retorna o limite de caixa atual usando banco como fonte primária."""
    try:
        from database_manager import get_db_connection

        with get_db_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS config_sistema (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO config_sistema (chave, valor) VALUES ('limite_caixa', ?)",
                (f"{float(default):.2f}",),
            )
            row = conn.execute("SELECT valor FROM config_sistema WHERE chave = 'limite_caixa'").fetchone()
            if row and row[0] is not None:
                return _parse_valor_monetario(row[0], default=default)
    except Exception as e:
        _registrar_erro_fiscal(f"Falha ao obter limite de sangria: {e}")

    config = carregar_configuracoes()
    return _parse_valor_monetario(config.get("limite_sangria_preventiva", default), default=default)


def salvar_limite_sangria_preventiva(valor, exibir_alerta=True):
    """Atualiza o limite de caixa no banco e no JSON de configuração."""
    valor_float = _parse_valor_monetario(valor)

    from database_manager import get_db_connection

    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config_sistema (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO config_sistema (chave, valor) VALUES ('limite_caixa', ?)
            ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
            """,
            (f"{valor_float:.2f}",),
        )

    dados = carregar_configuracoes()
    dados["limite_sangria_preventiva"] = valor_float
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_normalizar_config(dados), f, indent=4, ensure_ascii=False)

    if exibir_alerta:
        messagebox.showinfo("Sucesso", "Limite de sangria preventiva atualizado.")
    return valor_float


def testar_conexao_plugnotas(api_key: str, ambiente: str):
    """Valida token PlugNotas com requisição simples de leitura."""
    token = (api_key or "").strip()
    if not token:
        return False, "Informe a API Key para testar conexão."

    ambiente_norm = "PRODUCAO" if str(ambiente).upper() == "PRODUCAO" else "HOMOLOGACAO"
    base_url = "https://api.plugnotas.com.br"
    endpoint = f"{base_url}/v1/nfce?pagina=1&tamanho=1"

    headers = {
        "x-api-key": token,
        "Accept": "application/json",
        "User-Agent": "FRS-Mercado/1.0",
    }

    req = request.Request(endpoint, headers=headers, method="GET")

    try:
        with request.urlopen(req, timeout=12) as resp:
            if 200 <= resp.status < 300:
                return True, f"Conexão OK ({ambiente_norm}). Token válido."
            return False, f"Conexão recebida com status inesperado: {resp.status}."
    except error.HTTPError as http_err:
        if http_err.code in (401, 403):
            return False, "Token inválido ou sem permissão para este ambiente."
        if http_err.code in (404, 405):
            return False, "Endpoint PlugNotas indisponível no momento. Tente novamente."
        return False, f"Falha HTTP ao validar conexão: {http_err.code}."
    except Exception as e:
        _registrar_erro_fiscal(f"Erro no teste de conexão PlugNotas ({ambiente_norm}): {e}")
        return False, f"Erro de conexão com PlugNotas: {e}"


def exibir_configuracoes(master=None):
    """Abre a janela de configurações com abas Geral e Fiscal."""
    if master is not None and not getattr(master, "usuario_atual", None):
        messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar configurações.")
        return None

    config_atual = carregar_configuracoes()
    fiscal_atual = carregar_config_fiscal()

    janela_config = ctk.CTkToplevel(master) if master is not None else ctk.CTkToplevel()
    janela_config.title("Configurações do Sistema")
    janela_config.geometry("740x620")
    janela_config.grab_set()
    janela_config.focus_force()

    ctk.CTkLabel(janela_config, text="Configurações Globais", font=("Arial", 20, "bold")).pack(pady=12)

    status_label = ctk.CTkLabel(janela_config, text="", text_color="#2ecc71", font=("Arial", 11, "bold"))
    status_label.pack(pady=(0, 6))

    tabview = ctk.CTkTabview(janela_config, width=680, height=470)
    tabview.pack(padx=20, pady=8, fill="both", expand=True)
    tabview.add("Geral")
    tabview.add("Fiscal")

    frame_geral = tabview.tab("Geral")
    frame_fiscal = tabview.tab("Fiscal")

    # --- ABA GERAL ---
    ctk.CTkLabel(frame_geral, text="Razão Social:").grid(row=0, column=0, padx=10, pady=6, sticky="e")
    entry_razao = ctk.CTkEntry(frame_geral, width=360)
    entry_razao.insert(0, config_atual.get("razao_social", ""))
    entry_razao.grid(row=0, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="CNPJ:").grid(row=1, column=0, padx=10, pady=6, sticky="e")
    entry_cnpj = ctk.CTkEntry(frame_geral, width=360)
    entry_cnpj.insert(0, config_atual.get("cnpj", ""))
    entry_cnpj.grid(row=1, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="E-mail do Cliente:").grid(row=2, column=0, padx=10, pady=6, sticky="e")
    entry_email_cliente = ctk.CTkEntry(frame_geral, width=360)
    entry_email_cliente.insert(0, config_atual.get("email_cliente", ""))
    entry_email_cliente.grid(row=2, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Emissor Fiscal (.exe):").grid(row=3, column=0, padx=10, pady=6, sticky="e")
    entry_emissor = ctk.CTkEntry(frame_geral, width=270)
    entry_emissor.insert(0, config_atual.get("emissor_fiscal_path", ""))
    entry_emissor.grid(row=3, column=1, padx=(10, 0), pady=6, sticky="w")

    fiscal_ativo_var = ctk.BooleanVar(value=bool(config_atual.get("fiscal_ativo", False)))
    check_fiscal_ativo = ctk.CTkCheckBox(
        frame_geral,
        text="ACBrMonitor (Emissão Fiscal) Ativo",
        variable=fiscal_ativo_var,
        onvalue=True,
        offvalue=False,
    )
    check_fiscal_ativo.grid(row=11, column=0, columnspan=2, padx=10, pady=(2, 8), sticky="w")

    ctk.CTkLabel(frame_geral, text="Google Drive (Credenciais):").grid(row=4, column=0, padx=10, pady=6, sticky="e")
    entry_drive_creds = ctk.CTkEntry(frame_geral, width=360)
    entry_drive_creds.insert(0, config_atual.get("drive_credentials_path", ""))
    entry_drive_creds.grid(row=4, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="ID Pasta Backup:").grid(row=5, column=0, padx=10, pady=6, sticky="e")
    entry_drive_id = ctk.CTkEntry(frame_geral, width=360)
    entry_drive_id.insert(0, config_atual.get("drive_backup_folder_id", ""))
    entry_drive_id.grid(row=5, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Pasta Entrada Fiscal:").grid(row=6, column=0, padx=10, pady=6, sticky="e")
    entry_in = ctk.CTkEntry(frame_geral, width=270)
    entry_in.insert(0, config_atual.get("pasta_entrada_fiscal", ""))
    entry_in.grid(row=6, column=1, padx=(10, 0), pady=6, sticky="w")

    ctk.CTkLabel(frame_geral, text="Pasta Retorno Fiscal:").grid(row=7, column=0, padx=10, pady=6, sticky="e")
    entry_out = ctk.CTkEntry(frame_geral, width=270)
    entry_out.insert(0, config_atual.get("pasta_retorno_fiscal", ""))
    entry_out.grid(row=7, column=1, padx=(10, 0), pady=6, sticky="w")

    ctk.CTkLabel(frame_geral, text="Limite de Sangria Preventiva:").grid(row=8, column=0, padx=10, pady=6, sticky="e")
    entry_limite = ctk.CTkEntry(frame_geral, width=360)
    entry_limite.insert(0, f"{obter_limite_sangria_preventiva():.2f}")
    entry_limite.grid(row=8, column=1, padx=10, pady=6)
    aplicar_padrao_entrada_numerica(entry_limite, inteiro=False, casas_decimais=2)

    lbl_status_backup_drive = ctk.CTkLabel(frame_geral, text="", font=("Arial", 11, "bold"))
    lbl_status_backup_drive.grid(row=9, column=0, columnspan=2, padx=10, pady=(8, 4), sticky="w")

    def atualizar_alerta_backup_drive():
        status = obter_status_backup_local()
        lbl_status_backup_drive.configure(text=status["mensagem"], text_color=status["cor"])

    def acao_provisionar_cliente_drive():
        email_cliente = entry_email_cliente.get().strip()
        if not email_cliente:
            status_label.configure(text="Informe o e-mail do cliente para provisionar o Drive.", text_color="#ff6666")
            return
        try:
            provisionar_novo_cliente(email_cliente)
            atualizar_alerta_backup_drive()
            status_label.configure(text="Provisionamento do cliente no Drive concluído.", text_color="#2ecc71")
        except Exception as e:
            status_label.configure(text=f"Falha no provisionamento Drive: {e}", text_color="#ff6666")

    ctk.CTkButton(
        frame_geral,
        text="Provisionar Cliente no Drive",
        fg_color="#1f6aa5",
        hover_color="#144870",
        command=acao_provisionar_cliente_drive,
    ).grid(row=10, column=0, columnspan=2, padx=10, pady=(4, 8), sticky="w")

    def procurar_exe(entry_widget):
        caminho = filedialog.askopenfilename(
            title="Selecionar Executável",
            filetypes=[("Executáveis", "*.exe"), ("Todos os arquivos", "*.*")],
        )
        if caminho:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, caminho)
            janela_config.focus_force()

    def selecionar_pasta(entry_widget):
        caminho = filedialog.askdirectory()
        if caminho:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, caminho)
            janela_config.focus_force()

    ctk.CTkButton(frame_geral, text="...", width=40, command=lambda: procurar_exe(entry_emissor)).grid(row=3, column=1, padx=(0, 10), pady=6, sticky="e")
    ctk.CTkButton(frame_geral, text="...", width=40, command=lambda: selecionar_pasta(entry_in)).grid(row=6, column=1, padx=(0, 10), pady=6, sticky="e")
    ctk.CTkButton(frame_geral, text="...", width=40, command=lambda: selecionar_pasta(entry_out)).grid(row=7, column=1, padx=(0, 10), pady=6, sticky="e")

    # --- ABA FISCAL (PlugNotas) ---
    ctk.CTkLabel(frame_fiscal, text="Integração Fiscal PlugNotas (API v2.4.2)", font=("Arial", 16, "bold")).pack(pady=(12, 14))

    form_fiscal = ctk.CTkFrame(frame_fiscal)
    form_fiscal.pack(fill="x", padx=14, pady=8)

    ctk.CTkLabel(form_fiscal, text="API Key:").grid(row=0, column=0, padx=10, pady=10, sticky="e")
    entry_api_key = ctk.CTkEntry(form_fiscal, width=440)
    entry_api_key.insert(0, fiscal_atual.get("api_key", ""))
    entry_api_key.grid(row=0, column=1, padx=10, pady=10)

    ctk.CTkLabel(form_fiscal, text="Webhook Token:").grid(row=1, column=0, padx=10, pady=10, sticky="e")
    entry_webhook_token = ctk.CTkEntry(form_fiscal, width=440, show="*")
    entry_webhook_token.grid(row=1, column=1, padx=10, pady=10)
    ctk.CTkLabel(
        form_fiscal,
        text=(
            "Status token: CONFIGURADO" if fiscal_atual.get("webhook_token_configurado") else "Status token: NÃO CONFIGURADO"
        ),
        text_color="#2ecc71" if fiscal_atual.get("webhook_token_configurado") else "#ff6666",
        font=("Arial", 10, "bold"),
    ).grid(row=2, column=1, padx=10, pady=(0, 8), sticky="w")

    ctk.CTkLabel(form_fiscal, text="Ambiente:").grid(row=3, column=0, padx=10, pady=10, sticky="e")
    ambiente_var = ctk.StringVar(value="PRODUCAO" if fiscal_atual.get("ambiente") == "PRODUCAO" else "HOMOLOGACAO")
    switch_ambiente = ctk.CTkSwitch(
        form_fiscal,
        text="Homologação",
        onvalue="PRODUCAO",
        offvalue="HOMOLOGACAO",
        variable=ambiente_var,
    )
    if ambiente_var.get() == "PRODUCAO":
        switch_ambiente.select()
        switch_ambiente.configure(text="Produção")
    else:
        switch_ambiente.deselect()
        switch_ambiente.configure(text="Homologação")
    switch_ambiente.grid(row=3, column=1, padx=10, pady=10, sticky="w")

    def atualizar_texto_switch():
        switch_ambiente.configure(text="Produção" if ambiente_var.get() == "PRODUCAO" else "Homologação")

    switch_ambiente.configure(command=atualizar_texto_switch)

    lbl_teste = ctk.CTkLabel(frame_fiscal, text="", font=("Arial", 11, "bold"), text_color="#3498db")
    lbl_teste.pack(pady=(6, 8))

    def acao_testar_conexao():
        ok, msg = testar_conexao_plugnotas(entry_api_key.get(), ambiente_var.get())
        lbl_teste.configure(text=msg, text_color="#2ecc71" if ok else "#ff6666")
        janela_config.focus_force()
        entry_api_key.focus_set()

    ctk.CTkButton(
        frame_fiscal,
        text="TESTAR CONEXÃO",
        fg_color="#1f6aa5",
        hover_color="#144870",
        command=acao_testar_conexao,
    ).pack(pady=(4, 10))

    # --- Ações ---
    def acao_salvar():
        try:
            if not bool(fiscal_ativo_var.get()):
                confirmar_desativacao = messagebox.askyesno(
                    "ATENÇÃO",
                    (
                        "ATENÇÃO: Você está desativando o componente de emissão fiscal. "
                        "Caso esta opção seja desmarcada, não será possível emitir Nota Fiscal "
                        "nem realizar a busca de XML no banco de dados. Deseja realmente prosseguir?"
                    ),
                    parent=janela_config,
                )
                if not confirmar_desativacao:
                    fiscal_ativo_var.set(True)
                    status_label.configure(
                        text="Desativação do ACBrMonitor cancelada pelo usuário.",
                        text_color="#ff5555",
                    )
                    return

            novos_dados = {
                "razao_social": entry_razao.get(),
                "nome_estabelecimento": entry_razao.get(),
                "cnpj": entry_cnpj.get(),
                "email_cliente": entry_email_cliente.get(),
                "drive_credentials_path": entry_drive_creds.get(),
                "drive_backup_folder_id": entry_drive_id.get(),
                "emissor_fiscal_path": entry_emissor.get(),
                "fiscal_ativo": bool(fiscal_ativo_var.get()),
                "pasta_entrada_fiscal": entry_in.get(),
                "pasta_retorno_fiscal": entry_out.get(),
                "limite_sangria_preventiva": entry_limite.get(),
            }

            ok_geral, msg_geral = salvar_configuracoes(novos_dados, exibir_alerta=False)
            if not ok_geral:
                status_label.configure(text=msg_geral, text_color="#ff6666")
                janela_config.focus_force()
                return

            token_digitado = entry_webhook_token.get().strip()
            salvar_config_fiscal(
                entry_api_key.get(),
                ambiente_var.get(),
                webhook_token=token_digitado if token_digitado else None,
            )
            status_label.configure(text="Configurações gerais e fiscais salvas com sucesso.", text_color="#2ecc71")
            atualizar_alerta_backup_drive()
            janela_config.focus_force()
            tabview.set("Geral")
            entry_razao.focus_set()
        except Exception as e:
            _registrar_erro_fiscal(f"Erro ao salvar configurações fiscais: {e}")
            status_label.configure(text=f"Falha ao salvar configurações: {e}", text_color="#ff6666")
            janela_config.focus_force()

    botoes = ctk.CTkFrame(janela_config, fg_color="transparent")
    botoes.pack(pady=(8, 14))

    ctk.CTkButton(
        botoes,
        text="SALVAR CONFIGURAÇÕES",
        fg_color="green",
        hover_color="darkgreen",
        width=240,
        command=acao_salvar,
    ).pack(side="left", padx=8)

    ctk.CTkButton(
        botoes,
        text="FECHAR",
        fg_color="#666666",
        hover_color="#4f4f4f",
        width=140,
        command=lambda: janela_config.destroy(),
    ).pack(side="left", padx=8)

    janela_config.protocol("WM_DELETE_WINDOW", janela_config.destroy)
    janela_config.focus_force()
    atualizar_alerta_backup_drive()
    entry_razao.focus_set()

    return janela_config


if __name__ == "__main__":
    root = ctk.CTk()
    root.title("Teste do Módulo")
    root.geometry("220x120")

    btn_abrir = ctk.CTkButton(root, text="Abrir Configurações", command=exibir_configuracoes)
    btn_abrir.pack(expand=True)

    root.mainloop()
