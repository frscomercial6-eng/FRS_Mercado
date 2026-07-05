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
_AUX_SCHEMA_MIGRATED = False


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
        "ncm": "ALTER TABLE produtos ADD COLUMN ncm TEXT",
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
                    ncm TEXT,
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
                    id, codigo_barras, nome, variacao, ncm, preco_custo, margem_lucro, preco_venda,
                    quantidade_atual, quantidade_minima, validade, categoria,
                    preco_base, inicio_promocao, fim_promocao, imagem_path
                )
                SELECT
                    id,
                    codigo_barras,
                    nome,
                    variacao,
                    COALESCE(ncm, ''),
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


def _ensure_aux_schema(conn):
    """Garante tabelas auxiliares de cadastro e vínculos de fornecedores."""
    global _AUX_SCHEMA_MIGRATED
    if _AUX_SCHEMA_MIGRATED:
        return

    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            documento TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            data_cadastro DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS fornecedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cnpj_cpf TEXT,
            telefone TEXT,
            email TEXT,
            endereco TEXT,
            data_cadastro DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS fornecedor_produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fornecedor_id INTEGER NOT NULL,
            produto_id INTEGER NOT NULL,
            codigo_fornecedor TEXT,
            custo_compra_padrao REAL DEFAULT 0.0,
            data_vinculo DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (fornecedor_id, produto_id),
            FOREIGN KEY (fornecedor_id) REFERENCES fornecedores (id) ON DELETE CASCADE,
            FOREIGN KEY (produto_id) REFERENCES produtos (id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entradas'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(entradas)")
        colunas_entradas = {row[1] for row in cursor.fetchall()}
        if "fornecedor_id" not in colunas_entradas:
            cursor.execute("ALTER TABLE entradas ADD COLUMN fornecedor_id INTEGER")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vendas'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(vendas)")
        colunas_vendas = {row[1] for row in cursor.fetchall()}
        if "valor_impostos_retidos" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_impostos_retidos REAL NOT NULL DEFAULT 0.0")
        if "valor_liquido" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_liquido REAL NOT NULL DEFAULT 0.0")
        if "origem" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN origem TEXT NOT NULL DEFAULT 'LOJA_FISICA'")
        if "status_pedido" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN status_pedido TEXT NOT NULL DEFAULT 'APROVADO'")
        if "status_pagamento" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN status_pagamento TEXT NOT NULL DEFAULT 'PAGO'")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vendas_dia'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(vendas_dia)")
        colunas_vendas_dia = {row[1] for row in cursor.fetchall()}
        if "valor_impostos_retidos" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_impostos_retidos REAL NOT NULL DEFAULT 0.0")
        if "valor_liquido" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_liquido REAL NOT NULL DEFAULT 0.0")
        if "origem" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN origem TEXT NOT NULL DEFAULT 'LOJA_FISICA'")
        if "status_pedido" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN status_pedido TEXT NOT NULL DEFAULT 'APROVADO'")
        if "status_pagamento" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN status_pagamento TEXT NOT NULL DEFAULT 'PAGO'")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='financeiro'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(financeiro)")
        colunas_financeiro = {row[1] for row in cursor.fetchall()}
        if "valor_impostos_retidos" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_impostos_retidos REAL NOT NULL DEFAULT 0.0")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS config_aliquotas_ncm (
            ncm_prefixo TEXT PRIMARY KEY,
            aliquota_percentual REAL NOT NULL,
            descricao TEXT,
            ativo INTEGER NOT NULL DEFAULT 1,
            atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS orcamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_orcamento DATETIME DEFAULT CURRENT_TIMESTAMP,
            cliente_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ORCAMENTO',
            valor_total NUMERIC NOT NULL DEFAULT 0.0,
            valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0,
            valor_liquido NUMERIC NOT NULL DEFAULT 0.0,
            forma_pagamento TEXT,
            convertido_venda_id INTEGER,
            observacao TEXT,
            FOREIGN KEY (cliente_id) REFERENCES clientes (id),
            FOREIGN KEY (convertido_venda_id) REFERENCES vendas (id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS orcamento_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orcamento_id INTEGER NOT NULL,
            produto_id INTEGER,
            codigo_barras TEXT,
            descricao_produto TEXT NOT NULL,
            ncm TEXT,
            quantidade INTEGER NOT NULL,
            valor_unitario NUMERIC NOT NULL,
            subtotal NUMERIC NOT NULL,
            FOREIGN KEY (orcamento_id) REFERENCES orcamentos (id) ON DELETE CASCADE,
            FOREIGN KEY (produto_id) REFERENCES produtos (id)
        )
        """
    )

    cursor.execute("PRAGMA table_info(orcamentos)")
    colunas_orcamentos = {row[1] for row in cursor.fetchall()}
    if colunas_orcamentos:
        if "status" not in colunas_orcamentos:
            cursor.execute("ALTER TABLE orcamentos ADD COLUMN status TEXT NOT NULL DEFAULT 'ORCAMENTO'")
        if "valor_impostos_retidos" not in colunas_orcamentos:
            cursor.execute("ALTER TABLE orcamentos ADD COLUMN valor_impostos_retidos REAL NOT NULL DEFAULT 0.0")
        if "valor_liquido" not in colunas_orcamentos:
            cursor.execute("ALTER TABLE orcamentos ADD COLUMN valor_liquido REAL NOT NULL DEFAULT 0.0")
        if "forma_pagamento" not in colunas_orcamentos:
            cursor.execute("ALTER TABLE orcamentos ADD COLUMN forma_pagamento TEXT")
        if "convertido_venda_id" not in colunas_orcamentos:
            cursor.execute("ALTER TABLE orcamentos ADD COLUMN convertido_venda_id INTEGER")
        if "observacao" not in colunas_orcamentos:
            cursor.execute("ALTER TABLE orcamentos ADD COLUMN observacao TEXT")

    cursor.execute(
        """
        INSERT OR IGNORE INTO config_aliquotas_ncm (ncm_prefixo, aliquota_percentual, descricao, ativo)
        VALUES ('*', 0.0, 'Aliquota padrao/fallback', 1)
        """
    )

    _AUX_SCHEMA_MIGRATED = True

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
            _ensure_aux_schema(conn)
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
                    _ensure_produtos_schema(conn)
                    _ensure_aux_schema(conn)
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