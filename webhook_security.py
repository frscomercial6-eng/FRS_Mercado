from datetime import datetime

import bcrypt

from app_paths import obter_caminho_dados
from database_manager import get_db_connection


LOG_INTEGRACAO = obter_caminho_dados("log_integracao.txt")


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


def _garantir_coluna_token_webhook(conn):
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


def salvar_token_webhook(token_plano):
    """Armazena token do webhook somente em hash bcrypt (sem texto puro)."""
    token = (token_plano or "").strip()
    if not token:
        return False

    token_hash = bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    with get_db_connection() as conn:
        _garantir_coluna_token_webhook(conn)
        conn.execute(
            """
            UPDATE config_fiscal
            SET webhook_token_hash = ?
            WHERE id = 1
            """,
            (token_hash,),
        )
    return True


def webhook_token_configurado():
    with get_db_connection() as conn:
        _garantir_coluna_token_webhook(conn)
        row = conn.execute("SELECT webhook_token_hash FROM config_fiscal WHERE id = 1").fetchone()
    return bool(row and row[0])


def validar_token_webhook(token_recebido):
    """Valida token recebido no header contra hash persistido."""
    token = (token_recebido or "").strip()
    with get_db_connection() as conn:
        _garantir_coluna_token_webhook(conn)
        row = conn.execute("SELECT webhook_token_hash FROM config_fiscal WHERE id = 1").fetchone()

    token_hash = row[0] if row and row[0] else ""
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
