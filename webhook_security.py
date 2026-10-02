from datetime import datetime

import bcrypt

from app_paths import obter_caminho_dados
from database_manager import get_db_connection


LOG_INTEGRACAO = obter_caminho_dados("log_integracao.txt")

# Persistência própria do webhook. Antes o hash vivia em
# config_fiscal.webhook_token_hash, misturado com as credenciais fiscais do
# PlugNotas (api_key/ambiente). A coluna antiga NÃO é removida aqui: ela
# continua no schema e só deixa de ser lida quando o novo valor já existe.
_TABELA_WEBHOOK = "config_webhook"
_TABELA_LEGADA = "config_fiscal"
_COLUNA_LEGADA = "webhook_token_hash"


def registrar_rejeicao_integracao(motivo, ip="desconhecido", path="/receber_pedido_externo"):
    """Registra tentativas rejeitadas de integração em log no APPDATA."""
    try:
        linha = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"IP={ip} PATH={path} MOTIVO={motivo}"
        )
        with open(LOG_INTEGRACAO, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


def _ler_token_legado(conn) -> str:
    """Hash antigo em config_fiscal, somente leitura e sem nunca falhar."""
    try:
        tabelas = {
            linha[0]
            for linha in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if _TABELA_LEGADA not in tabelas:
            return ""
        colunas = [
            linha[1]
            for linha in conn.execute(f"PRAGMA table_info({_TABELA_LEGADA})").fetchall()
        ]
        if _COLUNA_LEGADA not in colunas:
            return ""
        row = conn.execute(
            f"SELECT {_COLUNA_LEGADA} FROM {_TABELA_LEGADA} WHERE id = 1"
        ).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _garantir_tabela_config_webhook(conn) -> None:
    """Cria config_webhook e herda o token já salvo pelo cliente.

    Idempotente por construção: só grava quando o destino está VAZIO, nunca
    substitui um token existente e nunca troca um token salvo por default.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABELA_WEBHOOK} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token_hash TEXT NOT NULL DEFAULT ''
        )
        """
    )
    row = conn.execute(f"SELECT token_hash FROM {_TABELA_WEBHOOK} WHERE id = 1").fetchone()
    if row is None:
        conn.execute(f"INSERT INTO {_TABELA_WEBHOOK} (id, token_hash) VALUES (1, '')")
        row = ("",)
    if str(row[0] or "").strip():
        # Já migrado (ou já configurado): preserva sem tocar.
        return

    legado = _ler_token_legado(conn)
    if legado.strip():
        conn.execute(
            f"UPDATE {_TABELA_WEBHOOK} SET token_hash = ? WHERE id = 1",
            (legado,),
        )


def _ler_token_hash(conn) -> str:
    """Hash vigente do webhook, com leitura segura da origem legada.

    Enquanto a migração não estiver confirmada (ex.: banco ainda aberto só
    pela versão antiga), o valor legado continua sendo aceito, para que um
    cliente já configurado nunca perca o acesso ao webhook.
    """
    _garantir_tabela_config_webhook(conn)
    row = conn.execute(f"SELECT token_hash FROM {_TABELA_WEBHOOK} WHERE id = 1").fetchone()
    if row and str(row[0] or "").strip():
        return str(row[0])
    return _ler_token_legado(conn)


def salvar_token_webhook(token_plano):
    """Armazena token do webhook somente em hash bcrypt (sem texto puro)."""
    token = (token_plano or "").strip()
    if not token:
        return False

    token_hash = bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    with get_db_connection() as conn:
        _garantir_tabela_config_webhook(conn)
        conn.execute(
            f"""
            UPDATE {_TABELA_WEBHOOK}
            SET token_hash = ?
            WHERE id = 1
            """,
            (token_hash,),
        )
    return True


def webhook_token_configurado():
    with get_db_connection() as conn:
        token_hash = _ler_token_hash(conn)
    return bool(token_hash and str(token_hash).strip())


def validar_token_webhook(token_recebido):
    """Valida token recebido no header contra hash persistido."""
    token = (token_recebido or "").strip()
    with get_db_connection() as conn:
        token_hash = _ler_token_hash(conn)

    token_hash = str(token_hash or "")
    if not token_hash:
        return False, "Token de webhook não configurado no sistema"
    if not token:
        return False, "Header X-Webhook-Token ausente"

    try:
        ok = bcrypt.checkpw(token.encode("utf-8"), token_hash.encode("utf-8"))
    except Exception:
        return False, "Falha ao validar token do webhook"

    if not ok:
        return False, "Token inválido"

    return True, "Token válido"
