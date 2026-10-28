import json
import os
import pickle
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

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


def _registrar_acesso_drive(mensagem: str):
    """Registra tentativa de acesso ao Drive sem abrir o banco do cliente na UI.

    Import local: modulo_config e importado por fluxos headless e nao deve
    arrastar database_manager na importacao do modulo.
    """
    try:
        from database_manager import registrar_log

        registrar_log(None, "Backup Drive", "Aviso", mensagem)
    except Exception:
        _registrar_erro_fiscal(f"[Drive] {mensagem}")


def _garantir_pastas_usuario():
    """Cria pasta de trabalho e subpastas padrão em APPDATA na primeira execução."""
    os.makedirs(obter_caminho_dados("exportacao_fiscal"), exist_ok=True)
    os.makedirs(obter_caminho_dados("fiscal_in"), exist_ok=True)
    os.makedirs(obter_caminho_dados("fiscal_out"), exist_ok=True)


def _config_padrao():
    return {
        "nome_fantasia": "",
        "razao_social": "",
        "nome_estabelecimento": "",
        "cnpj": "",
        "cep": "",
        "logradouro": "",
        "numero": "",
        "complemento": "",
        "bairro": "",
        "cidade": "",
        "uf": "",
        "fiscal_ativo": False,
        "auto_update_enabled": True,
        "auto_update_repo": (AUTO_UPDATE_REPO or "").strip(),
        "auto_update_remind_hours": 24,
        "app_executable_path": "",
        "update_executable_path": "",
        "email_cliente": "",
    # E-mail utilizado para backup do Google Drive (mantido para referência)
    # Campo de credenciais removido - localização automática
        "google_oauth_credentials_path": "client_secret.json",
        "drive_credentials_path": "",
    # Campo removido da interface - credenciais localizadas automaticamente
        "drive_backup_folder_id": "",
    "google_drive_conta": "",
    # Campo removido da interface: credenciais do Google são localizadas automaticamente
    # sem necessidade de o usuário fornecer caminhos manuais.
        "emissor_fiscal_path": "",
        "pasta_entrada_fiscal": obter_caminho_dados("fiscal_in"),
        "pasta_retorno_fiscal": obter_caminho_dados("fiscal_out"),
        "pasta_exportacao_fiscal": obter_caminho_dados("exportacao_fiscal"),
        "limite_sangria_preventiva": 500.0,
        # Largura da bobina térmica: 58 mm (32 caracteres) ou 80 mm (48).
        "largura_cupom_mm": 80,
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

    # Compat: campo legado "endereco" (texto livre) não é mais usado pela
    # impressão; os campos estruturados prevalecem. Se existirem valores
    # novos vazios mas houver legado preenchido, expõe o legado como
    # logradouro somente para visualização (sem duplicar impressão).
    try:
        legado = str((dados or {}).get("endereco") or "").strip()
    except Exception:
        legado = ""
    if legado and not str(cfg.get("logradouro") or "").strip():
        cfg["logradouro"] = legado

    # Compat: nome_estabelecimento antigo alimenta nome_fantasia vazio.
    try:
        if not str(cfg.get("nome_fantasia") or "").strip():
            antigo = str((dados or {}).get("nome_estabelecimento") or "").strip()
            if antigo:
                cfg["nome_fantasia"] = antigo
    except Exception:
        pass

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


class ConfiguracaoInvalidaError(RuntimeError):
    """config.json existe no diretório de dados, mas não pôde ser lido.

    O arquivo original é PRESERVADO: a falha é fechada de propósito, para que
    uma configuração padrão nunca seja escrita por cima dos dados do cliente.
    Subclasse de RuntimeError para não alterar o contrato dos chamadores.
    """

    def __init__(self, caminho: str):
        super().__init__(f"config.json existente é inválido e foi preservado: {caminho}")
        self.caminho = caminho


def carregar_configuracoes():
    """Carrega as configurações do arquivo JSON ou retorna valores padrão."""
    _garantir_pastas_usuario()

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if not isinstance(dados, dict):
                raise ValueError("config.json deve conter um objeto JSON")
            config = _normalizar_config(dados)
            if config != dados:
                with open(CONFIG_FILE, "w", encoding="utf-8") as fw:
                    json.dump(config, fw, indent=4, ensure_ascii=False)
            return config
        except Exception as e:
            print(f"Erro ao carregar configurações: {e}")
            raise ConfiguracaoInvalidaError(CONFIG_FILE) from e

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

    from modulo_config import carregar_configuracoes

    configuracao = carregar_configuracoes() or {}
    diretorio_base = Path(__file__).parent.resolve()
    diretorio_exe = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else diretorio_base

    # Usar caminho configurado do novo cliente OAuth se disponível
    caminho_oauth = configuracao.get("google_oauth_credentials_path")
    if caminho_oauth:
        # Caminho relativo ao projeto, não ao diretório de trabalho atual
        caminho_projeto = Path(__file__).parent.resolve() / caminho_oauth
        if caminho_projeto.exists():
            # Usar o client_secret.json configurado, sem processamento complexo
            try:
                import json
                with open(Path(__file__).parent.resolve() / caminho_oauth, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if 'installed' in data or 'web' in data:
                    credenciais_seguras = {
                        "credentials": str(Path(__file__).parent.resolve() / caminho_oauth),
                        "google_services": str(Path(__file__).parent.resolve() / caminho_oauth.with_name("google-services.json"))
                    }
                else:
                    credenciais_seguras = load_client_credentials()
            except Exception as e:
                credenciais_seguras = load_client_credentials()
        else:
            credenciais_seguras = load_client_credentials()
    else:
        credenciais_seguras = load_client_credentials()

    valor_creds_seguro = str(
        credenciais_seguras.get("credentials")
        or credenciais_seguras.get("drive_credentials_path")
        or ""
    ).strip()
    valor_services_seguro = str(credenciais_seguras.get("google_services") or credenciais_seguras.get("google_services_path") or "").strip()
    caminho_creds_seguro = Path(valor_creds_seguro) if valor_creds_seguro else None
    caminho_services_seguro = Path(valor_services_seguro) if valor_services_seguro else None

    caminho_creds = diretorio_exe / "credentials.json"
    caminho_services = diretorio_exe / "google-services.json"

    if caminho_creds_seguro and caminho_creds_seguro.exists():
        caminho_creds = caminho_creds_seguro
    if caminho_services_seguro and caminho_services_seguro.exists():
        caminho_services = caminho_services_seguro

    # Priorizar caminhos configurados (do novo cliente OAuth) antes de cair back para defaults
    if caminho_creds_seguro and caminho_creds_seguro.exists():
        caminho_creds = caminho_creds_seguro
    elif not caminho_creds.exists():
        caminho_creds = diretorio_base / "credentials.json"
    
    if caminho_services_seguro and caminho_services_seguro.exists():
        caminho_services = caminho_services_seguro
    elif not caminho_services.exists():
        caminho_services = diretorio_base / "google-services.json"
    
    # Apenas usar AppData se os caminhos configurados nÃ£o existirem
    appdata_creds = Path(obter_caminho_dados("credentials.json"))
    appdata_services = Path(obter_caminho_dados("google-services.json"))
    if appdata_creds.exists() and not caminho_creds_seguro:
        caminho_creds = appdata_creds
    if appdata_services.exists() and not caminho_services_seguro:
        caminho_services = appdata_services

    credenciais = {
        "credentials": str(caminho_creds) if caminho_creds.exists() else None,
        "google_services": str(caminho_services) if caminho_services.exists() else None,
    }

    if not credenciais["credentials"]:
        from database_manager import registrar_log

        msg = "Integração Google indisponível: Arquivo credentials.json não encontrado para OAuth."
        registrar_log(None, "Segurança/Config", "Falha", msg)
        print(f"Aviso de integracao Google: {msg}")
        return {}

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
                "SELECT api_key, ambiente FROM config_fiscal WHERE id = 1"
            ).fetchone()
            if row:
                return {
                    "api_key": row[0] or "",
                    "ambiente": (row[1] or "HOMOLOGACAO").upper(),
                    # O token do webhook tem persistência própria desde a
                    # desconexão do PlugNotas; perguntamos à fonte correta.
                    "webhook_token_configurado": webhook_token_configurado(),
                }
    except Exception as e:
        _registrar_erro_fiscal(f"Falha ao carregar config_fiscal: {e}")

    return {"api_key": "", "ambiente": "HOMOLOGACAO", "webhook_token_configurado": webhook_token_configurado()}


def salvar_config_fiscal(api_key: str = "", ambiente: str = "", webhook_token: str | None = None):
    """Mantém o schema legado e grava apenas o que ainda é usado.

    A integração PlugNotas foi removida: `api_key`/`ambiente` deixam de ser
    gravados, mas a tabela e suas colunas permanecem intactas para não quebrar
    instalações existentes. A função é preservada (assinatura e retorno) para
    compatibilidade com chamadores antigos.
    """
    from database_manager import get_db_connection

    with get_db_connection() as conn:
        # Garante a estrutura legada sem sobrescrever valores já existentes.
        _garantir_tabela_config_fiscal(conn)

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

    # LAYOUT (correção cirúrgica): o conteúdo de cada aba vai para um
    # CTkScrollableFrame — os campos ficam acessíveis por rolagem tanto em
    # janela normal quanto maximizada, a altura do tabview deixa de estourar
    # e os botões inferiores (principalmente SALVAR) permanecem visíveis.
    # Campos, textos e lógica de salvar/carregar: NÃO alterados.
    frame_geral = ctk.CTkScrollableFrame(tabview.tab("Geral"))
    frame_geral.pack(fill="both", expand=True)

    frame_fiscal = ctk.CTkScrollableFrame(tabview.tab("Fiscal"))
    frame_fiscal.pack(fill="both", expand=True)

    # --- SEÇÕES DA ABA GERAL (criadas antes dos campos para servir de pai) ---
    frame_cadastro = ctk.CTkFrame(frame_geral)
    frame_cadastro.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="ew")
    ctk.CTkLabel(
        frame_cadastro, text="🏢 CADASTRO DA EMPRESA",
        font=("Arial", 14, "bold"),
    ).grid(row=0, column=0, columnspan=2, padx=10, pady=(12, 8), sticky="w")

    frame_drive = ctk.CTkFrame(frame_geral)
    frame_drive.grid(row=1, column=0, columnspan=2, padx=8, pady=(12, 8), sticky="ew")
    ctk.CTkLabel(
        frame_drive, text="☁️ BACKUP E GOOGLE DRIVE",
        font=("Arial", 14, "bold"),
    ).grid(row=0, column=0, columnspan=2, padx=10, pady=(8, 4), sticky="w")

    # --- ABA GERAL ---
    ctk.CTkLabel(frame_cadastro, text="Nome Fantasia:").grid(row=1, column=0, padx=10, pady=6, sticky="e")
    entry_fantasia = ctk.CTkEntry(frame_cadastro, width=360)
    entry_fantasia.insert(0, config_atual.get("nome_fantasia", "") or config_atual.get("nome_estabelecimento", ""))
    entry_fantasia.grid(row=1, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_cadastro, text="Razão Social:").grid(row=2, column=0, padx=10, pady=6, sticky="e")
    entry_razao = ctk.CTkEntry(frame_cadastro, width=360)
    entry_razao.insert(0, config_atual.get("razao_social", ""))
    entry_razao.grid(row=2, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_cadastro, text="CNPJ:").grid(row=3, column=0, padx=10, pady=6, sticky="e")
    entry_cnpj = ctk.CTkEntry(frame_cadastro, width=360)
    entry_cnpj.insert(0, config_atual.get("cnpj", ""))
    entry_cnpj.grid(row=3, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_cadastro, text="Endereço — Logradouro:").grid(row=4, column=0, padx=10, pady=6, sticky="e")
    entry_logradouro = ctk.CTkEntry(frame_cadastro, width=280)
    entry_logradouro.insert(0, config_atual.get("logradouro", ""))
    entry_logradouro.grid(row=4, column=1, padx=(10, 0), pady=6, sticky="w")
    ctk.CTkLabel(frame_cadastro, text="Nº:").grid(row=4, column=1, padx=(0, 0), pady=6, sticky="e")
    entry_numero = ctk.CTkEntry(frame_cadastro, width=70)
    entry_numero.insert(0, config_atual.get("numero", ""))
    entry_numero.grid(row=4, column=1, padx=(0, 10), pady=6, sticky="e")

    ctk.CTkLabel(frame_cadastro, text="Complemento:").grid(row=5, column=0, padx=10, pady=6, sticky="e")
    entry_complemento = ctk.CTkEntry(frame_cadastro, width=360)
    entry_complemento.insert(0, config_atual.get("complemento", ""))
    entry_complemento.grid(row=5, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_cadastro, text="Bairro:").grid(row=6, column=0, padx=10, pady=6, sticky="e")
    entry_bairro = ctk.CTkEntry(frame_cadastro, width=360)
    entry_bairro.insert(0, config_atual.get("bairro", ""))
    entry_bairro.grid(row=6, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_cadastro, text="Cidade:").grid(row=7, column=0, padx=10, pady=6, sticky="e")
    entry_cidade = ctk.CTkEntry(frame_cadastro, width=220)
    entry_cidade.insert(0, config_atual.get("cidade", ""))
    entry_cidade.grid(row=7, column=1, padx=(10, 0), pady=6, sticky="w")
    ctk.CTkLabel(frame_cadastro, text="UF:").grid(row=7, column=1, padx=(0, 90), pady=6, sticky="e")
    entry_uf = ctk.CTkEntry(frame_cadastro, width=60)
    entry_uf.insert(0, config_atual.get("uf", ""))
    entry_uf.grid(row=7, column=1, padx=(0, 10), pady=6, sticky="e")

    ctk.CTkLabel(frame_cadastro, text="CEP:").grid(row=8, column=0, padx=10, pady=6, sticky="e")
    entry_cep = ctk.CTkEntry(frame_cadastro, width=360)
    entry_cep.insert(0, config_atual.get("cep", ""))
    entry_cep.grid(row=8, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_cadastro, text="E-mail da Empresa:").grid(row=9, column=0, padx=10, pady=6, sticky="e")
    entry_email_cliente = ctk.CTkEntry(frame_cadastro, width=360)
    entry_email_cliente.insert(0, config_atual.get("email_cliente", ""))
    entry_email_cliente.grid(row=9, column=1, padx=10, pady=6)

    fiscal_ativo_var = ctk.BooleanVar(value=bool(config_atual.get("fiscal_ativo", False)))
    check_fiscal_ativo = ctk.CTkCheckBox(
        frame_geral,
        text="ACBrMonitor (Emissão Fiscal) Ativo",
        variable=fiscal_ativo_var,
        onvalue=True,
        offvalue=False,
    )
    check_fiscal_ativo.grid(row=15, column=0, columnspan=2, padx=10, pady=(2, 8), sticky="w")

    # L6: as duas secoes abaixo separam CADASTRO da EMPRESA de BACKUP/DRIVE.
    # O "ID Pasta Backup" saiu da UI: o destino agora e sempre resolvido como
    # FRS Mercado/<market_id>/backups. A chave legada continua sendo gravada
    # (abaixo) apenas para preservar o valor ja existente no config.json.

    # Campo de credenciais do Google Drive removido da interface - localização automática
    # O arquivo credentials.json é localizado automaticamente pelo sistema de prioridades
    # (caminho executável -> diretório base -> APPDATA do usuário)

    ctk.CTkLabel(frame_geral, text="Pasta Entrada Fiscal:").grid(row=12, column=0, padx=10, pady=6, sticky="e")
    entry_in = ctk.CTkEntry(frame_geral, width=270)
    entry_in.insert(0, config_atual.get("pasta_entrada_fiscal", ""))
    entry_in.grid(row=12, column=1, padx=(10, 0), pady=6, sticky="w")

    ctk.CTkLabel(frame_geral, text="Pasta Retorno Fiscal:").grid(row=13, column=0, padx=10, pady=6, sticky="e")
    entry_out = ctk.CTkEntry(frame_geral, width=270)
    entry_out.insert(0, config_atual.get("pasta_retorno_fiscal", ""))
    entry_out.grid(row=13, column=1, padx=(10, 0), pady=6, sticky="w")

    ctk.CTkLabel(frame_geral, text="Limite de Sangria Preventiva:").grid(row=14, column=0, padx=10, pady=6, sticky="e")
    entry_limite = ctk.CTkEntry(frame_geral, width=360)
    entry_limite.insert(0, f"{obter_limite_sangria_preventiva():.2f}")
    entry_limite.grid(row=14, column=1, padx=10, pady=6)
    aplicar_padrao_entrada_numerica(entry_limite, inteiro=False, casas_decimais=2)

    lbl_status_backup_drive = ctk.CTkLabel(
        frame_drive, text="", font=("Arial", 11, "bold"), justify="left", anchor="w",
    )
    lbl_status_backup_drive.grid(row=1, column=0, columnspan=2, padx=10, pady=(2, 6), sticky="w")

    ctk.CTkLabel(frame_drive, text="Gmail para fazer backups:").grid(
        row=2, column=0, padx=10, pady=6, sticky="e",
    )
    entry_gmail_backup = ctk.CTkEntry(
        frame_drive, width=300, placeholder_text="exemplo@gmail.com",
    )
    _conta_ja_conectada = str(config_atual.get("google_drive_conta") or "").strip()
    if _conta_ja_conectada:
        entry_gmail_backup.insert(0, _conta_ja_conectada)
    entry_gmail_backup.grid(row=2, column=1, padx=10, pady=6, sticky="w")

    def atualizar_alerta_backup_drive():
        import drive_conexao as _conexao

        status = _conexao.status_conexao()
        lbl_status_backup_drive.configure(text=status["mensagem"], text_color=status["cor"])
        return status

    def _eh_administrador():
        """Reaproveita o perfil de usuario JA existente (nao cria RBAC novo)."""
        try:
            usuario = getattr(master, "usuario_atual", None) or {}
            permissao = usuario.get("permissão") or usuario.get("permissao") or ""
            return str(permissao).strip().lower() in ("administrador", "admin", "administrator")
        except Exception:
            return False

    def acao_conectar_drive():
        if not _eh_administrador():
            _registrar_acesso_drive("Tentativa de conectar Drive sem perfil de administrador.")
            status_label.configure(
                text="Apenas administrador pode conectar o Google Drive.", text_color="#ff6666",
            )
            return
        email = entry_gmail_backup.get().strip()
        import drive_conexao as _conexao

        # [DRIVE-DIAG] Início do clique em Conectar ao Google Drive
        import sys, os, traceback
        from pathlib import Path
        from modulo_config import carregar_credenciais_google

        print("[DRIVE-DIAG] Clique em Conectar ao Google Drive iniciado")
        print(f"[DRIVE-DIAG] email_informado={email}")
        print(f"[DRIVE-DIAG] sys.executable={sys.executable}")
        print(f"[DRIVE-DIAG] sys.frozen={getattr(sys, 'frozen', False)}")
        print(f"[DRIVE-DIAG] os.getcwd()={os.getcwd()}")
        print(f"[DRIVE-DIAG] Path(__file__).parent.resolve()={Path(__file__).parent.resolve()}")
        if getattr(sys, 'frozen', False):
            print(f"[DRIVE-DIAG] diretorio_exe={Path(sys.executable).resolve().parent}")
            print(f"[DRIVE-DIAG] sys._MEIPASS={getattr(sys, '_MEIPASS', 'N/A')}")

        # Carregar credenciais fresh no momento do clique
        creds = carregar_credenciais_google()
        print(f"[DRIVE-DIAG] carregar_credenciais_google() retornou: type={type(creds).__name__}")
        print(f"[DRIVE-DIAG]   credentials={'EXISTE' if creds.get('credentials') else 'AUSENTE'}")
        if creds.get('credentials'):
            print(f"[DRIVE-DIAG]   caminho_credentials={creds['credentials']}")
            print(f"[DRIVE-DIAG]   credenciais_existe={Path(creds['credentials']).exists()}")
        print(f"[DRIVE-DIAG]   google_services={'EXISTE' if creds.get('google_services') else 'AUSENTE'}")
        if creds.get('google_services'):
            print(f"[DRIVE-DIAG]   caminho_google_services={creds['google_services']}")
            print(f"[DRIVE-DIAG]   google_services_existe={Path(creds['google_services']).exists()}")

        # Verificar se credentials.json existe (necessário para OAuth2)
        creds_path = creds.get("credentials")
        creds_ok = bool(creds_path and Path(creds_path).exists())
        print(f"[DRIVE-DIAG] credenciais_validas={creds_ok}")

        if not creds_ok:
            # Credenciais OAuth2 não encontradas
            msg_erro = ("Integração Google indisponível: Arquivo 'credentials.json' não encontrado. "
                        "Verifique se o arquivo está incluído no pacote do executável.")
            print(f"[DRIVE-DIAG] ERRO: {msg_erro}")
            from database_manager import registrar_log
            registrar_log(None, "Segurança/Config", "Falha", msg_erro)
            status_label.configure(
                text="Arquivo credentials.json não encontrado para OAuth.", text_color="#ff6666",
            )
            return

        # Credenciais disponíveis - prosseguir com conexão
        ok, motivo = _conexao.validar_gmail(email)
        if not ok:
            status_label.configure(text=motivo, text_color="#ff6666")
            return
        try:
            lbl_status_backup_drive.configure(
                text="Aguardando autorização no Google…", text_color="#f1c40f",
            )
            # [DRIVE-DIAG] Chamar conectar com credenciais fresh
            print(f"[DRIVE-DIAG] Chamando _conexao.conectar(email, credenciais=creds)")
            resultado = _conexao.conectar(email, credenciais=creds)
            print(f"[DRIVE-DIAG] conectar() retornou com sucesso: conta={resultado.get('conta')}")
            entry_gmail_backup.delete(0, "end")
            entry_gmail_backup.insert(0, resultado["conta"])
            atualizar_alerta_backup_drive()
            if resultado["divergente"]:
                status_label.configure(
                    text=(f"Conectado como {resultado['conta']} — não é o Gmail informado "
                          f"({resultado['email_informado']})."),
                    text_color="#f1c40f",
                )
            else:
                status_label.configure(
                    text="Google Drive conectado. Backups automáticos ativados.", text_color="#2ecc71",
                )
        except Exception as e:
            print(f"[DRIVE-DIAG] EXCEÇÃO em conectar(): {e}")
            print(f"[DRIVE-DIAG] Traceback: {traceback.format_exc()}")
            atualizar_alerta_backup_drive()
            status_label.configure(text=f"Falha ao conectar o Drive: {e}", text_color="#ff6666")

    def _escolher_pasta_destino():
        pasta = filedialog.askdirectory(title="Onde salvar o backup?")
        if pasta:
            janela_config.focus_force()
        return pasta

    def acao_baixar_ultimo():
        pasta = _escolher_pasta_destino()
        if not pasta:
            return
        try:
            import backup_download as _download

            resultado = _download.baixar_ultimo_backup(pasta)
            status_label.configure(
                text=f"Backup baixado: {resultado['backup_id']}", text_color="#2ecc71",
            )
        except Exception as e:
            status_label.configure(text=f"Falha ao baixar backup: {e}", text_color="#ff6666")

    def acao_escolher_backup():
        try:
            import backup_download as _download

            disponiveis = _download.listar_backups_mercado()
        except Exception as e:
            status_label.configure(text=f"Falha ao listar backups: {e}", text_color="#ff6666")
            return
        if not disponiveis:
            status_label.configure(
                text="Nenhum backup encontrado para este mercado.", text_color="#f1c40f",
            )
            return
        janela_lista = ctk.CTkToplevel(janela_config)
        janela_lista.title("Backups disponíveis")
        janela_lista.geometry("560x420")
        lista = ctk.CTkScrollableFrame(janela_lista)
        lista.pack(fill="both", expand=True, padx=10, pady=10)

        def _baixar(item):
            pasta = _escolher_pasta_destino()
            if not pasta:
                return
            try:
                import backup_download as _download

                resultado = _download.baixar_backup(item["backup_id"], pasta)
                janela_lista.destroy()
                status_label.configure(
                    text=f"Backup baixado: {resultado['backup_id']}", text_color="#2ecc71",
                )
            except Exception as e:
                status_label.configure(text=f"Falha ao baixar backup: {e}", text_color="#ff6666")

        for item in disponiveis:
            linha = ctk.CTkFrame(lista)
            linha.pack(fill="x", pady=3)
            ctk.CTkLabel(
                linha, text=f"{item['data_hora']}  •  {item['tipo']}", anchor="w",
            ).pack(side="left", padx=8)
            ctk.CTkButton(
                linha, text="BAIXAR", width=90, command=lambda i=item: _baixar(i),
            ).pack(side="right", padx=8)

    ctk.CTkButton(
        frame_drive,
        text="CONECTAR AO GOOGLE DRIVE",
        fg_color="#1f6aa5",
        hover_color="#144870",
        command=acao_conectar_drive,
    ).grid(row=3, column=0, padx=10, pady=(6, 6), sticky="w")

    ctk.CTkLabel(frame_drive, text="Backups", font=("Arial", 12, "bold")).grid(
        row=4, column=0, columnspan=2, padx=10, pady=(14, 4), sticky="w",
    )
    ctk.CTkButton(
        frame_drive, text="BAIXAR ÚLTIMO BACKUP", command=acao_baixar_ultimo,
    ).grid(row=5, column=0, padx=10, pady=4, sticky="w")
    ctk.CTkButton(
        frame_drive, text="ESCOLHER BACKUP", command=acao_escolher_backup,
    ).grid(row=5, column=1, padx=10, pady=4, sticky="w")
    ctk.CTkLabel(
        frame_drive,
        text=(
            "☁️ BACKUP OPERACIONAL (LEVE) — apenas BAIXA o arquivo do Drive.\n"
            "Baixar um backup não altera seus dados atuais. Para aplicar esses "
            "dados no sistema, use 'Restaurar do Google Drive' abaixo."
        ),
        font=("Arial", 10), text_color="#9aa5b1", wraplength=520, justify="left",
    ).grid(row=6, column=0, columnspan=2, padx=10, pady=(4, 8), sticky="w")

    atualizar_alerta_backup_drive()
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

    ctk.CTkButton(frame_geral, text="...", width=40, command=lambda: procurar_exe(entry_emissor)).grid(row=9, column=1, padx=(0, 10), pady=6, sticky="e")
    ctk.CTkButton(frame_geral, text="...", width=40, command=lambda: selecionar_pasta(entry_in)).grid(row=12, column=1, padx=(0, 10), pady=6, sticky="e")
    ctk.CTkButton(frame_geral, text="...", width=40, command=lambda: selecionar_pasta(entry_out)).grid(row=13, column=1, padx=(0, 10), pady=6, sticky="e")

    # Largura da bobina térmica. Fica na row=18 (livre) para não renumerar
    # os campos existentes. Somente 58 mm e 80 mm nesta versão: qualquer
    # outro valor é tratado como 80 mm por _largura_cupom_chars().
    try:
        largura_atual = int(config_atual.get("largura_cupom_mm") or 80)
    except (TypeError, ValueError):
        largura_atual = 80
    if largura_atual not in (58, 80):
        largura_atual = 80
    largura_cupom_var = ctk.StringVar(value=f"{largura_atual} mm")

    ctk.CTkLabel(frame_geral, text="Largura da Bobina:").grid(row=18, column=0, padx=10, pady=6, sticky="e")
    ctk.CTkOptionMenu(
        frame_geral,
        values=["58 mm", "80 mm"],
        variable=largura_cupom_var,
        width=140,
    ).grid(row=18, column=1, padx=10, pady=6, sticky="w")

    # --- L7: BACKUP / RESTAURAÇÃO (seção própria; a UI só confirma, o motor fica em backup_restore.py) ---
    def _status_l7(mensagem, cor="#aaaaaa"):
        status_label.configure(text=mensagem, text_color=cor)
        janela_config.update_idletasks()

    def _executar_restauracao_drive(item, aceitar_novos=False):
        import backup_restore as restore

        def _prog(mensagem):
            _status_l7(f"Restauração do Drive: {mensagem}", "#f1c40f")

        try:
            resultado = restore.restaurar_do_drive(
                item["backup_id"], confirmado=True,
                aceitar_dados_mais_novos=aceitar_novos, ao_atualizar=_prog,
            )
        except restore.ErroRestauracao as erro:
            mensagem = str(erro)
            if "mais novos" in mensagem and not aceitar_novos:
                if messagebox.askyesno(
                    "Dados locais mais novos",
                    mensagem + "\n\nSubstituir os dados locais mesmo assim?",
                ):
                    _executar_restauracao_drive(item, aceitar_novos=True)
                    return
                _status_l7("Restauração cancelada pelo usuário.", "#f1c40f")
                return
            messagebox.showerror("Restauração do Google Drive", mensagem)
            _status_l7(f"Falha na restauração: {mensagem}", "#ff6666")
            return
        except Exception as erro:
            messagebox.showerror("Restauração do Google Drive", str(erro))
            _status_l7(f"Falha na restauração: {erro}", "#ff6666")
            return
        _status_l7(f"Restauração concluída: {', '.join(resultado['arquivos'])}.", "#2ecc71")
        messagebox.showinfo(
            "Restauração concluída",
            "Backup restaurado com sucesso.\n\n"
            f"Arquivos: {', '.join(resultado['arquivos'])}\n"
            f"Cópia de segurança do estado anterior:\n{resultado['seguranca'] or 'não criada'}\n\n"
            "Reinicie o FRS Mercado para aplicar integralmente.",
        )

    def acao_restaurar_drive():
        import backup_restore as restore

        try:
            disponiveis = restore.listar_backups()
        except Exception as erro:
            _status_l7(f"Falha ao listar backups: {erro}", "#ff6666")
            return
        if not disponiveis:
            messagebox.showinfo(
                "Restaurar do Google Drive",
                "Nenhum backup encontrado para este mercado.",
            )
            return
        janela_lista = ctk.CTkToplevel(janela_config)
        janela_lista.title("Restaurar backup do Google Drive")
        janela_lista.geometry("580x440")
        janela_lista.transient(janela_config)
        janela_lista.grab_set()
        ctk.CTkLabel(
            janela_lista,
            text="ATENÇÃO: restaurar SUBSTITUI os dados atuais pelo backup.",
            font=("Arial", 12, "bold"), text_color="#f1c40f",
        ).pack(padx=10, pady=(10, 8))
        lista = ctk.CTkFrame(janela_lista)
        lista.pack(fill="both", expand=True, padx=10, pady=10)

        def _escolher(item):
            janela_lista.destroy()
            if not messagebox.askyesno(
                "Confirmar restauração",
                f"Restaurar o backup?\n\n"
                f"Identificação: {item['backup_id']}\n"
                f"Data: {item['data_hora']}  |  Tipo: {item['tipo']}\n\n"
                "Seus dados atuais serão SUBSTITUÍDOS pelos dados do backup.\n"
                "Uma cópia de segurança do estado atual será criada antes.",
            ):
                _status_l7("Restauração cancelada pelo usuário.", "#f1c40f")
                return
            _executar_restauracao_drive(item)

        for item in disponiveis:
            linha = ctk.CTkFrame(lista)
            linha.pack(fill="x", pady=3)
            ctk.CTkLabel(
                linha, text=f"{item['data_hora']}  •  {item['tipo']}", anchor="w",
            ).pack(side="left", padx=8)
            ctk.CTkButton(
                linha, text="RESTAURAR", width=110, fg_color="#aa3333",
                hover_color="#772222", command=lambda i=item: _escolher(i),
            ).pack(side="right", padx=8)

