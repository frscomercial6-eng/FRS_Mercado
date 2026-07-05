import os
import sqlite3
from database_manager import get_db_path


def _garantir_coluna_assinatura():
    """Adiciona a coluna assinatura em licenca sem remover dados existentes."""
    base_dir = os.path.dirname(__file__)
    app_db_path = get_db_path()
    candidatos = [
        app_db_path,
        os.path.join(base_dir, "database.db"),
        os.path.join(base_dir, "mercado.db"),
    ]

    for db_path in candidatos:
        if not os.path.exists(db_path):
            continue

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='licenca'")
            if not cur.fetchone():
                continue

            cur.execute("PRAGMA table_info(licenca)")
            colunas = [row[1] for row in cur.fetchall()]
            if "assinatura" not in colunas:
                cur.execute("ALTER TABLE licenca ADD COLUMN assinatura TEXT")
                conn.commit()
        finally:
            conn.close()


_garantir_coluna_assinatura()


def _garantir_coluna_caixa_operacao_id_sangrias():
    """Adiciona a coluna caixa_operacao_id em sangrias para fechamento por caixa."""
    base_dir = os.path.dirname(__file__)
    app_db_path = get_db_path()
    candidatos = [
        app_db_path,
        os.path.join(base_dir, "database.db"),
        os.path.join(base_dir, "mercado.db"),
    ]

    for db_path in candidatos:
        if not os.path.exists(db_path):
            continue

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sangrias'")
            if not cur.fetchone():
                continue

            cur.execute("PRAGMA table_info(sangrias)")
            colunas = [row[1] for row in cur.fetchall()]
            if "caixa_operacao_id" not in colunas:
                cur.execute("ALTER TABLE sangrias ADD COLUMN caixa_operacao_id INTEGER")
                conn.commit()
        finally:
            conn.close()


_garantir_coluna_caixa_operacao_id_sangrias()


def _garantir_tabela_config_sistema():
    """Garante tabela de configuração sistêmica e valor padrão de limite de caixa."""
    base_dir = os.path.dirname(__file__)
    app_db_path = get_db_path()
    candidatos = [
        app_db_path,
        os.path.join(base_dir, "database.db"),
        os.path.join(base_dir, "mercado.db"),
    ]

    for db_path in candidatos:
        if not os.path.exists(db_path):
            continue

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS config_sistema (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "INSERT OR IGNORE INTO config_sistema (chave, valor) VALUES ('limite_caixa', '500.00')"
            )
            conn.commit()
        finally:
            conn.close()


_garantir_tabela_config_sistema()


