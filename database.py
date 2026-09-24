import os
import sqlite3
from database_manager import get_db_path


def _garantir_coluna_assinatura():
    """Adiciona a coluna assinatura em licenca sem remover dados existentes."""
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='licenca'")
        if not cur.fetchone():
            return

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
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sangrias'")
        if not cur.fetchone():
            return

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
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return

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
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return

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
        ''')
        cursor.execute("PRAGMA table_info(produtos)")
        produtos_cols = [row[1] for row in cursor.fetchall()]
        if "aliquota_icms" not in produtos_cols:
            cursor.execute("ALTER TABLE produtos ADD COLUMN aliquota_icms REAL NOT NULL DEFAULT 0.0")
        if "aliquota_pis" not in produtos_cols:
            cursor.execute("ALTER TABLE produtos ADD COLUMN aliquota_pis REAL NOT NULL DEFAULT 0.0")
        if "aliquota_cofins" not in produtos_cols:
            cursor.execute("ALTER TABLE produtos ADD COLUMN aliquota_cofins REAL NOT NULL DEFAULT 0.0")
        if "aliquota_ibs" not in produtos_cols:
            cursor.execute("ALTER TABLE produtos ADD COLUMN aliquota_ibs REAL NOT NULL DEFAULT 0.0")
        if "aliquota_cbs" not in produtos_cols:
            cursor.execute("ALTER TABLE produtos ADD COLUMN aliquota_cbs REAL NOT NULL DEFAULT 0.0")
        if "unidade" not in produtos_cols:
            cursor.execute("ALTER TABLE produtos ADD COLUMN unidade TEXT NOT NULL DEFAULT 'UN'")
        # Backfill legado: preenche unidade ausente sem sobrescrever valores existentes.
        cursor.execute(
            "UPDATE produtos SET unidade = 'KG' WHERE (unidade IS NULL OR TRIM(unidade) = '') "
            "AND (LOWER(COALESCE(variacao,'')) = 'kg' OR LOWER(COALESCE(categoria,'')) = 'kg' "
            "OR LOWER(TRIM(nome)) LIKE '%kg')"
        )
        cursor.execute(
            "UPDATE produtos SET unidade = 'UN' WHERE unidade IS NULL OR TRIM(unidade) = ''"
        )

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
                lote_id INTEGER,
                FOREIGN KEY (produto_id) REFERENCES produtos (id)
            )
        ''')
        cursor.execute("PRAGMA table_info(entradas)")
        entradas_cols = [row[1] for row in cursor.fetchall()]
        if "fornecedor_id" not in entradas_cols:
            cursor.execute("ALTER TABLE entradas ADD COLUMN fornecedor_id INTEGER")
        if "lote_id" not in entradas_cols:
            cursor.execute("ALTER TABLE entradas ADD COLUMN lote_id INTEGER")

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
                forma_pagamento TEXT NOT NULL,
                valor_informado NUMERIC NOT NULL DEFAULT 0.0,
                diferenca NUMERIC NOT NULL DEFAULT 0.0
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
        if "valor_icms" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_icms NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_pis" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_pis NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_cofins NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_ibs NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_cbs NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_informado" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN valor_informado NUMERIC NOT NULL DEFAULT 0.0")
        if "diferenca" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN diferenca NUMERIC NOT NULL DEFAULT 0.0")

        # Garantir coluna caixa_operacao_id em vendas
        if "caixa_operacao_id" not in vendas_cols:
            cursor.execute("ALTER TABLE vendas ADD COLUMN caixa_operacao_id INTEGER")

        # Tabela de Orçamentos (Propostas Comerciais)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orcamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_orcamento DATETIME DEFAULT CURRENT_TIMESTAMP,
                cliente_id INTEGER,
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
                quantidade NUMERIC NOT NULL,
                unidade TEXT,
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

        # Vale é um documento separado do orçamento comercial. O registro
        # permanece PENDENTE até ser quitado por uma venda normal do PDV.
        cursor.execute('''
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
        ''')
        cursor.execute('''
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
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vales_cliente_status ON vales (cliente_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vale_itens_vale ON vale_itens (vale_id)")

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

        ############################################################
        # Conferência analítica do fechamento de caixa (uma linha por modalidade)
        cursor.execute('''
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
        ''')

        # Defensivo idempotente para bases legadas que chegaram sem uma ou mais
        # dessas colunas. Omitir colunas já existentes evita "duplicate column"
        # em reinicializações e garante que o campo reportado no erro em produção
        # (caixa_conferencia.valor_calculado) exista em qualquer base criada antes
        # desta versão.
        def _garantir_colunas_caixa_conferencia(cur):
            cur.execute("PRAGMA table_info(caixa_conferencia)")
            cc_cols = [row[1] for row in cur.fetchall()]
            for col, sql in {
                "valor_calculado": "ALTER TABLE caixa_conferencia ADD COLUMN valor_calculado NUMERIC NOT NULL DEFAULT 0.0",
                "valor_sistema":   "ALTER TABLE caixa_conferencia ADD COLUMN valor_sistema NUMERIC NOT NULL DEFAULT 0.0",
                "valor_informado": "ALTER TABLE caixa_conferencia ADD COLUMN valor_informado NUMERIC NOT NULL DEFAULT 0.0",
                "diferenca":       "ALTER TABLE caixa_conferencia ADD COLUMN diferenca NUMERIC NOT NULL DEFAULT 0.0",
                "data_registro":   "ALTER TABLE caixa_conferencia ADD COLUMN data_registro DATETIME",
            }.items():
                if col not in cc_cols:
                    cur.execute(sql)
        _garantir_colunas_caixa_conferencia(cursor)

        # Lotes de produtos (rastro da NF-e: nLote/qLote/dFab/dVal por item)
        cursor.execute('''
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
        ''')

        # Índices de rastreabilidade/FEFO para lotes (idempotentes)
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lotes_produto_numero_chave ON produto_lotes (produto_id, numero_lote, chave_nfe)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lotes_produto_id ON produto_lotes (produto_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lotes_data_validade ON produto_lotes (data_validade)")

        # Triggers para assegurar que nenhum lote fique com quantidade negativa (T38)
        cursor.execute("CREATE TRIGGER IF NOT EXISTS trg_lotes_nao_negativo_ins BEFORE INSERT ON produto_lotes FOR EACH ROW WHEN NEW.quantidade < 0 BEGIN SELECT RAISE(ABORT, 'Quantidade de lote não pode ser negativa (T38)'); END;")
        cursor.execute("CREATE TRIGGER IF NOT EXISTS trg_lotes_nao_negativo_upd BEFORE UPDATE OF quantidade ON produto_lotes FOR EACH ROW WHEN NEW.quantidade < 0 BEGIN SELECT RAISE(ABORT, 'Quantidade de lote não pode ser negativa (T38)'); END;")

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
        if "valor_icms" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_icms NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_pis" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_pis NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_cofins NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_ibs NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_cbs NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_informado" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN valor_informado REAL NOT NULL DEFAULT 0.0")
        if "diferenca" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN diferenca REAL NOT NULL DEFAULT 0.0")

        # Garantir coluna caixa_operacao_id em vendas_dia
        if "caixa_operacao_id" not in vendas_dia_cols:
            cursor.execute("ALTER TABLE vendas_dia ADD COLUMN caixa_operacao_id INTEGER")


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
        if "valor_icms" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_icms NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_pis" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_pis NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_cofins NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_ibs NUMERIC NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN valor_cbs NUMERIC NOT NULL DEFAULT 0.0")
        if "caixa_operacao_id" not in financeiro_cols:
            cursor.execute("ALTER TABLE financeiro ADD COLUMN caixa_operacao_id INTEGER")

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

        cursor.execute('''
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
                quantidade NUMERIC NOT NULL,
                subtotal NUMERIC NOT NULL,
                regime_tributario TEXT NOT NULL DEFAULT 'ATUAL',
                aliquota_icms REAL NOT NULL DEFAULT 0.0,
                aliquota_pis REAL NOT NULL DEFAULT 0.0,
                aliquota_cofins REAL NOT NULL DEFAULT 0.0,
                aliquota_ibs REAL NOT NULL DEFAULT 0.0,
                aliquota_cbs REAL NOT NULL DEFAULT 0.0,
                valor_icms REAL NOT NULL DEFAULT 0.0,
                valor_pis REAL NOT NULL DEFAULT 0.0,
                valor_cofins REAL NOT NULL DEFAULT 0.0,
                valor_ibs REAL NOT NULL DEFAULT 0.0,
                valor_cbs REAL NOT NULL DEFAULT 0.0,
                lote_id INTEGER,
                quantidade_lote REAL NOT NULL DEFAULT 0.0,
                FOREIGN KEY (venda_id) REFERENCES vendas (id),
                FOREIGN KEY (produto_id) REFERENCES produtos (id)
            )
        ''')
        cursor.execute("PRAGMA table_info(itens_venda)")
        itens_venda_cols = [row[1] for row in cursor.fetchall()]
        if "regime_tributario" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN regime_tributario TEXT NOT NULL DEFAULT 'ATUAL'")
        if "aliquota_icms" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_icms REAL NOT NULL DEFAULT 0.0")
        if "aliquota_pis" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_pis REAL NOT NULL DEFAULT 0.0")
        if "aliquota_cofins" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_cofins REAL NOT NULL DEFAULT 0.0")
        if "aliquota_ibs" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_ibs REAL NOT NULL DEFAULT 0.0")
        if "aliquota_cbs" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN aliquota_cbs REAL NOT NULL DEFAULT 0.0")
        if "valor_icms" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_icms REAL NOT NULL DEFAULT 0.0")
        if "valor_pis" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_pis REAL NOT NULL DEFAULT 0.0")
        if "valor_cofins" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_cofins REAL NOT NULL DEFAULT 0.0")
        if "valor_ibs" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_ibs REAL NOT NULL DEFAULT 0.0")
        if "valor_cbs" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN valor_cbs REAL NOT NULL DEFAULT 0.0")
        if "lote_id" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN lote_id INTEGER")
        if "quantidade_lote" not in itens_venda_cols:
            cursor.execute("ALTER TABLE itens_venda ADD COLUMN quantidade_lote REAL NOT NULL DEFAULT 0.0")

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
        cursor.execute("INSERT OR IGNORE INTO config_taxas (tipo, percentual) VALUES ('VOUCHER', 0.0)")
        cursor.execute("INSERT OR IGNORE INTO config_sistema (chave, valor) VALUES ('limite_caixa', '500.00')")
        cursor.execute("INSERT OR IGNORE INTO config_fiscal (id, api_key, ambiente, webhook_token_hash) VALUES (1, '', 'HOMOLOGACAO', '')")
        cursor.execute(
            "INSERT OR IGNORE INTO config_aliquotas_ncm (ncm_prefixo, aliquota_percentual, descricao, ativo) VALUES ('*', 0.0, 'Aliquota padrao/fallback', 1)"
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO config_aliquotas_fiscais_ncm (
                ncm_prefixo, aliquota_icms, aliquota_pis, aliquota_cofins, aliquota_ibs, aliquota_cbs, descricao, ativo
            )
            VALUES ('*', 0.0, 0.0, 0.0, 0.0, 0.0, 'Aliquotas fiscais padrao/fallback', 1)
            """
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