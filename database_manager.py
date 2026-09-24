import sqlite3
import os
import re
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
_ITENS_VENDA_SCHEMA_MIGRATED = False


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
        "quantidade_atual": "ALTER TABLE produtos ADD COLUMN quantidade_atual NUMERIC NOT NULL DEFAULT 0",
        "quantidade_minima": "ALTER TABLE produtos ADD COLUMN quantidade_minima NUMERIC NOT NULL DEFAULT 0",
        "variacao": "ALTER TABLE produtos ADD COLUMN variacao TEXT",
        "unidade": "ALTER TABLE produtos ADD COLUMN unidade TEXT NOT NULL DEFAULT 'UN'",
        "ncm": "ALTER TABLE produtos ADD COLUMN ncm TEXT",
        "aliquota_icms": "ALTER TABLE produtos ADD COLUMN aliquota_icms REAL NOT NULL DEFAULT 0.0",
        "aliquota_pis": "ALTER TABLE produtos ADD COLUMN aliquota_pis REAL NOT NULL DEFAULT 0.0",
        "aliquota_cofins": "ALTER TABLE produtos ADD COLUMN aliquota_cofins REAL NOT NULL DEFAULT 0.0",
        "aliquota_ibs": "ALTER TABLE produtos ADD COLUMN aliquota_ibs REAL NOT NULL DEFAULT 0.0",
        "aliquota_cbs": "ALTER TABLE produtos ADD COLUMN aliquota_cbs REAL NOT NULL DEFAULT 0.0",
    }

    for coluna, ddl in alteracoes.items():
        if coluna not in colunas:
            cursor.execute(ddl)

    # FASE 1 UN/KG: backfill defensivo da unidade, sem sobrescrever o que existe.
    # Só preenche unidade ausente/nula; KG conforme convenção legada
    # (variacao/categoria 'kg' ou nome terminando em 'kg'), demais UN.
    cursor.execute(
        "UPDATE produtos SET unidade = 'KG' WHERE (unidade IS NULL OR TRIM(unidade) = '') "
        "AND (LOWER(COALESCE(variacao,'')) = 'kg' OR LOWER(COALESCE(categoria,'')) = 'kg' "
        "OR LOWER(TRIM(nome)) LIKE '%kg')"
    )
    cursor.execute(
        "UPDATE produtos SET unidade = 'UN' WHERE unidade IS NULL OR TRIM(unidade) = ''"
    )

    cursor.execute("PRAGMA table_info(produtos)")
    tipos_por_coluna = {row[1]: (row[2] or "").upper() for row in cursor.fetchall()}
    # FASE 1 UN/KG: quantidade_atual precisa aceitar peso decimal (1.250 KG).
    # Afinidade INTEGER em bancos legados truncaria/roundaria o valor — a mesma
    # recriação transacional abaixo corrige o tipo preservando todos os dados.
    tipos_decimais = ("NUMERIC", "REAL", "DOUBLE", "FLOAT")
    requer_migracao_tipos = (
        tipos_por_coluna.get("preco_custo") != "REAL"
        or tipos_por_coluna.get("preco_venda") != "REAL"
        or tipos_por_coluna.get("quantidade_atual") not in tipos_decimais
        or tipos_por_coluna.get("quantidade_minima") not in tipos_decimais
    )

    if requer_migracao_tipos:
        # PRAGMA foreign_keys e no-op dentro de transacao: os UPDATE de backfill
        # acima abriram transacao implicita (sqlite3 modo legado). Sem este commit
        # o OFF abaixo nao tem efeito e o DROP TABLE produtos falha com
        # "FOREIGN KEY constraint failed" em bases com linhas em itens_venda.
        conn.commit()

        # Preserva o AUTOINCREMENT original: DROP/RENAME zeram o sqlite_sequence
        # de produtos e permitiriam reutilizar IDs ja emitidos no passado.
        seq_produtos = None
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        )
        if cursor.fetchone():
            cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = 'produtos'")
            linha_seq = cursor.fetchone()
            if linha_seq:
                seq_produtos = linha_seq[0]

        cursor.execute("SELECT COUNT(*) FROM produtos")
        total_antes = cursor.fetchone()[0]

        conn.execute("PRAGMA foreign_keys = OFF;")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            conn.execute("PRAGMA foreign_keys = ON;")
            raise sqlite3.DatabaseError(
                "Migracao produtos abortada: PRAGMA foreign_keys = OFF nao foi aplicado"
            )

        try:
            conn.execute("BEGIN")
            cursor.execute("DROP TABLE IF EXISTS produtos_schema_tmp")
            cursor.execute(
                """
                CREATE TABLE produtos_schema_tmp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    codigo_barras TEXT UNIQUE NOT NULL,
                    nome TEXT NOT NULL,
                    variacao TEXT,
                    unidade TEXT NOT NULL DEFAULT 'UN',
                    ncm TEXT,
                    aliquota_icms REAL NOT NULL DEFAULT 0.0,
                    aliquota_pis REAL NOT NULL DEFAULT 0.0,
                    aliquota_cofins REAL NOT NULL DEFAULT 0.0,
                    aliquota_ibs REAL NOT NULL DEFAULT 0.0,
                    aliquota_cbs REAL NOT NULL DEFAULT 0.0,
                    preco_custo REAL NOT NULL,
                    margem_lucro REAL NOT NULL DEFAULT 0.0,
                    preco_venda REAL NOT NULL,
                    quantidade_atual NUMERIC NOT NULL DEFAULT 0,
                    quantidade_minima NUMERIC NOT NULL DEFAULT 0,
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
                    id, codigo_barras, nome, variacao, unidade, ncm,
                    aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs,
                    preco_custo, margem_lucro, preco_venda,
                    quantidade_atual, quantidade_minima, validade, categoria,
                    preco_base, inicio_promocao, fim_promocao, imagem_path
                )
                SELECT
                    id,
                    codigo_barras,
                    nome,
                    variacao,
                    COALESCE(NULLIF(TRIM(CAST(unidade AS TEXT)), ''), 'UN'),
                    COALESCE(ncm, ''),
                    CAST(COALESCE(aliquota_icms, 0.0) AS REAL),
                    CAST(COALESCE(aliquota_pis, 0.0) AS REAL),
                    CAST(COALESCE(aliquota_cofins, 0.0) AS REAL),
                    CAST(COALESCE(aliquota_ibs, 0.0) AS REAL),
                    CAST(COALESCE(aliquota_cbs, 0.0) AS REAL),
                    CAST(COALESCE(preco_custo, 0.0) AS REAL),
                    CAST(COALESCE(margem_lucro, 0.0) AS REAL),
                    CAST(COALESCE(preco_venda, 0.0) AS REAL),
                    CAST(COALESCE(quantidade_atual, 0) AS NUMERIC),
                    CAST(COALESCE(quantidade_minima, 0) AS NUMERIC),
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

            cursor.execute("SELECT COUNT(*) FROM produtos")
            total_depois = cursor.fetchone()[0]
            if total_antes != total_depois:
                raise sqlite3.DatabaseError(
                    f"Migracao produtos abortada: contagem divergente "
                    f"({total_antes} antes, {total_depois} depois)"
                )

            # Restaura o AUTOINCREMENT capturado antes da reconstrucao.
            if seq_produtos is not None:
                cursor.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = 'produtos'",
                    (seq_produtos,),
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        "INSERT INTO sqlite_sequence (name, seq) VALUES ('produtos', ?)",
                        (seq_produtos,),
                    )

            orfaos = cursor.execute("PRAGMA foreign_key_check").fetchall()
            if orfaos:
                raise sqlite3.DatabaseError(
                    "Migracao produtos abortada: foreign_key_check retornou "
                    f"{len(orfaos)} violacao(oes)"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")

        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise sqlite3.DatabaseError(
                "Migracao produtos: PRAGMA foreign_keys nao retornou para ON"
            )

        _PRODUTOS_SCHEMA_MIGRATED = True


def _migrar_itens_venda_quantidade_numeric(conn):
    """Garante itens_venda.quantidade com afinidade decimal (FASE 1 UN/KG).

    Bancos legados criaram itens_venda.quantidade como INTEGER: peso KG
    (ex.: 1.250) seria truncado para 1, corrompendo histórico e relatórios.
    A correção exige recriação transacional da tabela (SQLite não altera
    afinidade de coluna via ALTER). Preserva todas as colunas e dados; a
    tabela não possui índices próprios nem é referenciada por FK de terceiros.

    Retorna True quando houve migração, False quando nada era necessário.
    """
    global _ITENS_VENDA_SCHEMA_MIGRATED
    if _ITENS_VENDA_SCHEMA_MIGRATED:
        return False

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='itens_venda'")
    if not cursor.fetchone():
        _ITENS_VENDA_SCHEMA_MIGRATED = True
        return False

    cursor.execute("PRAGMA table_info(itens_venda)")
    tipos = {row[1]: (row[2] or "").upper() for row in cursor.fetchall()}
    tipos_decimais = ("NUMERIC", "REAL", "DOUBLE", "FLOAT")
    if tipos.get("quantidade") in tipos_decimais:
        _ITENS_VENDA_SCHEMA_MIGRATED = True
        return False

    colunas_destino = [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("venda_id", "INTEGER NOT NULL"),
        ("produto_id", "INTEGER NOT NULL"),
        ("quantidade", "NUMERIC NOT NULL"),
        ("subtotal", "NUMERIC NOT NULL"),
        ("regime_tributario", "TEXT NOT NULL DEFAULT 'ATUAL'"),
        ("aliquota_icms", "REAL NOT NULL DEFAULT 0.0"),
        ("aliquota_pis", "REAL NOT NULL DEFAULT 0.0"),
        ("aliquota_cofins", "REAL NOT NULL DEFAULT 0.0"),
        ("aliquota_ibs", "REAL NOT NULL DEFAULT 0.0"),
        ("aliquota_cbs", "REAL NOT NULL DEFAULT 0.0"),
        ("valor_icms", "REAL NOT NULL DEFAULT 0.0"),
        ("valor_pis", "REAL NOT NULL DEFAULT 0.0"),
        ("valor_cofins", "REAL NOT NULL DEFAULT 0.0"),
        ("valor_ibs", "REAL NOT NULL DEFAULT 0.0"),
        ("valor_cbs", "REAL NOT NULL DEFAULT 0.0"),
        ("lote_id", "INTEGER"),
        ("quantidade_lote", "REAL NOT NULL DEFAULT 0.0"),
    ]
    # Só copia colunas que existem na origem (bases muito antigas podem não ter
    # todas as colunas fiscais); as ausentes ficam com o DEFAULT do destino.
    colunas_origem = [nome for nome, _ in colunas_destino if nome in tipos]
    lista_colunas = ", ".join(colunas_origem)

    cursor.execute("SELECT COUNT(*) FROM itens_venda")
    total_antes = cursor.fetchone()[0]

    # Preserva o AUTOINCREMENT original: DROP/RENAME zeram o sqlite_sequence
    # de itens_venda e permitiriam reutilizar IDs ja emitidos no passado.
    seq_itens_venda = None
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    )
    if cursor.fetchone():
        cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = 'itens_venda'")
        linha_seq_itens_venda = cursor.fetchone()
        if linha_seq_itens_venda:
            seq_itens_venda = linha_seq_itens_venda[0]

    # PRAGMA foreign_keys e no-op dentro de transacao: garante autocommit antes
    # do OFF, caso contrario o DROP abaixo executaria com FK ativa.
    conn.commit()

    conn.execute("PRAGMA foreign_keys = OFF;")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        conn.execute("PRAGMA foreign_keys = ON;")
        raise sqlite3.DatabaseError(
            "Migracao itens_venda abortada: PRAGMA foreign_keys = OFF nao foi aplicado"
        )
    try:
        conn.execute("BEGIN")
        cursor.execute("DROP TABLE IF EXISTS itens_venda_schema_tmp")
        cursor.execute(
            "CREATE TABLE itens_venda_schema_tmp ("
            + ", ".join(f"{nome} {tipo}" for nome, tipo in colunas_destino)
            + ", FOREIGN KEY (venda_id) REFERENCES vendas (id),"
            + " FOREIGN KEY (produto_id) REFERENCES produtos (id))"
        )
        cursor.execute(
            f"INSERT INTO itens_venda_schema_tmp ({lista_colunas}) "
            f"SELECT {lista_colunas} FROM itens_venda"
        )
        cursor.execute("DROP TABLE itens_venda")
        cursor.execute("ALTER TABLE itens_venda_schema_tmp RENAME TO itens_venda")
        cursor.execute("SELECT COUNT(*) FROM itens_venda")
        total_depois = cursor.fetchone()[0]
        if total_antes != total_depois:
            raise sqlite3.DatabaseError(
                f"Migracao itens_venda abortada: contagem divergente "
                f"({total_antes} antes, {total_depois} depois)"
            )

        # Restaura o AUTOINCREMENT capturado antes da reconstrucao.
        if seq_itens_venda is not None:
            cursor.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'itens_venda'",
                (seq_itens_venda,),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('itens_venda', ?)",
                    (seq_itens_venda,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")

    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError(
            "Migracao itens_venda: PRAGMA foreign_keys nao retornou para ON"
        )

    _ITENS_VENDA_SCHEMA_MIGRATED = True
    return True


def _garantir_colunas_caixa_conferencia(cursor):
    """Adiciona colunas da conferência de caixa em bases legadas.

    Garante que ``caixa_conferencia`` possua todas as colunas referenciadas
    pelo fechamento de caixa (valor_calculado, valor_sistema, valor_informado,
    diferenca, data_registro). Cada ALTER é condicional: apenas se a coluna
    ainda não existir. É idempotente e não remove dados existentes.

    Corrige a falha ``table caixa_conferencia has no column named valor_calculado``
    em bancos antigos onde a tabela foi criada por versões anteriores, antes da
    adição de ``valor_calculado``.
    """
    # Tabela inexistente é criada logo depois pelo CREATE TABLE IF NOT EXISTS
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='caixa_conferencia'"
    )
    if not cursor.fetchone():
        return

    cursor.execute("PRAGMA table_info(caixa_conferencia)")
    colunas = {row[1] for row in cursor.fetchall()}

    alteracoes = {
        "valor_calculado": "ALTER TABLE caixa_conferencia ADD COLUMN valor_calculado NUMERIC NOT NULL DEFAULT 0.0",
        "valor_sistema": "ALTER TABLE caixa_conferencia ADD COLUMN valor_sistema NUMERIC NOT NULL DEFAULT 0.0",
        "valor_informado": "ALTER TABLE caixa_conferencia ADD COLUMN valor_informado NUMERIC NOT NULL DEFAULT 0.0",
        "diferenca": "ALTER TABLE caixa_conferencia ADD COLUMN diferenca NUMERIC NOT NULL DEFAULT 0.0",
        # SQLite recusa DEFAULT CURRENT_TIMESTAMP em ALTER TABLE ADD COLUMN
        # ("Cannot add a column with non-constant default"). Em bases legadas a
        # coluna é adicionada sem default (nullable); bases novas já têm
        # CURRENT_TIMESTAMP definido pelo próprio CREATE TABLE.
        "data_registro": "ALTER TABLE caixa_conferencia ADD COLUMN data_registro DATETIME",
    }
    for coluna, ddl in alteracoes.items():
        if coluna not in colunas:
            cursor.execute(ddl)


def _migrar_cliente_id_nullable_orcamentos(conn):
    """Permite orçamento sem cliente sem apagar ou reconstruir dados de vendas.

    Bases antigas criaram ``orcamentos.cliente_id INTEGER NOT NULL``. SQLite
    não oferece ALTER para remover essa restrição; por isso a migração troca
    somente o schema da tabela, copiando todas as linhas e o AUTOINCREMENT.
    Não é backfill: valores históricos são preservados exatamente.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orcamentos'")
    if not cursor.fetchone():
        return False

    cursor.execute("PRAGMA table_info(orcamentos)")
    info = cursor.fetchall()
    if not info:
        return False
    cliente_info = next((row for row in info if row[1] == "cliente_id"), None)
    if cliente_info is None or not int(cliente_info[3] or 0):
        return False

    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='orcamentos'")
    sql_row = cursor.fetchone()
    if not sql_row or not sql_row[0]:
        return False
    sql_original = str(sql_row[0]).strip().rstrip(";")
    sql_novo, substitutions = re.subn(
        r"(?i)(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)([\"`]?orcamentos[\"`]?)(?=\s*\()",
        lambda match: f"{match.group(1)}orcamentos__schema_tmp",
        sql_original,
        count=1,
    )
    sql_novo, removals = re.subn(
        r"(?i)(\bcliente_id\s+INTEGER\s+)NOT\s+NULL",
        r"\1",
        sql_novo,
        count=1,
    )
    if substitutions != 1 or removals != 1:
        return False

    colunas = [row[1] for row in info]
    colunas_sql = ", ".join(f'"{coluna}"' for coluna in colunas)
    colunas_origem = ", ".join(f'"{coluna}"' for coluna in colunas)
    seq_row = None
    try:
        cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='orcamentos'")
        seq_row = cursor.fetchone()
    except sqlite3.Error:
        seq_row = None

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        conn.execute("PRAGMA foreign_keys=ON")
        raise sqlite3.DatabaseError("Não foi possível desabilitar FKs para migração de orçamentos.")
    try:
        conn.execute("BEGIN")
        conn.execute(sql_novo)
        conn.execute(
            f"INSERT INTO orcamentos__schema_tmp ({colunas_sql}) "
            f"SELECT {colunas_origem} FROM orcamentos"
        )
        conn.execute("DROP TABLE orcamentos")
        conn.execute("ALTER TABLE orcamentos__schema_tmp RENAME TO orcamentos")
        if seq_row is not None:
            conn.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                ("orcamentos", int(seq_row[0] or 0)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True


def _garantir_coluna_unidade_orcamento_itens(conn):
    """Adiciona a unidade ao item de orçamento sem preencher históricos."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orcamento_itens'")
    if not cursor.fetchone():
        return False
    cursor.execute("PRAGMA table_info(orcamento_itens)")
    colunas = {row[1] for row in cursor.fetchall()}
    if "unidade" in colunas:
        return False
    cursor.execute("ALTER TABLE orcamento_itens ADD COLUMN unidade TEXT")
    return True


def _garantir_schema_vales(conn):
    """Cria o modelo mínimo e rastreável de Vale, sem tocar em históricos."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero INTEGER NOT NULL UNIQUE,
            cliente_id INTEGER NOT NULL,
            data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'PENDENTE',
            data_quitacao DATETIME,
            venda_id INTEGER,
            caixa_operacao_id INTEGER,
            forma_pagamento TEXT,
            total NUMERIC NOT NULL DEFAULT 0.0,
            FOREIGN KEY (cliente_id) REFERENCES clientes (id),
            FOREIGN KEY (venda_id) REFERENCES vendas (id),
            FOREIGN KEY (caixa_operacao_id) REFERENCES caixa_operacao (id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vale_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vale_id INTEGER NOT NULL,
            produto_id INTEGER,
            codigo_barras TEXT,
            descricao_produto TEXT NOT NULL,
            ncm TEXT,
            quantidade NUMERIC NOT NULL,
            unidade TEXT,
            preco_unitario NUMERIC NOT NULL,
            subtotal NUMERIC NOT NULL,
            FOREIGN KEY (vale_id) REFERENCES vales (id) ON DELETE CASCADE,
            FOREIGN KEY (produto_id) REFERENCES produtos (id)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_vales_cliente_status ON vales (cliente_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_vale_itens_vale ON vale_itens (vale_id)")


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
        if "lote_id" not in colunas_entradas:
            cursor.execute("ALTER TABLE entradas ADD COLUMN lote_id INTEGER")

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
        if "valor_icms" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_icms REAL NOT NULL DEFAULT 0.0")
        if "valor_pis" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_pis REAL NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_cofins REAL NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_ibs REAL NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_cbs REAL NOT NULL DEFAULT 0.0")
        if "valor_informado" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_informado REAL NOT NULL DEFAULT 0.0")
        if "diferenca" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN diferenca REAL NOT NULL DEFAULT 0.0")
        if "caixa_operacao_id" not in colunas_vendas:
            cursor.execute("ALTER TABLE vendas ADD COLUMN caixa_operacao_id INTEGER")

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
        if "valor_icms" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_icms REAL NOT NULL DEFAULT 0.0")
        if "valor_pis" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_pis REAL NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_cofins REAL NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_ibs REAL NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_cbs REAL NOT NULL DEFAULT 0.0")
        if "valor_informado" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_informado REAL NOT NULL DEFAULT 0.0")
        if "diferenca" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN diferenca REAL NOT NULL DEFAULT 0.0")
        if "caixa_operacao_id" not in colunas_vendas_dia:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN caixa_operacao_id INTEGER")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='financeiro'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(financeiro)")
        colunas_financeiro = {row[1] for row in cursor.fetchall()}
        if "valor_impostos_retidos" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_impostos_retidos REAL NOT NULL DEFAULT 0.0")
        if "valor_icms" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_icms REAL NOT NULL DEFAULT 0.0")
        if "valor_pis" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_pis REAL NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_cofins REAL NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_ibs REAL NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_cbs REAL NOT NULL DEFAULT 0.0")
        if "caixa_operacao_id" not in colunas_financeiro:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN caixa_operacao_id INTEGER")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='itens_venda'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(itens_venda)")
        colunas_itens = {row[1] for row in cursor.fetchall()}
        if "regime_tributario" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN regime_tributario TEXT NOT NULL DEFAULT 'ATUAL'")
        if "aliquota_icms" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_icms REAL NOT NULL DEFAULT 0.0")
        if "aliquota_pis" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_pis REAL NOT NULL DEFAULT 0.0")
        if "aliquota_cofins" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_cofins REAL NOT NULL DEFAULT 0.0")
        if "aliquota_ibs" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_ibs REAL NOT NULL DEFAULT 0.0")
        if "aliquota_cbs" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_cbs REAL NOT NULL DEFAULT 0.0")
        if "valor_icms" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_icms REAL NOT NULL DEFAULT 0.0")
        if "valor_pis" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_pis REAL NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_cofins REAL NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_ibs REAL NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_cbs REAL NOT NULL DEFAULT 0.0")
        if "lote_id" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN lote_id INTEGER")
        if "quantidade_lote" not in colunas_itens:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN quantidade_lote REAL NOT NULL DEFAULT 0.0")

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
        CREATE TABLE IF NOT EXISTS config_aliquotas_fiscais_ncm (
            ncm_prefixo TEXT PRIMARY KEY,
            aliquota_icms REAL NOT NULL DEFAULT 0.0,
            aliquota_pis REAL NOT NULL DEFAULT 0.0,
            aliquota_cofins REAL NOT NULL DEFAULT 0.0,
            aliquota_ibs REAL NOT NULL DEFAULT 0.0,
            aliquota_cbs REAL NOT NULL DEFAULT 0.0,
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
            cliente_id INTEGER,
            status TEXT NOT NULL DEFAULT 'ORCAMENTO',
            valor_total NUMERIC NOT NULL DEFAULT 0.0,
            valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0,
            valor_liquido NUMERIC NOT NULL DEFAULT 0.0,
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
            quantidade NUMERIC NOT NULL,
            unidade TEXT,
            valor_unitario NUMERIC NOT NULL,
            subtotal NUMERIC NOT NULL,
            FOREIGN KEY (orcamento_id) REFERENCES orcamentos (id) ON DELETE CASCADE,
            FOREIGN KEY (produto_id) REFERENCES produtos (id)
        )
        """
    )

    _migrar_cliente_id_nullable_orcamentos(conn)
    _garantir_coluna_unidade_orcamento_itens(conn)

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
    cursor.execute(
        """
        INSERT OR IGNORE INTO config_aliquotas_fiscais_ncm (
            ncm_prefixo, aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs, descricao, ativo
        )
        VALUES ('*', 0.0, 0.0, 0.0, 0.0, 0.0, 'Aliquotas fiscais padrao/fallback', 1)
        """
    )

        # Conferência analítica do fechamento de caixa (uma linha por modalidade).
    # Criada aqui (e não só em init_db) para existir também em bases legadas.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS caixa_conferencia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caixa_operacao_id INTEGER NOT NULL,
            modalidade TEXT NOT NULL,
            valor_calculado NUMERIC NOT NULL DEFAULT 0.0,
            valor_sistema NUMERIC NOT NULL DEFAULT 0.0,
            valor_informado NUMERIC NOT NULL DEFAULT 0.0,
            diferenca NUMERIC NOT NULL DEFAULT 0.0,
            data_registro DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Migração idempotente para bases legadas que possuem caixa_conferencia
    # criada por versões anteriores (sem valor_calculado). SQLite não adiciona
    # colunas via CREATE TABLE IF NOT EXISTS quando a tabela já existe, por isso
    # cada coluna é garantida individualmente com ALTER TABLE. Isso corrige a
    # falha "table caixa_conferencia has no column named valor_calculado" no
    # fechamento de caixa sem remover nenhum dado existente.
    _garantir_colunas_caixa_conferencia(cursor)
    _garantir_schema_vales(conn)

    # Lotes de produtos (rastro da NF-e: nLote/qLote/dFab/dVal por item).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS produto_lotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER,
            codigo_barras TEXT,
            numero_lote TEXT,
            quantidade NUMERIC NOT NULL DEFAULT 0.0,
            data_fabricacao TEXT,
            data_validade TEXT,
            chave_nfe TEXT,
            origem TEXT DEFAULT 'NF-e',
            data_registro DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Índices de rastreabilidade/FEFO para lotes (idempotentes).
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lotes_produto_numero_chave ON produto_lotes (produto_id, numero_lote, chave_nfe)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_lotes_produto_id ON produto_lotes (produto_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_lotes_data_validade ON produto_lotes (data_validade)"
    )

    # Triggers para assegurar que nenhum lote fique com quantidade negativa (T38)
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_lotes_nao_negativo_ins
        BEFORE INSERT ON produto_lotes
        FOR EACH ROW
        WHEN NEW.quantidade < 0
        BEGIN
            SELECT RAISE(ABORT, 'Quantidade de lote não pode ser negativa (T38)');
        END;
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_lotes_nao_negativo_upd
        BEFORE UPDATE OF quantidade ON produto_lotes
        FOR EACH ROW
        WHEN NEW.quantidade < 0
        BEGIN
            SELECT RAISE(ABORT, 'Quantidade de lote não pode ser negativa (T38)');
        END;
        """
    )

    # Garante a modalidade VOUCHER na tabela de taxas em bases já existentes.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config_taxas'")
    if cursor.fetchone():
        cursor.execute("INSERT OR IGNORE INTO config_taxas (tipo, percentual) VALUES ('VOUCHER', 0.0)")

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
            _migrar_itens_venda_quantidade_numeric(conn)
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
    except Exception as e:
        if conn:
            try:
                conn.rollback() # Rollback automático em caso de erro
            except Exception:
                pass
        if isinstance(e, sqlite3.Error):
            print(f"Erro no banco de dados: {e}")
        raise # Re-lança a exceção para que o chamador possa tratá-la
    finally:
        if conn:
            conn.close()

def registrar_log(usuario_id, acao, status, detalhes=None, *, conn=None):
    """Registra auditoria; com conn, participa da transação e propaga falhas."""
    sql = "INSERT INTO logs_auditoria (timestamp, usuario_id, acao, status, detalhes) VALUES (?, ?, ?, ?, ?)"
    valores = (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario_id, acao, status, detalhes)
    if conn is not None:
        conn.execute(sql, valores)
        return

    try:
        with get_db_connection() as conexao:
            conexao.execute(sql, valores)
    except Exception as e:
        print(f"Erro ao registrar log de auditoria: {e}")

# Inicializa a localização das credenciais do Google para uso em backups e serviços externos
try:
    from modulo_config import carregar_credenciais_google
    GOOGLE_CREDS = carregar_credenciais_google()
except ImportError:
    GOOGLE_CREDS = {"credentials": None, "google_services": None}