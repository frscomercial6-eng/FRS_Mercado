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
        "drive_credentials_path": "",
        "drive_backup_folder_id": "",
    "google_drive_conta": "",
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

    # --- ABA GERAL ---
    ctk.CTkLabel(frame_geral, text="Nome Fantasia:").grid(row=0, column=0, padx=10, pady=6, sticky="e")
    entry_fantasia = ctk.CTkEntry(frame_geral, width=360)
    entry_fantasia.insert(0, config_atual.get("nome_fantasia", "") or config_atual.get("nome_estabelecimento", ""))
    entry_fantasia.grid(row=0, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Razão Social:").grid(row=1, column=0, padx=10, pady=6, sticky="e")
    entry_razao = ctk.CTkEntry(frame_geral, width=360)
    entry_razao.insert(0, config_atual.get("razao_social", ""))
    entry_razao.grid(row=1, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="CNPJ:").grid(row=2, column=0, padx=10, pady=6, sticky="e")
    entry_cnpj = ctk.CTkEntry(frame_geral, width=360)
    entry_cnpj.insert(0, config_atual.get("cnpj", ""))
    entry_cnpj.grid(row=2, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Endereço — Logradouro:").grid(row=3, column=0, padx=10, pady=6, sticky="e")
    entry_logradouro = ctk.CTkEntry(frame_geral, width=280)
    entry_logradouro.insert(0, config_atual.get("logradouro", ""))
    entry_logradouro.grid(row=3, column=1, padx=(10, 0), pady=6, sticky="w")
    ctk.CTkLabel(frame_geral, text="Nº:").grid(row=3, column=1, padx=(0, 0), pady=6, sticky="e")
    entry_numero = ctk.CTkEntry(frame_geral, width=70)
    entry_numero.insert(0, config_atual.get("numero", ""))
    entry_numero.grid(row=3, column=1, padx=(0, 10), pady=6, sticky="e")

    ctk.CTkLabel(frame_geral, text="Complemento:").grid(row=4, column=0, padx=10, pady=6, sticky="e")
    entry_complemento = ctk.CTkEntry(frame_geral, width=360)
    entry_complemento.insert(0, config_atual.get("complemento", ""))
    entry_complemento.grid(row=4, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Bairro:").grid(row=5, column=0, padx=10, pady=6, sticky="e")
    entry_bairro = ctk.CTkEntry(frame_geral, width=360)
    entry_bairro.insert(0, config_atual.get("bairro", ""))
    entry_bairro.grid(row=5, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Cidade:").grid(row=6, column=0, padx=10, pady=6, sticky="e")
    entry_cidade = ctk.CTkEntry(frame_geral, width=220)
    entry_cidade.insert(0, config_atual.get("cidade", ""))
    entry_cidade.grid(row=6, column=1, padx=(10, 0), pady=6, sticky="w")
    ctk.CTkLabel(frame_geral, text="UF:").grid(row=6, column=1, padx=(0, 90), pady=6, sticky="e")
    entry_uf = ctk.CTkEntry(frame_geral, width=60)
    entry_uf.insert(0, config_atual.get("uf", ""))
    entry_uf.grid(row=6, column=1, padx=(0, 10), pady=6, sticky="e")

    ctk.CTkLabel(frame_geral, text="CEP:").grid(row=7, column=0, padx=10, pady=6, sticky="e")
    entry_cep = ctk.CTkEntry(frame_geral, width=360)
    entry_cep.insert(0, config_atual.get("cep", ""))
    entry_cep.grid(row=7, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="E-mail do Cliente:").grid(row=8, column=0, padx=10, pady=6, sticky="e")
    entry_email_cliente = ctk.CTkEntry(frame_geral, width=360)
    entry_email_cliente.insert(0, config_atual.get("email_cliente", ""))
    entry_email_cliente.grid(row=8, column=1, padx=10, pady=6)

    ctk.CTkLabel(frame_geral, text="Emissor Fiscal (.exe):").grid(row=9, column=0, padx=10, pady=6, sticky="e")
    entry_emissor = ctk.CTkEntry(frame_geral, width=270)
    entry_emissor.insert(0, config_atual.get("emissor_fiscal_path", ""))
    entry_emissor.grid(row=9, column=1, padx=(10, 0), pady=6, sticky="w")

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
    frame_cadastro = ctk.CTkFrame(frame_geral)
    frame_cadastro.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="ew")
    ctk.CTkLabel(
        frame_cadastro, text="🏢 CADASTRO DA EMPRESA",
        font=("Arial", 14, "bold"),
    ).grid(row=0, column=0, columnspan=2, padx=10, pady=(8, 4), sticky="w")

    frame_drive = ctk.CTkFrame(frame_geral)
    frame_drive.grid(row=1, column=0, columnspan=2, padx=8, pady=(12, 8), sticky="ew")
    ctk.CTkLabel(
        frame_drive, text="☁️ BACKUP E GOOGLE DRIVE",
        font=("Arial", 14, "bold"),
    ).grid(row=0, column=0, columnspan=2, padx=10, pady=(8, 4), sticky="w")

    ctk.CTkLabel(frame_geral, text="Google Drive (Credenciais):").grid(row=2, column=0, padx=10, pady=6, sticky="e")
    entry_drive_creds = ctk.CTkEntry(frame_geral, width=360)
    entry_drive_creds.insert(0, config_atual.get("drive_credentials_path", ""))
    entry_drive_creds.grid(row=2, column=1, padx=10, pady=6)

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

        ok, motivo = _conexao.validar_gmail(email)
        if not ok:
            status_label.configure(text=motivo, text_color="#ff6666")
            return
        try:
            lbl_status_backup_drive.configure(
                text="Aguardando autorização no Google…", text_color="#f1c40f",
            )
            resultado = _conexao.conectar(email)
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
                    text="Google Drive conectado. Backups automáticos ativados.",
                    text_color="#2ecc71",
                )
        except Exception as e:
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
        text="Baixar um backup não altera seus dados atuais.",
        font=("Arial", 10), text_color="#aaaaaa",
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

    root.mainloop()