def acao_restaurar_drive_threaded():
    import backup_restore as restore
    import threading
    from tkinter import messagebox

    # Protecao contra cliques duplicados
    if getattr(acao_restaurar_drive_threaded, "_em_execucao", False):
        return
    acao_restaurar_drive_threaded._em_execucao = True

    # Atualizar UI para indicar processamento
    try:
        if janela_config.winfo_exists():
            janela_config.after(0, lambda: _status_l7("Conectando ao Google Drive...", "#f1c40f"))
    except Exception:
        pass

    resultado_container = {"disponiveis": None, "erro": None, "finalizado": False}
    evento_final = threading.Event()

    def _trabalho_restaurar():
        try:
            disponiveis = restore.listar_backups()
            resultado_container["disponiveis"] = disponiveis
        except Exception as erro:
            resultado_container["erro"] = str(erro)
        finally:
            resultado_container["finalizado"] = True
            evento_final.set()

    thread = threading.Thread(target=_trabalho_restaurar, daemon=True)
    thread.start()
    # REMOVIDO: thread.join(timeout=120) - bloqueava a thread principal do Tkinter
    # Usar polling via after() para manter a UI responsiva durante a autenticacao
    def _verificar_conclusao():
        if not janela_config.winfo_exists():
            return
        if thread.is_alive():
            janela_config.after(500, _verificar_conclusao)
            return
        acao_restaurar_drive_threaded._em_execucao = False
        try:
            if not janela_config.winfo_exists():
                return
        except Exception:
            return
        if resultado_container["erro"]:
            _status_l7(f"Falha ao listar backups: {resultado_container['erro']}", "#ff6666")
            try:
                if janela_config.winfo_exists():
                    messagebox.showerror("Restaurar do Google Drive", f"Falha na conexao: {resultado_container['erro']}", parent=janela_config)
            except Exception:
                pass
            return
        disponiveis = resultado_container["disponiveis"]
        if not disponiveis:
            try:
                if janela_config.winfo_exists():
                    messagebox.showinfo("Restaurar do Google Drive", "Nenhum backup encontrado para este mercado.", parent=janela_config)
            except Exception:
                pass
            _status_l7("Nenhum backup encontrado no Google Drive.", "#f1c40f")
            return
        try:
            if not janela_config.winfo_exists():
                return
            janela_lista = ctk.CTkToplevel(janela_config)
            janela_lista.title("Restaurar backup do Google Drive")
            janela_lista.geometry("580x440")
            janela_lista.transient(janela_config)
            janela_lista.grab_set()
            ctk.CTkLabel(janela_lista, text="ATENCAO: restaurar SUBSTITUI os dados atuais pelo backup.", font=("Arial", 12, "bold"), text_color="#f1c40f").pack(padx=10, pady=(10, 8))
            lista = ctk.CTkFrame(janela_lista)
            lista.pack(fill="both", expand=True, padx=10, pady=10)
            def _escolher(item):
                janela_lista.destroy()
                if not messagebox.askyesno("Confirmar restauracao", f"Restaurar o backup?\\n\\nIdentificacao: {item['backup_id']}\\nData: {item['data_hora']}  |  Tipo: {item['tipo']}\\n\\nSeus dados atuais serao SUBSTITUIDOS pelos dados do backup.\\nUma copia de seguranca do estado atual sera criada antes.", parent=janela_config):
                    _status_l7("Restauracao cancelada pelo usuario.", "#f1c40f")
                    return
                _executar_restauracao_drive(item)
            for item in disponiveis:
                linha = ctk.CTkFrame(lista)
                linha.pack(fill="x", pady=3)
                ctk.CTkLabel(linha, text=f"{item['data_hora']}  .  {item['tipo']}", anchor="w").pack(side="left", padx=8)
                ctk.CTkButton(linha, text="RESTAURAR", width=110, fg_color="#aa3333", hover_color="#772222", command=lambda i=item: _escolher(i)).pack(side="right", padx=8)
            _status_l7(f"Backups listados: {len(disponiveis)} disponiveis.", "#2ecc71")
        except Exception as e:
            _status_l7(f"Falha ao exibir lista: {e}", "#ff6666")
            try:
                if janela_config.winfo_exists():
                    from tkinter import messagebox
                    messagebox.showerror("Restaurar do Google Drive", f"Erro: {e}", parent=janela_config)
            except Exception:
                pass
        janela_config.after(100, _verificar_conclusao)

    # Atualizar UI para indicar processamento
    try:
        if janela_config.winfo_exists():
            janela_config.after(0, lambda: _status_l7("Conectando ao Google Drive...", "#f1c40f"))
    except Exception:
        pass

    resultado_container = {"disponiveis": None, "erro": None, "finalizado": False}
    evento_final = threading.Event()

    def _trabalho_restaurar():
        try:
            disponiveis = restore.listar_backups()
            resultado_container["disponiveis"] = disponiveis
        except Exception as erro:
            resultado_container["erro"] = str(erro)
        finally:
            resultado_container["finalizado"] = True
            evento_final.set()

    thread = threading.Thread(target=_trabalho_restaurar, daemon=True)
    thread.start()
    # REMOVIDO: thread.join(timeout=120) - bloqueava a thread principal do Tkinter
    # Usar polling via after() para manter a UI responsiva durante a autenticacao

    # Atualizar UI para indicar processamento
    try:
        if janela_config.winfo_exists():
            janela_config.after(0, lambda: _status_l7("Conectando ao Google Drive...", "#f1c40f"))
    except Exception:
        pass

    resultado_container = {"disponiveis": None, "erro": None, "finalizado": False}
    evento_final = threading.Event()

    def _trabalho_restaurar():
        try:
            disponiveis = restore.listar_backups()
            resultado_container["disponiveis"] = disponiveis
        except Exception as erro:
            resultado_container["erro"] = str(erro)
        finally:
            resultado_container["finalizado"] = True
            evento_final.set()

    thread = threading.Thread(target=_trabalho_restaurar, daemon=True)
     
    thread.start()
    # Nao bloquear a thread principal - usar polling via after()
    # thread.join foi removido para manter UI responsiva
    

    if thread.is_alive():
        resultado_container["erro"] = "Operacao Google Drive demorou mais de 2 minutos. Verifique o navegador."
        evento_final.set()

    evento_final.wait(timeout=5)
    acao_restaurar_drive_threaded._em_execucao = False

    # Processar resultado na thread principal
    try:
        if not janela_config.winfo_exists():
            return
    except Exception:
        return

    if resultado_container["erro"]:
        _status_l7(f"Falha ao listar backups: {resultado_container['erro']}", "#ff6666")
        try:
            if janela_config.winfo_exists():
                messagebox.showerror("Restaurar do Google Drive", f"Falha na conexao: {resultado_container['erro']}", parent=janela_config)
        except Exception:
            pass
        return

    disponiveis = resultado_container["disponiveis"]
    if not disponiveis:
        try:
            if janela_config.winfo_exists():
                messagebox.showinfo("Restaurar do Google Drive", "Nenhum backup encontrado para este mercado.", parent=janela_config)
        except Exception:
            pass
        _status_l7("Nenhum backup encontrado no Google Drive.", "#f1c40f")
        return

    # Criar janela de lista de backups (na thread principal)
    try:
        if not janela_config.winfo_exists():
            return
        janela_lista = ctk.CTkToplevel(janela_config)
        janela_lista.title("Restaurar backup do Google Drive")
        janela_lista.geometry("580x440")
        janela_lista.transient(janela_config)
        janela_lista.grab_set()
        ctk.CTkLabel(janela_lista, text="ATENCAO: restaurar SUBSTITUI os dados atuais pelo backup.", font=("Arial", 12, "bold"), text_color="#f1c40f").pack(padx=10, pady=(10, 8))
        lista = ctk.CTkFrame(janela_lista)
        lista.pack(fill="both", expand=True, padx=10, pady=10)

        def _escolher(item):
            janela_lista.destroy()
            if not messagebox.askyesno("Confirmar restauracao", f"Restaurar o backup?\\n\\nIdentificacao: {item['backup_id']}\\nData: {item['data_hora']}  |  Tipo: {item['tipo']}\\n\\nSeus dados atuais serao SUBSTITUIDOS pelos dados do backup.\\nUma copia de seguranca do estado atual sera criada antes.", parent=janela_config):
                _status_l7("Restauracao cancelada pelo usuario.", "#f1c40f")
                return
            _executar_restauracao_drive(item)

        for item in disponiveis:
            linha = ctk.CTkFrame(lista)
            linha.pack(fill="x", pady=3)
            ctk.CTkLabel(linha, text=f"{item['data_hora']}  .  {item['tipo']}", anchor="w").pack(side="left", padx=8)
            ctk.CTkButton(linha, text="RESTAURAR", width=110, fg_color="#aa3333", hover_color="#772222", command=lambda i=item: _escolher(i)).pack(side="right", padx=8)

        _status_l7(f"Backups listados: {len(disponiveis)} disponiveis.", "#2ecc71")
    except Exception as e:
        _status_l7(f"Falha ao exibir lista: {e}", "#ff6666")
        try:
            if janela_config.winfo_exists():
                from tkinter import messagebox
                messagebox.showerror("Restaurar do Google Drive", f"Erro: {e}", parent=janela_config)
        except Exception:
            pass
    def _popup_explicativo_backup_geral() -> bool:
        dialogo = ctk.CTkToplevel(janela_config)
        dialogo.title("Backup Geral")
        dialogo.geometry("620x460")
        dialogo.transient(janela_config)
        dialogo.grab_set()
        texto = (
            "BACKUP GERAL — backup completo para migração ou recuperação.\n\n"
            "• Este é um backup COMPLETO do ambiente do FRS Mercado, para "
            "migração de computador, recuperação total ou cópia de segurança.\n"
            "• NÃO é o mesmo backup leve do Google Drive.\n"
            "• NÃO é enviado ao Google Drive.\n"
            "• Deve ser gravado em uma mídia física removível: recomendamos "
            "pendrive, HD ou SSD externo.\n"
            "• O próprio FRS Mercado vai compactar e validar o backup.\n"
            "• Você não precisa compactar nada manualmente.\n"
            "• Ao terminar, o sistema informará que o backup está pronto.\n\n"
            "Deseja continuar?"
        )
        ctk.CTkLabel(
            dialogo, text=texto, justify="left", wraplength=560, font=("Arial", 13),
        ).pack(padx=22, pady=20, fill="both", expand=True)
        resultado = {"seguir": False}

        def _continuar():
            resultado["seguir"] = True
            dialogo.destroy()

        barra = ctk.CTkFrame(dialogo)
        barra.pack(pady=(0, 18))
        ctk.CTkButton(
            barra, text="Continuar", command=_continuar, width=150,
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            barra, text="Cancelar", command=dialogo.destroy, width=150,
            fg_color="#aa3333", hover_color="#772222",
        ).pack(side="left", padx=12)
        dialogo.wait_window()
        return resultado["seguir"]

    def _listar_unidades_para_backup() -> list:
        import backup_geral as geral

        try:
            unidades = geral.listar_unidades_removiveis()
        except Exception as erro:
            _status_l7(f"Falha ao detectar mídias: {erro}", "#ff6666")
            return []
        if not unidades and messagebox.askyesno(
            "Nenhum pendrive detectado",
            "Nenhuma mídia removível foi detectada.\n\n"
            "Deseja listar discos fixos (ex.: HD/SSD externo que o Windows "
            "classifica como disco fixo)?",
        ):
            try:
                unidades = geral.listar_unidades_removiveis(incluir_fixas=True)
            except Exception as erro:
                _status_l7(f"Falha ao detectar mídias: {erro}", "#ff6666")
                return []
        return unidades

    def _selecionar_unidade(unidades, titulo):
        janela_un = ctk.CTkToplevel(janela_config)
        janela_un.title(titulo)
        janela_un.geometry("660x340")
        janela_un.transient(janela_config)
        janela_un.grab_set()
        ctk.CTkLabel(janela_un, text="Selecione a mídia de destino:", font=("Arial", 13, "bold")).pack(padx=14, pady=(14, 8), anchor="w")
        escolhida = {"raiz": None}

        def _escolher(raiz, win):
            escolhida["raiz"] = raiz
            win.destroy()

        for unidade in unidades:
            livre_gb = unidade.get("espaco_livre", 0) / (1024 ** 3)
            rotulo = unidade.get("rotulo") or "sem rótulo"
            legenda = (
                f"{unidade['raiz']}  —  {rotulo}  —  tipo {unidade.get('tipo_nome', '?')}  —  "
                f"{livre_gb:.1f} GB livres"
            )
            ctk.CTkButton(
                janela_un, text=legenda, anchor="w", height=34,
                command=lambda r=unidade["raiz"], w=janela_un: _escolher(r, w),
            ).pack(fill="x", padx=14, pady=3)
        ctk.CTkButton(
            janela_un, text="Cancelar", command=janela_un.destroy, width=120,
            fg_color="#aa3333", hover_color="#772222",
        ).pack(pady=12)
        janela_un.wait_window()
        return escolhida["raiz"]

    def acao_backup_geral():
        if not _popup_explicativo_backup_geral():
            _status_l7("Backup Geral cancelado.", "#f1c40f")
            return
        import backup_geral as geral

        unidades = _listar_unidades_para_backup()
        if not unidades:
            messagebox.showinfo(
                "Backup Geral",
                "Nenhuma mídia disponível detectada.\n\nNenhum backup foi criado.",
            )
            return
        raiz = _selecionar_unidade(unidades, "Backup Geral — selecione a mídia")
        if not raiz:
            _status_l7("Backup Geral cancelado.", "#f1c40f")
            return
        unidade = next((u for u in unidades if u["raiz"].lower() == raiz.lower()), None)
        if unidade is None:
            messagebox.showerror("Backup Geral", "Mídia selecionada inválida.")
            return
        try:
            necessario = geral.estimar_tamanho(geral.obter_raiz_dados())
        except Exception as erro:
            messagebox.showerror("Backup Geral", f"Falha ao estimar tamanho: {erro}")
            return
        livre = int(unidade.get("espaco_livre") or 0)
        if livre < necessario:
            messagebox.showerror(
                "Espaço insuficiente",
                f"Espaço insuficiente na mídia {unidade['raiz']}:\n"
                f"necessário ~{geral._formatar_bytes(necessario)}\n"
                f"disponível {geral._formatar_bytes(livre)}",
            )
            return
        if not messagebox.askyesno(
            "Confirmar Backup Geral",
            f"Destino: {unidade['raiz']} — {unidade.get('rotulo') or 'sem rótulo'}\n"
            f"Espaço livre: {geral._formatar_bytes(livre)}\n"
            f"Espaço necessário (estimado): {geral._formatar_bytes(necessario)}\n\n"
            "Gravar o Backup Geral nesta mídia?",
        ):
            _status_l7("Backup Geral cancelado.", "#f1c40f")
            return

        def _prog(mensagem):
            _status_l7(f"Backup Geral: {mensagem}", "#f1c40f")

        try:
            resultado = geral.criar_backup_geral(raiz, unidades=unidades, ao_atualizar=_prog)
        except geral.ErroBackupGeral as erro:
            messagebox.showerror("Backup Geral", str(erro))
            _status_l7(f"Falha no Backup Geral: {erro}", "#ff6666")
            return
        except Exception as erro:
            messagebox.showerror("Backup Geral", f"Falha inesperada: {erro}")
            _status_l7(f"Falha no Backup Geral: {erro}", "#ff6666")
            return
        _status_l7(f"Backup Geral concluído: {resultado['nome']}", "#2ecc71")
        messagebox.showinfo(
            "BACKUP GERAL CONCLUÍDO COM SUCESSO",
            f"Arquivo: {resultado['nome']}\n"
            f"Destino: {resultado['unidade']}\n"
            f"Tamanho: {geral._formatar_bytes(resultado['tamanho'])}\n"
            f"Arquivos: {resultado['arquivos']}\n\n"
            "O pacote foi validado depois de gravado. Guarde a mídia em local seguro.",
        )

    def acao_restaurar_backup_geral():
        import backup_geral as geral
        from market_identity import ensure_local_market_id

        candidatos = []
        try:
            for unidade in geral.listar_unidades_removiveis():
                raiz = Path(unidade["raiz"])
                candidatos.extend(sorted(raiz.glob("FRS_Mercado_*.zip")))
        except Exception as erro:
            _status_l7(f"Falha ao varrer mídias: {erro}", "#ff6666")
        if not candidatos:
            caminho = filedialog.askopenfilename(
                title="Localizar Backup Geral (.zip)",
                filetypes=[("Backup Geral", "*.zip"), ("Todos os arquivos", "*.*")],
            )
            if not caminho:
                return
            candidatos = [Path(caminho)]

        alvo = candidatos[0]
        if len(candidatos) > 1:
            janela_zips = ctk.CTkToplevel(janela_config)
            janela_zips.title("Selecione o Backup Geral")
            janela_zips.geometry("640x380")
            janela_zips.transient(janela_config)
            janela_zips.grab_set()
            ctk.CTkLabel(
                janela_zips, text="Múltiplos backups encontrados. Escolha um:", font=("Arial", 13, "bold"),
            ).pack(padx=14, pady=(14, 8), anchor="w")
            escolhida = {"caminho": None}

            def _escolher(p, win):
                escolhida["caminho"] = p
                win.destroy()

            for p in candidatos:
                ctk.CTkButton(
                    janela_zips, text=str(p), anchor="w", height=34,
                    command=lambda pp=p, w=janela_zips: _escolher(pp, w),
                ).pack(fill="x", padx=14, pady=3)
            ctk.CTkButton(
                janela_zips, text="Cancelar", command=janela_zips.destroy, width=120,
                fg_color="#aa3333", hover_color="#772222",
            ).pack(pady=12)
            janela_zips.wait_window()
            alvo = escolhida["caminho"]
            if not alvo:
                janela_zips.destroy()
                return
        # A variável janela_zips só existe se houve múltiplos; destruí-la aqui
        # é seguro (no else ela não existe).
        try:
            resumo = geral.inspecionar_pacote_geral(alvo)
        except geral.ErroBackupGeral as erro:
            messagebox.showerror("Restaurar Backup Geral", str(erro))
            _status_l7(f"Arquivo inválido: {erro}", "#ff6666")
            return

        try:
            market_local = str(ensure_local_market_id() or "")
        except Exception:
            market_local = ""
        divergente = bool(resumo.get("market_id") and market_local and resumo["market_id"] != market_local)
        ambiente = resumo.get("ambiente") or {}
        texto_resumo = (
            f"Arquivo: {alvo.name}\n"
            f"Gerado em: {resumo['gerado_em']}  |  FRS {resumo['app_version']}\n"
            f"Market do backup: {resumo['market_id']}\n"
            f"Market desta máquina: {market_local or '(não configurado)'}\n"
            f"Origem: {ambiente.get('hostname', '?')} ({ambiente.get('sistema', '?')})\n"
            f"Arquivos: {len(resumo['arquivos'])}  |  Tamanho: {geral._formatar_bytes(resumo['tamanho'])}\n\n"
            "ISTO VAI SUBSTITUIR os dados atuais pelos dados do backup.\n"
            "Uma cópia de segurança do estado atual será criada antes."
        )
        if divergente and not messagebox.askyesno(
            "Migração entre máquinas",
            texto_resumo + f"\n\nO backup é do mercado {resumo['market_id']!r}, "
            f"mas esta máquina está em {market_local!r}. Prosseguir com a migração?",
        ):
            _status_l7("Restauração de Backup Geral cancelada.", "#f1c40f")
            return
        if not messagebox.askyesno("Confirmar restauração destrutiva", texto_resumo):
            _status_l7("Restauração de Backup Geral cancelada.", "#f1c40f")
            return

        def _prog(mensagem):
            _status_l7(f"Restauração Geral: {mensagem}", "#f1c40f")

        try:
            resultado = geral.restaurar_backup_geral(
                alvo, confirmado=True, aceitar_market_diferente=divergente, ao_atualizar=_prog,
            )
        except geral.ErroBackupGeral as erro:
            messagebox.showerror("Restaurar Backup Geral", str(erro))
            _status_l7(f"Falha na restauração: {erro}", "#ff6666")
            return
        except Exception as erro:
            messagebox.showerror("Restaurar Backup Geral", f"Falha inesperada: {erro}")
            _status_l7(f"Falha na restauração: {erro}", "#ff6666")
            return
        _status_l7(f"Restauração concluída: {resultado['arquivos']}", "#2ecc71")
        messagebox.showinfo(
            "RESTAURAR BACKUP GERAL CONCLUÍDO",
            f"Backup restaurado de: {alvo.name}\n\n"
            f"Arquivos restaurados: {len(resultado['arquivos'])}\n"
            f"Cópia de segurança do estado anterior: {resultado['seguranca']}\n\n"
            "Reinicie o FRS Mercado para aplicar integralmente.",
        )

    frame_l7 = ctk.CTkFrame(frame_geral)
    frame_l7.grid(row=19, column=0, columnspan=2, padx=8, pady=(12, 16), sticky="ew")
    ctk.CTkLabel(
        frame_l7, text="💾 MIGRAÇÃO / RECUPERAÇÃO COMPLETA (mídia física)",
        font=("Arial", 14, "bold"),
    ).grid(row=0, column=0, columnspan=3, padx=10, pady=(8, 4), sticky="w")

    ctk.CTkButton(
        frame_l7, text="RESTAURAR DO GOOGLE DRIVE", command=acao_restaurar_drive_threaded
    ).grid(row=2, column=0, padx=8, pady=6, sticky="ew")
    ctk.CTkButton(
        frame_l7, text="BACKUP GERAL", command=acao_backup_geral,
    ).grid(row=2, column=1, padx=8, pady=6, sticky="ew")
    ctk.CTkButton(
        frame_l7, text="RESTAURAR BACKUP GERAL", command=acao_restaurar_backup_geral,
    ).grid(row=2, column=2, padx=8, pady=6, sticky="ew")

    ctk.CTkLabel(
        frame_l7,
        text=(
            "Backup Geral: pacote COMPLETO para migrar/recuperar o sistema, gravado "
            "em mídia física removível (pendrive/HD/SSD) — não vai para o Google Drive. "
            "As opções acima RESTAURAM (aplicam dados no sistema). Para apenas baixar "
            "o arquivo do Drive, use 'Baixar último backup' / 'Escolher backup'."
        ),
        font=("Arial", 10), text_color="#9aa5b1", wraplength=560, justify="left",
    ).grid(row=3, column=0, columnspan=3, padx=10, pady=(4, 8), sticky="w")

    # --- ABA FISCAL ---
    # A emissão fiscal é feita pelo motor ACBrMonitor (configurado na aba Geral).
    # Aqui resta apenas o token do webhook de pedidos externos, que é uma
    # funcionalidade ativa e independente do ACBr.
    ctk.CTkLabel(
        frame_fiscal,
        text="Pedidos Externos (Webhook)",
        font=("Arial", 16, "bold"),
    ).pack(pady=(12, 14))

    form_fiscal = ctk.CTkFrame(frame_fiscal)
    form_fiscal.pack(fill="x", padx=14, pady=8)

    ctk.CTkLabel(form_fiscal, text="Webhook Token:").grid(row=0, column=0, padx=10, pady=10, sticky="e")
    entry_webhook_token = ctk.CTkEntry(form_fiscal, width=440, show="*")
    entry_webhook_token.grid(row=0, column=1, padx=10, pady=10)
    ctk.CTkLabel(
        form_fiscal,
        text=(
            "Status token: CONFIGURADO"
            if fiscal_atual.get("webhook_token_configurado")
            else "Status token: NÃO CONFIGURADO"
        ),
        text_color="#2ecc71" if fiscal_atual.get("webhook_token_configurado") else "#ff6666",
        font=("Arial", 10, "bold"),
    ).grid(row=1, column=1, padx=10, pady=(0, 8), sticky="w")

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
                "nome_fantasia": entry_fantasia.get().strip(),
                "razao_social": entry_razao.get(),
                "nome_estabelecimento": (entry_fantasia.get().strip() or entry_razao.get()),
                "cnpj": entry_cnpj.get().strip(),
                "cep": entry_cep.get().strip(),
                "logradouro": entry_logradouro.get().strip(),
                "numero": entry_numero.get().strip(),
                "complemento": entry_complemento.get().strip(),
                "bairro": entry_bairro.get().strip(),
                "cidade": entry_cidade.get().strip(),
                "uf": entry_uf.get().strip().upper(),
                "email_cliente": entry_email_cliente.get(),
                "drive_credentials_path": entry_drive_creds.get(),
                # L6: a chave legada NAO aparece mais na UI, mas continua
                # gravada com o valor ja existente para nao apagar o dado do
                # cliente. O fluxo novo nao a utiliza.
                "drive_backup_folder_id": config_atual.get("drive_backup_folder_id", ""),
                "google_drive_conta": config_atual.get("google_drive_conta", ""),
                "emissor_fiscal_path": entry_emissor.get(),
                "fiscal_ativo": bool(fiscal_ativo_var.get()),
                "pasta_entrada_fiscal": entry_in.get(),
                "pasta_retorno_fiscal": entry_out.get(),
                "limite_sangria_preventiva": entry_limite.get(),
                "largura_cupom_mm": int(str(largura_cupom_var.get()).split("mm")[0].strip() or 80),
            }

            ok_geral, msg_geral = salvar_configuracoes(novos_dados, exibir_alerta=False)
            if not ok_geral:
                status_label.configure(text=msg_geral, text_color="#ff6666")
                janela_config.focus_force()
                return

            # A integração PlugNotas foi removida: api_key/ambiente legados
            # permanecem no banco apenas para compatibilidade e NÃO são
            # gravados aqui. O que se salva é o token do webhook.
            token_digitado = entry_webhook_token.get().strip()
            if token_digitado:
                salvar_token_webhook(token_digitado)
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

