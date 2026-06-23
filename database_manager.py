import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime
from app_paths import obter_caminho_dados


def _get_app_data_dir():
    return obter_caminho_dados()

def get_db_path():
    return obter_caminho_dados("mercado.db")


DB_PATH = get_db_path()
_PRODUTOS_SCHEMA_MIGRATED = False


def _configure_sqlite_connection(conn):
    """Aplica pragmas de concorrencia para reduzir lock entre caixas."""
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")


def _ensure_produtos_schema(conn):
    """Garante colunas essenciais da tabela produtos em bases legadas."""
    global _PRODUTOS_SCHEMA_MIGRATED
    if _PRODUTOS_SCHEMA_MIGRATED:
        return

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='produtos'")
    if not cursor.fetchone():
        _PRODUTOS_SCHEMA_MIGRATED = True
        return

    cursor.execute("PRAGMA table_info(produtos)")
    colunas = {row[1] for row in cursor.fetchall()}
    alteracoes = {
        "preco_custo": "ALTER TABLE produtos ADD COLUMN preco_custo REAL NOT NULL DEFAULT 0.0",
        "margem_lucro": "ALTER TABLE produtos ADD COLUMN margem_lucro REAL NOT NULL DEFAULT 0.0",
        "preco_venda": "ALTER TABLE produtos ADD COLUMN preco_venda REAL NOT NULL DEFAULT 0.0",
        "quantidade_atual": "ALTER TABLE produtos ADD COLUMN quantidade_atual INTEGER NOT NULL DEFAULT 0",
        "quantidade_minima": "ALTER TABLE produtos ADD COLUMN quantidade_minima INTEGER NOT NULL DEFAULT 0",
        "variacao": "ALTER TABLE produtos ADD COLUMN variacao TEXT",
    }

    for coluna, ddl in alteracoes.items():
        if coluna not in colunas:
            cursor.execute(ddl)

    cursor.execute("PRAGMA table_info(produtos)")
    tipos_por_coluna = {row[1]: (row[2] or "").upper() for row in cursor.fetchall()}
    requer_migracao_tipos = (
        tipos_por_coluna.get("preco_custo") != "REAL"
        or tipos_por_coluna.get("preco_venda") != "REAL"
    )

    if requer_migracao_tipos:
        conn.execute("PRAGMA foreign_keys = OFF;")
        try:
            cursor.execute("DROP TABLE IF EXISTS produtos_schema_tmp")
            cursor.execute(
                """
                CREATE TABLE produtos_schema_tmp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    codigo_barras TEXT UNIQUE NOT NULL,
                    nome TEXT NOT NULL,
                    variacao TEXT,
                    preco_custo REAL NOT NULL,
                    margem_lucro REAL NOT NULL DEFAULT 0.0,
                    preco_venda REAL NOT NULL,
                    quantidade_atual INTEGER NOT NULL DEFAULT 0,
                    quantidade_minima INTEGER NOT NULL DEFAULT 0,
                    validade DATE,
                    categoria TEXT,
                    preco_base NUMERIC,
                    inicio_promocao DATE,
                    fim_promocao DATE,
                    imagem_path TEXT
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO produtos_schema_tmp (
                    id, codigo_barras, nome, variacao, preco_custo, margem_lucro, preco_venda,
                    quantidade_atual, quantidade_minima, validade, categoria,
                    preco_base, inicio_promocao, fim_promocao, imagem_path
                )
                SELECT
                    id,
                    codigo_barras,
                    nome,
                    variacao,
                    CAST(COALESCE(preco_custo, 0.0) AS REAL),
                    CAST(COALESCE(margem_lucro, 0.0) AS REAL),
                    CAST(COALESCE(preco_venda, 0.0) AS REAL),
                    CAST(COALESCE(quantidade_atual, 0) AS INTEGER),
                    CAST(COALESCE(quantidade_minima, 0) AS INTEGER),
                    validade,
                    categoria,
                    preco_base,
                    inicio_promocao,
                    fim_promocao,
                    imagem_path
                FROM produtos
                """
            )
            cursor.execute("DROP TABLE produtos")
            cursor.execute("ALTER TABLE produtos_schema_tmp RENAME TO produtos")
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")

    _PRODUTOS_SCHEMA_MIGRATED = True

def _ensure_database_file():
    """Cria estrutura do banco do zero quando o arquivo não existe."""
    if os.path.exists(DB_PATH):
        return

    from database import init_db
    init_db()

@contextmanager
def get_db_connection():
    """
    Gerencia a conexão com o banco de dados mercado.db, garantindo que
    ela seja aberta, as transações commitadas ou revertidas, e a conexão fechada.
    Realiza auto-recuperação se o arquivo estiver corrompido ou inexistente.
    """
    conn = None
    try:
        # Garante que exista um banco em local gravável para o usuário atual.
        _ensure_database_file()

        try:
            conn = sqlite3.connect(DB_PATH)
            _configure_sqlite_connection(conn)
            _ensure_produtos_schema(conn)
            # Força uma leitura de metadados para validar se o arquivo é um banco válido
            conn.execute("SELECT name FROM sqlite_master LIMIT 1;")
        except sqlite3.DatabaseError as e:
            # Se o arquivo não for um banco de dados válido (corrompido)
            if "file is not a database" in str(e).lower():
                if conn: conn.close()
                
                try:
                    from database import init_db
                    # Renomeia o arquivo corrompido para backup
                    backup_path = os.path.join(_get_app_data_dir(), "mercado_old.db")
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                    os.rename(DB_PATH, backup_path)
                    
                    init_db() # Recria a estrutura completa
                    conn = sqlite3.connect(DB_PATH) # Nova conexão no arquivo limpo
                    _configure_sqlite_connection(conn)
                except Exception:
                    print("Erro ao recriar banco")
                    raise
            else:
                raise

        if conn is None:
            raise sqlite3.DatabaseError("Falha ao abrir conexão com o banco de dados.")

        yield conn
        conn.commit() # Commit automático se não houver exceções
    except sqlite3.Error as e:
        if conn:
            conn.rollback() # Rollback automático em caso de erro
        print(f"Erro no banco de dados: {e}")
        raise # Re-lança a exceção para que o chamador possa tratá-la
    finally:
        if conn:
            conn.close()

def registrar_log(usuario_id, acao, status, detalhes=None):
    """Registra uma ação no log de auditoria."""
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO logs_auditoria (timestamp, usuario_id, acao, status, detalhes) VALUES (?, ?, ?, ?, ?)",
                         (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario_id, acao, status, detalhes))
    except Exception as e:
        print(f"Erro ao registrar log de auditoria: {e}")

# Inicializa a localização das credenciais do Google para uso em backups e serviços externos
try:
    from modulo_config import carregar_credenciais_google
    GOOGLE_CREDS = carregar_credenciais_google()
except ImportError:
    GOOGLE_CREDS = {"credentials": None, "google_services": None}