def _garantir_tabela_config_fiscal():
    """Garante tabela de configuração fiscal para integração PlugNotas."""
    base_dir = os.path.dirname(__file__)
    app_db_path = get_db_path()
    candidatos = [
        app_db_path,
        os.path.join(base_dir, "database.db"),
        os.path.join(base_dir, "mercado.db"),
    ]

    for db_path in candidatos:
        if not os.path.exists(db_path):
            continue

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS config_fiscal (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    api_key TEXT NOT NULL DEFAULT '',
                    ambiente TEXT NOT NULL DEFAULT 'HOMOLOGACAO',
                    webhook_token_hash TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("PRAGMA table_info(config_fiscal)")
            colunas = [row[1] for row in cur.fetchall()]
            if "webhook_token_hash" not in colunas:
                cur.execute("ALTER TABLE config_fiscal ADD COLUMN webhook_token_hash TEXT NOT NULL DEFAULT ''")
            cur.execute(
                "INSERT OR IGNORE INTO config_fiscal (id, api_key, ambiente, webhook_token_hash) VALUES (1, '', 'HOMOLOGACAO', '')"
            )
            conn.commit()
        finally:
            conn.close()


_garantir_tabela_config_fiscal()

def init_db():
    """
    Inicializa o banco de dados principal e cria as tabelas necessárias
    otimizadas para um sistema de PDV.
    """
    conn = None
    try:
        # Conecta ao arquivo de banco de dados (será criado se não existir)
        db_path = get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Habilita o suporte a chaves estrangeiras (desabilitado por padrão no SQLite)
        cursor.execute("PRAGMA foreign_keys = ON;")

        # Tabela de Produtos
        # O índice único no código de barras garante performance na leitura do scanner
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS produtos (
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
        ''')

        # Tabela de Usuários
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                cpf TEXT UNIQUE,
                senha_hash TEXT NOT NULL,
                salario NUMERIC DEFAULT 0.0,
                recebe_comissao BOOLEAN DEFAULT FALSE,
                porcentagem_comissao NUMERIC DEFAULT 0.0,
                permissao TEXT NOT NULL -- 'Administrador', 'Operador'
            )
        ''')

        # Tabela de Entradas (Estoque)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entradas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto_id INTEGER NOT NULL,
                fornecedor_id INTEGER,
                quantidade INTEGER NOT NULL,
                data_entrada DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (produto_id) REFERENCES produtos (id)
            )
        ''')
        cursor.execute("PRAGMA table_info(entradas)")
        entradas_cols = [row[1] for row in cursor.fetchall()]
        if "fornecedor_id" not in entradas_cols:
            cursor.execute("ALTER TABLE entradas ADD COLUMN fornecedor_id INTEGER")

        # Tabela de Clientes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                documento TEXT,
                telefone TEXT,
                email TEXT,
                endereco TEXT,
                data_cadastro DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Tabela de Fornecedores
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fornecedores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                cnpj_cpf TEXT,
                telefone TEXT,
                email TEXT,
                endereco TEXT,
                data_cadastro DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Vínculo de fornecedores com produtos comprados
        cursor.execute('''
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
        ''')

        # Tabela de Vendas (Cabeçalho)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vendas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_venda DATETIME DEFAULT CURRENT_TIMESTAMP,
                valor_total NUMERIC NOT NULL,
                valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0,
                valor_liquido NUMERIC NOT NULL DEFAULT 0.0,
                origem TEXT NOT NULL DEFAULT 'LOJA_FISICA',
                status_pedido TEXT NOT NULL DEFAULT 'APROVADO',
                status_pagamento TEXT NOT NULL DEFAULT 'PAGO',
                forma_pagamento TEXT NOT NULL
            )
        ''')
        cursor.execute("PRAGMA table_info(vendas)")
        vendas_cols = [row[1] for row in cursor.fetchall()]
        if "valor_impostos_retidos" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_liquido" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_liquido NUMERIC NOT NULL DEFAULT 0.0")
        if "origem" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN origem TEXT NOT NULL DEFAULT 'LOJA_FISICA'")
        if "status_pedido" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN status_pedido TEXT NOT NULL DEFAULT 'APROVADO'")
        if "status_pagamento" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN status_pagamento TEXT NOT NULL DEFAULT 'PAGO'")

        # Tabela de Orçamentos (Propostas Comerciais)
        cursor.execute('''
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
        ''')

        # Tabela de Itens do Orçamento
        cursor.execute('''
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
        ''')

        # Tabela de Controle de Abertura/Fechamento de Caixa
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS caixa_operacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_abertura DATETIME DEFAULT CURRENT_TIMESTAMP,
                data_fechamento DATETIME,
                saldo_inicial NUMERIC NOT NULL,
                status TEXT DEFAULT 'ABERTO' -- 'ABERTO' ou 'FECHADO'
            )
        ''')

        # Tabela de Sangrias (Retiradas de dinheiro)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sangrias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                valor NUMERIC NOT NULL,
                justificativa TEXT,
                data_sangria DATETIME DEFAULT CURRENT_TIMESTAMP,
                caixa_operacao_id INTEGER
            )
        ''')

        # Migração defensiva para bases antigas sem vínculo de caixa nas sangrias
        cursor.execute("PRAGMA table_info(sangrias)")
        sangrias_cols = [row[1] for row in cursor.fetchall()]
        if "caixa_operacao_id" not in sangrias_cols:
            cursor.execute("ALTER TABLE sangrias ADD COLUMN caixa_operacao_id INTEGER")

        # Tabela de Logs de Auditoria
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs_auditoria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                usuario_id INTEGER, -- Pode ser NULL se a ação não for vinculada a um usuário logado
                acao TEXT NOT NULL,
                status TEXT NOT NULL, -- 'Sucesso' ou 'Falha'
                detalhes TEXT
            )
        ''')
        # Tabela Temporária de Vendas do Dia (PDV Ágil)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vendas_dia (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_venda DATETIME DEFAULT CURRENT_TIMESTAMP,
                valor_total NUMERIC NOT NULL,
                valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0,
                valor_liquido NUMERIC NOT NULL DEFAULT 0.0,
                origem TEXT NOT NULL DEFAULT 'LOJA_FISICA',
                status_pedido TEXT NOT NULL DEFAULT 'APROVADO',
                status_pagamento TEXT NOT NULL DEFAULT 'PAGO',
                forma_pagamento TEXT NOT NULL
            )
        ''')
        cursor.execute("PRAGMA table_info(vendas_dia)")
        vendas_dia_cols = [row[1] for row in cursor.fetchall()]
        if "valor_impostos_retidos" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_liquido" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_liquido NUMERIC NOT NULL DEFAULT 0.0")
        if "origem" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN origem TEXT NOT NULL DEFAULT 'LOJA_FISICA'")
        if "status_pedido" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN status_pedido TEXT NOT NULL DEFAULT 'APROVADO'")
        if "status_pagamento" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN status_pagamento TEXT NOT NULL DEFAULT 'PAGO'")

        # Tabela de Licenciamento
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS licenca (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_expiracao DATE NOT NULL,
                hwid1 TEXT,
                hwid2 TEXT,
                assinatura TEXT
            )
        ''')

        # Tabela de Configuração de Taxas
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config_taxas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT UNIQUE NOT NULL, -- 'DEBITO' ou 'CREDITO'
                percentual NUMERIC NOT NULL DEFAULT 0.0
            )
        ''')

        # Tabela Financeiro (Consolidado)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS financeiro (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_registro DATETIME DEFAULT CURRENT_TIMESTAMP,
                valor NUMERIC NOT NULL,
                tipo TEXT NOT NULL, -- 'Entrada' ou 'Saída'
                valor_bruto NUMERIC,
                valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0,
                taxa_aplicada NUMERIC,
                descricao TEXT
            )
        ''')
        cursor.execute("PRAGMA table_info(financeiro)")
        financeiro_cols = [row[1] for row in cursor.fetchall()]
        if "valor_impostos_retidos" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_impostos_retidos NUMERIC NOT NULL DEFAULT 0.0")

        # Configuração de alíquotas de retenção por NCM (SPED/tributário)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config_aliquotas_ncm (
                ncm_prefixo TEXT PRIMARY KEY,
                aliquota_percentual REAL NOT NULL,
                descricao TEXT,
                ativo INTEGER NOT NULL DEFAULT 1,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Tabela de configurações gerais persistidas em banco
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config_sistema (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
        ''')

        # Tabela de configuração fiscal (PlugNotas)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config_fiscal (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                api_key TEXT NOT NULL DEFAULT '',
                ambiente TEXT NOT NULL DEFAULT 'HOMOLOGACAO',
                webhook_token_hash TEXT NOT NULL DEFAULT ''
            )
        ''')
        cursor.execute("PRAGMA table_info(config_fiscal)")
        fiscal_cols = [row[1] for row in cursor.fetchall()]
        if "webhook_token_hash" not in fiscal_cols:
            cursor.execute("ALTER TABLE config_fiscal ADD COLUMN webhook_token_hash TEXT NOT NULL DEFAULT ''")

        # Tabela de Itens da Venda (Detalhes)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS itens_venda (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venda_id INTEGER NOT NULL,
                produto_id INTEGER NOT NULL,
                quantidade INTEGER NOT NULL,
                subtotal NUMERIC NOT NULL,
                FOREIGN KEY (venda_id) REFERENCES vendas (id),
                FOREIGN KEY (produto_id) REFERENCES produtos (id)
            )
        ''')

        # Tabela de Histórico da IA Mentora
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs_mentoria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                conselho TEXT NOT NULL
            )
        ''')

        # Inserção inicial de taxas se não existirem
        cursor.execute("INSERT OR IGNORE INTO config_taxas (tipo, percentual) VALUES ('DEBITO', 0.0)")
        cursor.execute("INSERT OR IGNORE INTO config_taxas (tipo, percentual) VALUES ('CREDITO', 0.0)")
        cursor.execute("INSERT OR IGNORE INTO config_sistema (chave, valor) VALUES ('limite_caixa', '500.00')")
        cursor.execute("INSERT OR IGNORE INTO config_fiscal (id, api_key, ambiente, webhook_token_hash) VALUES (1, '', 'HOMOLOGACAO', '')")
        cursor.execute(
            "INSERT OR IGNORE INTO config_aliquotas_ncm (ncm_prefixo, aliquota_percentual, descricao, ativo) VALUES ('*', 0.0, 'Aliquota padrao/fallback', 1)"
        )

        conn.commit()
        print("Banco de dados inicializado com sucesso.")
    except sqlite3.Error as e:
        print(f"Erro ao inicializar o banco de dados: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    init_db()