def _executar_fluxo_oauth_com_timeout(janela, timeout_segundos=120):
    import webbrowser, json, time, urllib.parse, threading
    resultado = {"sucesso": False, "credenciais": None, "erro": None}
    evento_final = threading.Event()
    url_autorizacao_definida = [False]
    def _obter_url_autorizacao():
        try:
            configuracao = carregar_configuracoes() or {}
            caminho_oauth = configuracao.get("google_oauth_credentials_path")
            if not caminho_oauth: return None
            caminho_projeto = Path(__file__).parent.resolve() / caminho_oauth
            if not caminho_projeto.exists(): return None
            with open(caminho_projeto, 'r', encoding='utf-8') as f: data = json.load(f)
            if 'installed' not in data and 'web' not in data: return None
            params = {'client_id': data['installed'].get('client_id') or data['web'].get('client_id'),
                      'redirect_uri': 'urn:ietf:wg:oauth:2.0:oob', 'response_type': 'code',
                      'scope': 'https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive',
                      'access_type': 'offline', 'prompt': 'consent'}
            base = data['installed'].get('auth_uri') or data['web'].get('auth_uri')
            if not base: return None
            query = urllib.parse.urlencode(params)
            return f"{base}?{query}"
        except Exception: return None
    def _trabalho_oauth():
        nonlocal url_autorizacao_definida
        try:
            url_auth = _obter_url_autorizacao()
            if not url_auth: resultado["erro"] = "Nao foi possivel construir a URL de autorizacao do Google."; evento_final.set(); return
            try: webbrowser.open(url_auth, new=2)
            except Exception: pass
            codigo_autorizacao = None
            tempo_inicio = time.time()
            while time.time() - tempo_inicio < timeout_segundos:
                if url_autorizacao_definida[0]: break
                time.sleep(2)
                if evento_final.is_set(): break
            if url_autorizacao_definida[0] and codigo_autorizacao:
                try:
                    caminho_oauth = configuracao.get("google_oauth_credentials_path")
                    caminho_projeto = Path(__file__).parent.resolve() / caminho_oauth
                    with open(caminho_projeto, 'r', encoding='utf-8') as f: data = json.load(f)
                    client_id = data['installed'].get('client_id')
                    client_secret = data['installed'].get('client_secret')
                    token_uri = data['installed'].get('token_uri')
                    if not all([client_id, client_secret, token_uri]): raise ValueError("OAuth dados incompletos")
                    token_data = {'code': codigo_autorizacao, 'client_id': client_id, 'client_secret': client_secret,
                                  'redirect_uri': 'urn:ietf:wg:oauth:2.0:oob', 'grant_type': 'authorization_code'}
                    import urllib.request
                    req = urllib.request.Request(token_uri, data=json.dumps(token_data).encode('utf-8'),
                        headers={'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=30) as resp: token_response = json.loads(resp.read().decode('utf-8'))
                    credenciais = {"credentials": str(Path(__file__).parent.resolve() / caminho_oauth),
                        "google_services": str(Path(__file__).parent.resolve() / caminho_oauth.with_name("google-services.json")),
                        "token": token_response.get('access_token'), "refresh_token": token_response.get('refresh_token'),
                        "expires_in": token_response.get('expires_in'), "scope": token_response.get('scope')}
                    resultado["sucesso"] = True; resultado["credenciais"] = credenciais; return
                except Exception as e: resultado["erro"] = f"Falha ao trocar codigo por token: {str(e)[:200]}"; evento_final.set(); return
            else: resultado["erro"] = "Autorizacao expirada ou cancelada pelo usuario."; evento_final.set()
        except Exception as e: resultado["erro"] = f"Erro inesperado no fluxo OAuth: {str(e)[:300]}"; evento_final.set()
    thread = threading.Thread(target=_trabalho_oauth, daemon=True)
    thread.start()
    # Nao bloquear a thread principal - usar polling via after()
    def _verificar_oauth_conclusao():
        if not janela.winfo_exists():
            return
        if thread.is_alive():
            janela.after(500, _verificar_oauth_conclusao)
            return
    if thread.is_alive(): resultado["erro"] = f"Operacao OAuth demorou mais de {timeout_segundos}s e foi cancelada"; return False, None, resultado["erro"]
    if resultado["erro"]:
        try:
            if janela and janela.winfo_exists():
                janela.after(0, lambda: _mostrar_mensagem_google(janela, "Google Drive", resultado["erro"], "error"))
        except Exception:
            pass
        return False, None, resultado["erro"]
    if resultado["sucesso"] and resultado["credenciais"]:
        try:
            if janela and janela.winfo_exists():
                janela.after(0, lambda: _mostrar_mensagem_google(janela, "Google Drive Sucesso", "Conexao Google Drive estabelecida com sucesso!\\nCredenciais salvas e backup agora disponivel.", "info"))
                janela.after(0, lambda: None)
        except Exception:
            pass
        return True, resultado["credenciais"], None
    return False, None, resultado["erro"]
def _mostrar_mensagem_google(janela, titulo, mensagem, tipo="info"):
    try:
        from tkinter import messagebox
        if janela and janela.winfo_exists():
            if tipo == "info": messagebox.showinfo(titulo, mensagem, parent=janela)
            elif tipo == "error": messagebox.showerror(titulo, mensagem, parent=janela)
            elif tipo == "warning": messagebox.showwarning(titulo, mensagem, parent=janela)
    except Exception: pass
    root.mainloop()
