import csv
import sqlite3
import customtkinter as ctk
from tkinter import StringVar, messagebox, filedialog, ttk
from datetime import datetime, timedelta
from pathlib import Path
from database_manager import get_db_connection, registrar_log
import os
import threading
from database_manager import obter_caminho_dados
from validacao_numerica import aplicar_padrao_entrada_numerica, parse_numero, _normalizar_texto_entrada
from modulo_config import carregar_configuracoes
from modulo_fiscal import normalizar_data_iso


def distribuir_lotes_fefo(cursor, produto_id: int, quantidade_necessaria: int) -> list:
    """Seleciona lotes com saldo do produto em ordem FEFO determinística.

    Critério (fixo, não parametrizável):
      1. lotes com data_validade preenchida primeiro, do mais próximo do vencimento
         (menor data) para o mais distante;
      2. lotes sem data_validade (NULL/vazia) por último;
      3. desempate por id ASC (sempre o mesmo resultado para o mesmo estado do banco).

    Não altera o banco: apenas consulta dentro da transação corrente do chamador.
    Retorna [(lote_id, quantidade_consumida)] respeitando o saldo de cada lote.
    """
    cursor.execute(
        """
        SELECT id, quantidade
        FROM produto_lotes
        WHERE produto_id = ?
          AND quantidade > 0
        ORDER BY
            CASE WHEN data_validade IS NULL OR data_validade = '' THEN 1 ELSE 0 END,
            data_validade ASC,
            id ASC
        """,
        (produto_id,),
    )
    restante = round(float(quantidade_necessaria), 3)
    distribuicao = []
    for lote_id, saldo in cursor.fetchall():
        if restante <= 0.0000005:
            break
        consumir = min(round(float(saldo), 3), restante)
        consumir = round(consumir, 3)
        if consumir > 0:
            distribuicao.append((int(lote_id), consumir))
            restante = round(restante - consumir, 3)
    return distribuicao


def aplicar_baixa_fefo(cursor, produto_id: int, quantidade_total: int) -> list:
    """Baixa de estoque com rastreabilidade FEFO.

    Deve ser chamada DENTRO da transação da venda (mesmo cursor com o qual os
    itens_venda serão gravados), garantindo atomicidade: qualquer falha posterior
    do chamador provoca rollback integral de lotes + agregado + itens.

    Comportamento:
      - produto SEM nenhum lote: não altera nada e retorna [] (o chamador
        mantém o mecanismo legado de baixa agregada);
      - produto COM lotes: consome em FEFO até a quantidade_total, decrementa
        produto_lotes.quantidade por lote, decrementa produtos.quantidade_atual
        exatamente pela quantidade_total e retorna [(lote_id, qtd_consumida)];
      - saldo de lotes insuficiente: levanta ValueError ANTES de qualquer
        UPDATE (nenhuma baixa parcial, nenhum saldo negativo). A existência de
        estoque agregado maior não compensa a diferença — o produto não tem
        lote suficiente para rastrear a saída.

    Retorno: lista de pares (lote_id, quantidade_consumida), na ordem FEFO.
    """
    distribuicao = distribuir_lotes_fefo(cursor, produto_id, quantidade_total)
    if not distribuicao and not produto_tem_lotes_com_saldo(cursor, produto_id):
        # Somente produtos sem histórico de lotes usam a baixa legada.
        return []
    total_disponivel = round(sum(float(qtd) for _, qtd in distribuicao), 3)
    if total_disponivel + 0.0000005 < round(float(quantidade_total), 3):
        raise ValueError(
            f"Estoque insuficiente nos lotes do produto {produto_id}: "
            f"necessario {quantidade_total}, disponivel {total_disponivel}"
        )
    for lote_id, qtd_consumida in distribuicao:
        cursor.execute(
            """
            UPDATE produto_lotes
            SET quantidade = quantidade - ?
            WHERE id = ? AND quantidade >= ?
            """,
            (qtd_consumida, lote_id, qtd_consumida),
        )
    cursor.execute(
        """
        UPDATE produtos
        SET quantidade_atual = CASE
            WHEN quantidade_atual - ? < 0 THEN 0
            ELSE quantidade_atual - ?
        END
        WHERE id = ?
        """,
        (quantidade_total, quantidade_total, produto_id),
    )
    return distribuicao


def reverter_estoque_venda(cursor, venda_id: int) -> dict:
    """Restaura o estoque consumido por uma venda registrada (4B-3).

    Deve ser chamada DENTRO da transação do estorno (mesmo cursor do UPDATE
    de status da venda), garantindo atomicidade: qualquer falha posterior do
    chamador provoca rollback integral de lotes + agregados.

    Regras:
      - item com lote_id registrado (4B-2): devolve quantidade_lote
        EXATAMENTE no lote de origem — nunca desloca mercadoria para outro
        lote e não altera numero_lote/data_validade/demais dados do lote;
      - item sem lote_id (histórico/legado): apenas restaura o agregado do
        produto pela quantidade do item (caminho legado);
      - retorna {produto_id: quantidade_total_restaurada} para conferência.
    """
    restaurado = {}
    linhas = cursor.execute(
        """
        SELECT produto_id, lote_id, COALESCE(quantidade_lote, 0), COALESCE(quantidade, 0)
        FROM itens_venda
        WHERE venda_id = ?
        ORDER BY id ASC
        """,
        (venda_id,),
    ).fetchall()
    for produto_id, lote_id, quantidade_lote, quantidade in linhas:
        qtd_lote = float(quantidade_lote or 0.0)
        qtd_item = float(quantidade or 0.0)
        if lote_id is not None and qtd_lote > 0:
            cursor.execute(
                "UPDATE produto_lotes SET quantidade = quantidade + ? WHERE id = ?",
                (qtd_lote, int(lote_id)),
            )
            devolve = qtd_lote
        else:
            devolve = qtd_item
        if devolve:
            chave = int(produto_id)
            restaurado[chave] = restaurado.get(chave, 0.0) + devolve
    for produto_id, devolve in restaurado.items():
        cursor.execute(
            "UPDATE produtos SET quantidade_atual = quantidade_atual + ? WHERE id = ?",
            (devolve, produto_id),
        )
    return restaurado


def estornar_venda(venda_id: int, motivo: str = "") -> dict:
    """Reverte estoque, venda, financeiro e auditoria em uma transação.

    Exige vínculo textual exato e único com a Entrada original do PDV.
    Não altera vendas_dia, emissão fiscal ou registros financeiros originais.
    """
    from decimal import Decimal, InvalidOperation

    def centavos(valor):
        try:
            numero = Decimal(str(valor))
            if not numero.is_finite() or numero < 0:
                raise ValueError("Valor financeiro inválido no estorno.")
            return numero.quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError("Valor financeiro inválido no estorno.") from exc

    with get_db_connection() as conn:
        # Serializa a verificação e a reversão, inclusive entre dois operadores.
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        try:
            linha = cursor.execute(
                """SELECT status_pedido, valor_total, valor_liquido,
                          valor_impostos_retidos, forma_pagamento, origem
                   FROM vendas WHERE id = ?""", (venda_id,),
            ).fetchone()
            if not linha:
                raise ValueError(f"Venda {venda_id} não encontrada.")
            if str(linha[0] or "").strip().upper() == "ESTORNADO":
                raise ValueError(f"Venda {venda_id} já foi estornada. Dupla reversão bloqueada.")
            descricao = f"Venda PDV #{venda_id} ({linha[4]}) [{linha[5]}]"
            movimentos = cursor.execute(
                """SELECT id, tipo, valor_bruto, valor, valor_impostos_retidos
                   FROM financeiro WHERE descricao = ? COLLATE BINARY""",
                (descricao,),
            ).fetchall()
            if not movimentos:
                raise ValueError(f"Movimento financeiro correspondente à venda {venda_id} não localizado.")
            if len(movimentos) != 1:
                raise ValueError(f"Movimento financeiro ambíguo para a venda {venda_id}. Estorno bloqueado.")
            financeiro_id, tipo, bruto, liquido, impostos = movimentos[0]
            if tipo != "Entrada":
                raise ValueError("Tipo/natureza do movimento financeiro incompatível: esperada Entrada.")
            if tuple(map(centavos, (bruto, liquido, impostos))) != tuple(map(centavos, linha[1:4])):
                raise ValueError("Valor do movimento financeiro incompatível com a venda.")
            descricao_estorno = f"Estorno venda PDV #{venda_id} | financeiro #{financeiro_id}"
            if cursor.execute(
                "SELECT 1 FROM financeiro WHERE descricao = ? COLLATE BINARY LIMIT 1",
                (descricao_estorno,),
            ).fetchone():
                raise ValueError("Movimento financeiro já revertido. Dupla reversão bloqueada.")

            restaurado = reverter_estoque_venda(cursor, venda_id)
            cursor.execute(
                "UPDATE vendas SET status_pedido = 'ESTORNADO' WHERE id = ?", (venda_id,),
            )
            cursor.execute(
                """INSERT INTO financeiro (
                       valor, tipo, valor_bruto, valor_impostos_retidos, taxa_aplicada,
                       descricao, valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs)
                   SELECT valor, 'Saída', valor_bruto, valor_impostos_retidos, taxa_aplicada,
                          ?, valor_icms, valor_pis, valor_cofins, valor_ibs, valor_cbs
                   FROM financeiro WHERE id = ?""",
                (descricao_estorno, financeiro_id),
            )
            reversao_id = cursor.lastrowid
            registrar_log(
                None, "Estorno de venda", "Sucesso",
                f"venda={venda_id} | financeiro={financeiro_id} | reversao={reversao_id} | "
                f"motivo={motivo or '-'} | restaurado={restaurado}", conn=conn,
            )
        finally:
            cursor.close()
    return restaurado


def produto_tem_lotes_com_saldo(cursor, produto_id: int) -> bool:
    """Indica controle por lotes, inclusive quando todos estão esgotados.

    O nome é mantido por compatibilidade com os chamadores existentes.
    Consulta apenas, dentro da transação corrente; somente produtos sem
    qualquer lote podem usar o caminho legado e a edição manual do agregado.
    """
    cursor.execute(
        "SELECT 1 FROM produto_lotes WHERE produto_id = ? LIMIT 1",
        (produto_id,),
    )
    return cursor.fetchone() is not None


def dependencias_exclusao_produto(cursor, produto_id: int) -> list:
    """Lista dependências de rastreabilidade que impedem a exclusão (4B-4).

    Verifica produto_lotes (qualquer registro — saldo ou não, pois FK está
    desligada no banco e a exclusão deixaria órfãos silenciosos), itens_venda
    (histórico de vendas), entradas (histórico de entradas), orcamento_itens
    e fornecedor_produtos.
    Consulta apenas, dentro da transação corrente do chamador.
    """
    dependencias = []
    for tabela in ("produto_lotes", "itens_venda", "entradas", "orcamento_itens", "fornecedor_produtos"):
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (tabela,)
        )
        if cursor.fetchone():
            cursor.execute(
                f"SELECT 1 FROM {tabela} WHERE produto_id = ? LIMIT 1", (produto_id,)
            )
            if cursor.fetchone():
                dependencias.append(tabela)
    return dependencias


def exportar_produtos_para_xls_arquivo(caminho_arquivo: str):
    """Exporta os produtos para CSV (ou XLSX se openpyxl estiver disponível)."""

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                codigo_barras,
                nome,
                variacao,
                ncm,
                aliquota_icms,
                aliquota_pis,
                aliquota_cofins,
                aliquota_ibs,
                aliquota_cbs,
                preco_custo,
                margem_lucro,
                preco_venda,
                quantidade_atual,
                quantidade_minima,
                validade,
                categoria,
                preco_base,
                inicio_promocao,
                fim_promocao,
                imagem_path
            FROM produtos
            ORDER BY nome COLLATE NOCASE ASC
            """
        ).fetchall()

    headers = [
        "ID",
        "Código de Barras",
        "Nome",
        "Variação",
        "NCM",
        "Aliq. ICMS",
        "Aliq. PIS",
        "Aliq. COFINS",
        "Aliq. IBS",
        "Aliq. CBS",
        "Preço de Custo",
        "Margem %",
        "Preço de Venda",
        "Quantidade Atual",
        "Quantidade Mínima",
        "Validade",
        "Categoria",
        "Preço Base",
        "Início Promoção",
        "Fim Promoção",
        "Caminho Imagem",
    ]

    dados = []
    for row_index, row in enumerate(rows, start=1):
        valores = [
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
            row[12],
            row[13],
            row[14],
            row[15],
            row[16],
            row[17],
            row[18],
            row[19],
            row[20],
        ]
        dados.append(["" if v is None else v for v in valores])

    pasta = os.path.dirname(caminho_arquivo)
    if pasta and not os.path.exists(pasta):
        os.makedirs(pasta, exist_ok=True)

    ext = Path(caminho_arquivo).suffix.lower()
    if ext == ".csv":
        with open(caminho_arquivo, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(headers)
            writer.writerows(dados)
        return caminho_arquivo

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError(
            "O exportador Excel não está disponível nesta instalação. "
            "Use CSV ou atualize o aplicativo para a versão mais recente."
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    ws.append(headers)
    for linha in dados:
        ws.append(linha)
    wb.save(caminho_arquivo)
    return caminho_arquivo


def calcular_preco_venda(preco_custo, margem_lucro):
    """Calcula preço de venda com base no custo e margem percentual."""
    custo = float(preco_custo)
    margem = float(margem_lucro)
    return round(custo * (1 + (margem / 100.0)), 2)


def formatar_percentual_inteiro(valor):
    """Formata percentuais para exibição com menor carga cognitiva (ex.: 100%)."""
    try:
        numero = float(valor)
    except Exception:
        numero = 0.0
    return f"{int(round(numero))}%"


def produto_e_vendido_por_kg(nome, variacao="", categoria=""):
    """Detecta produto vendido por KG (mesma convenção usada no PDV:
    variação 'kg', categoria 'kg' ou nome terminando em 'kg')."""
    nome = (nome or "").strip().lower()
    variacao = (variacao or "").strip().lower()
    categoria = (categoria or "").strip().lower()
    return variacao == "kg" or categoria == "kg" or nome.endswith("kg")


# Códigos de unidade que NÃO são uma variação real (usado só na EXIBIÇÃO
# da grade "PRODUTOS CADASTRADOS"; banco e consulta permanecem intactos).
_UNIDADES_COMO_VARIACAO = ("UN", "UND", "UNID", "KG", "CX", "PC")


def variacao_exibicao_grade(variacao):
    """Texto da coluna VARIAÇÃO na grade "PRODUTOS CADASTRADOS".

    Somente transformação de EXIBIÇÃO na montagem da linha da Treeview:
    - se o campo `variacao` contém apenas um código de unidade
      (UN, UND, UNID, KG, CX, PC), comparado normalizado (sem distinção
      de maiúsculas/minúsculas e ignorando espaços nas extremidades),
      a célula fica vazia;
    - qualquer outro texto real de variação é exibido normalmente.

    Não altera banco de dados, colunas `variacao`/`unidade`, SQL,
    estoque, PDV, pagamentos, gaveta, fechamento, cupom ou licenciamento.
    """
    texto = str(variacao or "")
    if texto.strip().upper() in _UNIDADES_COMO_VARIACAO:
        return ""
    # Valor realmente vazio mantém o "-" legado da grade.
    return texto if texto.strip() else "-"


class ModuloEstoque(ctk.CTkToplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Gestão de Estoque - PDV Mercado")
        self.geometry("1180x640")
        self.grab_set()

        if master is not None and not getattr(master, "usuario_atual", None):
            messagebox.showerror("Acesso Negado", "Sessão inválida. Faça login para acessar o estoque.")
            self.destroy()
            return

        self.current_editing_id = None
        self.produto_selecionado = None
        self.row_selecionada = None
        self.temp_image_path = ""
        self.page_size = 50
        self.current_offset = 0
        self.total_produtos = 0
        self._estoque_carregado = False
        self._janela_produtos = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        
        # Pasta de imagens graváveis por usuário (evita Program Files).
        self.pasta_imagens = obter_caminho_dados("assets", "produtos")
        os.makedirs(self.pasta_imagens, exist_ok=True)

        # --- Cabeçalho e Busca ---
        self.frame_top = ctk.CTkFrame(self)
        self.frame_top.grid(row=0, column=0, padx=20, pady=10, sticky="ew")

        ctk.CTkLabel(self.frame_top, text="Código de Barras (Entrada Rápida):", font=("Arial", 12, "bold")).pack(side="left", padx=10)
        self.entry_barcode = ctk.CTkEntry(self.frame_top, width=250, placeholder_text="Escaneie o produto...")
        self.entry_barcode.pack(side="left", padx=10, pady=10)
        self.entry_barcode.bind("<Return>", self.buscar_por_barcode)

        self.btn_importar_nfe = ctk.CTkButton(
            self.frame_top,
            text="IMPORTAR NF-e",
            width=150,
            height=34,
            fg_color="#14532d",
            hover_color="#166534",
            command=self.alternar_campo_importar_nfe,
        )
        self.btn_importar_nfe.pack(side="left", padx=6)

        self.btn_anexar_nfe_direto = ctk.CTkButton(
            self.frame_top,
            text="ANEXAR XML",
            width=130,
            height=34,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            command=self.anexar_nfe_xml,
        )
        self.btn_anexar_nfe_direto.pack(side="left", padx=6)

        self.frame_importar_nfe = ctk.CTkFrame(self.frame_top, fg_color="transparent")
        ctk.CTkLabel(
            self.frame_importar_nfe,
            text="Chave NF-e:",
            font=("Arial", 11, "bold"),
        ).pack(side="left", padx=(8, 4))
        self.entry_chave_nfe = ctk.CTkEntry(
            self.frame_importar_nfe,
            width=320,
            placeholder_text="Cole a chave de 44 dígitos",
        )
        self.entry_chave_nfe.pack(side="left", padx=4, pady=10)
        self.entry_chave_nfe.bind("<Return>", self.importar_nfe_por_chave)
        self.btn_buscar_nfe = ctk.CTkButton(
            self.frame_importar_nfe,
            text="Buscar",
            width=90,
            command=self.importar_nfe_por_chave,
        )
        self.btn_buscar_nfe.pack(side="left", padx=(4, 0))
        
        self.btn_refresh = ctk.CTkButton(self.frame_top, text="Atualizar Lista", command=self.recarregar_primeira_pagina)
        self.btn_refresh.pack(side="right", padx=10)

        # --- Painel de Cadastro Profissional ---
        self.frame_cadastro = ctk.CTkFrame(self)
        self.frame_cadastro.grid(row=1, column=0, padx=20, pady=5, sticky="ew")

        # Campos de Entrada
        campos_frame = ctk.CTkFrame(self.frame_cadastro, fg_color="transparent")
        campos_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        self.var_preco_custo = StringVar()
        self.var_margem_lucro = StringVar()
        self.var_preco_venda = StringVar()
        self._atualizando_precificacao = False
        self._margem_ajustada_manual = False
        self._cor_margem_padrao = ["#F9F9FA", "#343638"]
        self._cor_borda_margem_padrao = ["#979DA2", "#565B5E"]
        self._cor_texto_margem_padrao = ["gray10", "#DCE4EE"]

        self.ent_nome = ctk.CTkEntry(campos_frame, placeholder_text="Nome do Produto", width=300)
        self.ent_nome.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        self.ent_variacao = ctk.CTkEntry(campos_frame, placeholder_text="Variação (cor, tamanho, etc.)", width=300)
        self.ent_variacao.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="w")
        self.ent_variacao.bind("<KeyRelease>", lambda _e: self._atualizar_label_preco_venda())
        self.ent_variacao.bind("<FocusOut>", lambda _e: self._atualizar_label_preco_venda())
        self.ent_nome.bind("<KeyRelease>", lambda _e: self._atualizar_label_preco_venda())
        # Unidade de venda (FASE 1 UN/KG): controle explícito, sem usar variação.
        self.var_unidade = StringVar(value="UN")
        self.opt_unidade = ctk.CTkOptionMenu(campos_frame, variable=self.var_unidade, values=["UN", "KG"], width=145)
        self.opt_unidade.grid(row=1, column=2, padx=5, pady=5, sticky="w")
        try:
            self.var_unidade.trace_add("write", lambda *_a: self._ao_mudar_unidade())
        except Exception:
            pass

        ctk.CTkLabel(campos_frame, text="Código NCM", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=2, column=0, padx=5, pady=(0, 2), sticky="w")
        self.ent_ncm = ctk.CTkEntry(campos_frame, placeholder_text="NCM (somente números)", width=145)
        self.ent_ncm.grid(row=3, column=0, padx=5, pady=5, sticky="w")
        aplicar_padrao_entrada_numerica(self.ent_ncm, inteiro=True)

        # Labels explícitos para manter legibilidade independentemente do placeholder.
        ctk.CTkLabel(campos_frame, text="Custo", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=2, column=1, padx=5, pady=(0, 2), sticky="w")
        ctk.CTkLabel(campos_frame, text="Margem", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=4, column=0, padx=5, pady=(0, 2), sticky="w")

        self.ent_preco_custo = ctk.CTkEntry(campos_frame, placeholder_text="Preço Custo R$", width=145, textvariable=self.var_preco_custo)
        self.ent_preco_custo.grid(row=3, column=1, padx=5, pady=5, sticky="w")

        self.ent_margem_lucro = ctk.CTkEntry(campos_frame, placeholder_text="Margem Lucro %", width=145, textvariable=self.var_margem_lucro)
        self.ent_margem_lucro.grid(row=5, column=0, padx=5, pady=5, sticky="w")

        self.lbl_preco_venda = ctk.CTkLabel(campos_frame, text="Preço Venda", font=("Arial", 11, "bold"), text_color="#DCE4EE")
        self.lbl_preco_venda.grid(row=4, column=1, padx=5, pady=(0, 2), sticky="w")
        ctk.CTkLabel(campos_frame, text="QTD", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=6, column=0, padx=5, pady=(0, 2), sticky="w")

        self.ent_preco_venda = ctk.CTkEntry(campos_frame, placeholder_text="Preço Venda R$", width=145, textvariable=self.var_preco_venda)
        self.ent_preco_venda.grid(row=5, column=1, padx=5, pady=5, sticky="w")

        self.ent_qtd = ctk.CTkEntry(campos_frame, placeholder_text="Qtd Atual", width=145)
        self.ent_qtd.grid(row=7, column=0, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(campos_frame, text="Validade", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=6, column=1, padx=5, pady=(0, 2), sticky="w")
        ctk.CTkLabel(campos_frame, text="QTD Mínima", font=("Arial", 11, "bold"), text_color="#DCE4EE").grid(row=8, column=0, padx=5, pady=(0, 2), sticky="w")

        self.ent_val = ctk.CTkEntry(campos_frame, placeholder_text="Validade (AAAA-MM-DD)", width=145)
        self.ent_val.grid(row=7, column=1, padx=5, pady=5, sticky="w")

        self.ent_qtd_min = ctk.CTkEntry(campos_frame, placeholder_text="Qtd Mínima", width=145)
        self.ent_qtd_min.grid(row=9, column=0, padx=5, pady=5, sticky="w")

        aplicar_padrao_entrada_numerica(self.ent_preco_custo, inteiro=False, casas_decimais=2)
        aplicar_padrao_entrada_numerica(self.ent_margem_lucro, inteiro=False, casas_decimais=2)
        aplicar_padrao_entrada_numerica(self.ent_preco_venda, inteiro=False, casas_decimais=2)
        # FASE 1 UN/KG: máscara decimal (até 3 casas) permite digitar peso KG
        # (ex.: 1,250). A obrigatoriedade de inteiro em produtos UN continua
        # garantida na validação de salvar_produto (parse_numero inteiro=True).
        aplicar_padrao_entrada_numerica(self.ent_qtd, inteiro=False, casas_decimais=3)
        aplicar_padrao_entrada_numerica(self.ent_qtd_min, inteiro=False, casas_decimais=3)

        self.var_preco_custo.trace_add("write", self._atualizar_preco_venda_automatico)
        self.var_margem_lucro.trace_add("write", self._atualizar_preco_venda_automatico)
        self.var_preco_venda.trace_add("write", self._atualizar_margem_por_preco_manual)

        # Gerenciamento de Imagem
        self.img_frame = ctk.CTkFrame(self.frame_cadastro, width=120, height=120)
        self.img_frame.pack(side="left", padx=10)
        self.img_frame.pack_propagate(False)
        
        self.lbl_preview_img = ctk.CTkLabel(self.img_frame, text="Sem Imagem", font=("Arial", 10))
        self.lbl_preview_img.pack(expand=True)

        self.btn_upload = ctk.CTkButton(self.frame_cadastro, text="Carregar Foto", width=100, command=self.selecionar_imagem_manual)
        self.btn_upload.pack(side="left", padx=5)

        # Barra de Ações do Cadastro
        self.actions_frame = ctk.CTkFrame(self.frame_cadastro, width=170, fg_color="#20252b", corner_radius=8)
        self.actions_frame.pack(side="right", padx=10, pady=10, fill="y")
        self.actions_frame.pack_propagate(False)
        ctk.CTkLabel(
            self.actions_frame,
            text="AÇÕES DO CADASTRO",
            font=("Arial", 11, "bold"),
            text_color="#DCE4EE",
        ).pack(pady=(10, 6))

        self.btn_save = ctk.CTkButton(
            self.actions_frame,
            text="SALVAR CADASTRO",
            height=42,
            fg_color="#27ae60",
            hover_color="#2ecc71",
            command=self.salvar_produto,
        )
        self.btn_save.pack(padx=8, pady=5, fill="x")

        self.btn_edit_sel = ctk.CTkButton(
            self.actions_frame,
            text="EDITAR PRODUTO",
            height=38,
            fg_color="#2980b9",
            hover_color="#2b8fd8",
            command=self.editar_produto_selecionado,
            state="disabled",
        )
        self.btn_edit_sel.pack(padx=8, pady=5, fill="x")

        self.lbl_badge_margem_manual = ctk.CTkLabel(
            self.actions_frame,
            text="Margem Ajustada Manualmente",
            fg_color="#c27c0e",
            text_color="white",
            corner_radius=10,
            font=("Arial", 10, "bold"),
        )

        self.btn_limpar = ctk.CTkButton(self.actions_frame, text="LIMPAR CAMPOS", height=34, fg_color="gray40", command=self.limpar_campos)
        self.btn_limpar.pack(padx=8, pady=5, fill="x")

        self.btn_excluir = ctk.CTkButton(
            self.actions_frame,
            text="EXCLUIR PRODUTO",
            height=38,
            fg_color="#c0392b",
            hover_color="#e74c3c",
            command=self.excluir_produto_selecionado,
            state="disabled",
        )
        self.btn_excluir.pack(padx=8, pady=(5, 10), fill="x")

        # --- Organização da Interface (1.0.16 — homologação) ---
        # A listagem de produtos NÃO fica mais na tela de cadastro: abre em
        # JANELA INDEPENDENTE (grade/tabela) pelo botão "PRODUTOS CADASTRADOS".
        # Os widgets antigos de listagem embutida (header, scroll, paginação)
        # foram removidos desta tela; a consulta reutiliza os mesmos métodos.
        self.frame_info_topo = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_info_topo.grid(row=2, column=0, sticky="ew", padx=30)

        self.lbl_alerta = ctk.CTkLabel(self.frame_info_topo, text="", text_color="orange", font=("Arial", 11, "italic"))
        self.lbl_alerta.pack(side="left", pady=(0, 2))

        self.lbl_legenda_codigo = ctk.CTkLabel(
            self.frame_info_topo,
            text="Azul = Interno, Verde = Real",
            text_color="#bfc7d5",
            font=("Arial", 10, "bold"),
        )
        self.lbl_legenda_codigo.pack(side="right", pady=(0, 2))

        # Botão que abre a janela de listagem/seleção (grade/planilha).
        self.btn_produtos_cadastrados = ctk.CTkButton(
            self.frame_info_topo,
            text="PRODUTOS CADASTRADOS",
            width=230,
            height=30,
            fg_color="#5d4037",
            hover_color="#6d5047",
            font=("Arial", 12, "bold"),
            command=self.abrir_janela_produtos_cadastrados,
        )
        self.btn_produtos_cadastrados.pack(side="right", pady=(0, 2), padx=(10, 0))

        self._configurar_navegacao_tab()
        self._atualizar_disponibilidade_importacao_nfe()
        self._safe_focus(self.entry_barcode)

    def _atualizar_disponibilidade_importacao_nfe(self):
        cfg = carregar_configuracoes() or {}
        fiscal_ativo = bool(cfg.get("fiscal_ativo", False))
        self.btn_importar_nfe.configure(state="normal", fg_color="#14532d", hover_color="#166534")
        if not fiscal_ativo:
            self.lbl_alerta.configure(
                text="Importação local de XML disponível. ACBr é necessário somente para emissão fiscal.",
                text_color="#f1c40f",
            )

    def _obter_unidade_tela(self):
        """Unidade selecionada na tela (UN/KG), com fallback legado."""
        try:
            uni = str(self.var_unidade.get() or "").strip().upper()
        except Exception:
            uni = ""
        if uni in ("UN", "KG"):
            return uni
        try:
            nome = self.ent_nome.get()
            variacao = self.ent_variacao.get()
        except Exception:
            return "UN"
        return "KG" if produto_e_vendido_por_kg(nome, variacao) else "UN"

    def _ao_mudar_unidade(self):
        """UN/KG alterado na tela: atualiza o label de preço e normaliza a QTD.

        UN mantém quantidade inteira (a parte decimal de um peso digitado por
        engano é descartada sem virar milhar); KG preserva até 3 casas.
        """
        try:
            self._atualizar_label_preco_venda()
        except Exception:
            pass

        unidade = self._obter_unidade_tela()
        for entry in (getattr(self, "ent_qtd", None), getattr(self, "ent_qtd_min", None)):
            if entry is None:
                continue
            try:
                atual = entry.get().strip()
            except Exception:
                continue
            if not atual:
                continue
            if unidade == "KG":
                normalizado = _normalizar_texto_entrada(atual, inteiro=False, casas_decimais=3)
            else:
                normalizado = _normalizar_texto_entrada(atual.split(",")[0], inteiro=True)
            if atual != normalizado:
                entry.delete(0, "end")
                entry.insert(0, normalizado)

    def _formatar_qtd_tela(self, valor, unidade=None):
        """Formata a quantidade do cadastro conforme a unidade (UN / KG 3 casas)."""
        unidade = (unidade or self._obter_unidade_tela() or "UN").upper()
        try:
            numero = float(valor)
        except (TypeError, ValueError):
            return ""
        if unidade == "KG":
            return f"{numero:.3f}".replace(".", ",")
        return str(int(round(numero)))

    def _reconfigurar_mascara_qtd(self):
        """Compatibilidade: delegar para o único ponto que aplica a máscara.

        Mantido como alias para não duplicar a lógica de rebind da QTD
        (FASE 1 UN/KG). Use _aplicar_mascara_qtd() como fonte única.
        """
        try:
            self._aplicar_mascara_qtd(self._obter_unidade_tela())
        except Exception:
            pass

    def _atualizar_label_preco_venda(self):
        """Deixa inequívoco que o preço informado é POR KG em produtos pesáveis."""
        if not hasattr(self, "lbl_preco_venda"):
            return
        try:
            unidade = self._obter_unidade_tela()
        except Exception:
            return
        if unidade == "KG":
            self.lbl_preco_venda.configure(text="Preço Venda por KG (/KG)", text_color="#f39c12")
        else:
            self.lbl_preco_venda.configure(text="Preço Venda", text_color="#DCE4EE")
        # Unidade KG também libera QTD decimal (até 3 casas) no cadastro.
        self._aplicar_mascara_qtd(unidade)

    def _aplicar_mascara_qtd(self, unidade):
        """Máscara de quantidade coerente com a unidade (FASE 1 UN/KG).

        UN: inteiro. KG: decimal até 3 casas (1,250).
        A máscara é reaplicada apenas quando a unidade MUDA: evita acumular
        bindings de <KeyRelease>/<FocusOut> a cada tecla digitada no nome/variação.
        """
        if not hasattr(self, "ent_qtd"):
            return
        desejado = "KG" if str(unidade).strip().upper() == "KG" else "UN"
        if getattr(self, "_mascara_qtd_atual", None) == desejado:
            return
        self._mascara_qtd_atual = desejado
        try:
            for entry in (self.ent_qtd, self.ent_qtd_min):
                try:
                    entry.unbind("<KeyRelease>")
                    entry.unbind("<FocusOut>")
                except Exception:
                    pass
            if desejado == "KG":
                aplicar_padrao_entrada_numerica(self.ent_qtd, inteiro=False, casas_decimais=3)
                aplicar_padrao_entrada_numerica(self.ent_qtd_min, inteiro=False, casas_decimais=3)
            else:
                aplicar_padrao_entrada_numerica(self.ent_qtd, inteiro=True)
                aplicar_padrao_entrada_numerica(self.ent_qtd_min, inteiro=True)
        except Exception:
            pass

    def exportar_produtos_xls(self):
        """Exporta produtos; também mantém o rótulo de preço coerente."""
        """Abre o diálogo de salvamento e exporta os produtos para XLS."""
        pasta_padrao = obter_caminho_dados("exportacao_fiscal")
        os.makedirs(pasta_padrao, exist_ok=True)
        nome_padrao = f"produtos_{datetime.now().strftime('%d%m%Y_%H%M%S')}.xls"

        destino = filedialog.asksaveasfilename(
            initialdir=pasta_padrao,
            initialfile=nome_padrao,
            defaultextension=".xls",
            filetypes=[("Arquivo Excel", "*.xls")],
            title="Salvar exportação de produtos",
        )
        if not destino:
            return

        try:
            exportar_produtos_para_xls_arquivo(destino)
            registrar_log(None, "Exportação de Produtos", "Sucesso", f"Arquivo exportado: {destino}")
            messagebox.showinfo("Exportação concluída", f"Produtos exportados com sucesso em:\n{destino}")
        except Exception as exc:
            registrar_log(None, "Exportação de Produtos", "Falha", f"Erro: {exc}")
            messagebox.showerror("Erro na exportação", f"Não foi possível exportar os produtos:\n{exc}")

    def _normalizar_chave_nfe(self, chave: str) -> str:
        return "".join(ch for ch in str(chave or "") if ch.isdigit())

    def alternar_campo_importar_nfe(self):
        cfg = carregar_configuracoes() or {}
        if self.frame_importar_nfe.winfo_ismapped():
            self.frame_importar_nfe.pack_forget()
            self._safe_focus(self.entry_barcode)
            return
        self.frame_importar_nfe.pack(side="left", padx=6)
        if not bool(cfg.get("fiscal_ativo", False)):
            self.lbl_alerta.configure(
                text="Importação local de XML disponível. ACBr é necessário somente para emissão fiscal.",
                text_color="#f1c40f",
            )
        self._safe_focus(self.entry_chave_nfe)

    def _listar_xml_nfe_candidatos(self):
        candidatos_dirs = [
            Path(obter_caminho_dados("fiscal_in")),
            Path(obter_caminho_dados("exportacao_fiscal")),
            Path(__file__).resolve().parent / "fiscal_in",
            Path(__file__).resolve().parent / "exportacao_fiscal",
        ]

        try:
            from modulo_config import carregar_configuracoes

            cfg = carregar_configuracoes() or {}
            pasta_in = str(cfg.get("pasta_entrada_fiscal") or "").strip()
            pasta_exp = str(cfg.get("pasta_exportacao_fiscal") or "").strip()
            if pasta_in:
                candidatos_dirs.append(Path(pasta_in))
            if pasta_exp:
                candidatos_dirs.append(Path(pasta_exp))
        except Exception:
            pass

        vistos = set()
        arquivos = []
        for pasta in candidatos_dirs:
            try:
                pasta_norm = str(pasta.resolve())
            except Exception:
                pasta_norm = str(pasta)
            if pasta_norm in vistos or not pasta.exists() or not pasta.is_dir():
                continue
            vistos.add(pasta_norm)
            for arq in pasta.glob("*.xml"):
                if arq.is_file():
                    arquivos.append(arq)

        arquivos.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return arquivos

    def _buscar_nfe_por_chave(self, chave_nfe: str):
        from modulo_fiscal import FiscalManager

        fiscal = FiscalManager()
        for caminho_xml in self._listar_xml_nfe_candidatos():
            try:
                dados = fiscal.processar_xml_entrada(caminho_xml)
            except Exception:
                continue

            chave_xml = self._normalizar_chave_nfe(dados.get("chave_nfe", ""))
            if chave_xml == chave_nfe:
                return dados

        return None

    def importar_nfe_por_chave(self, event=None):
        chave = self._normalizar_chave_nfe(self.entry_chave_nfe.get())
        if len(chave) != 44:
            messagebox.showwarning("Chave NF-e inválida", "Informe uma chave NF-e com 44 dígitos.")
            self._safe_focus(self.entry_chave_nfe)
            return "break"

        self.lbl_alerta.configure(text="🔎 Buscando NF-e pelos XMLs locais...", text_color="cyan")

        def _worker():
            dados = self._buscar_nfe_por_chave(chave)
            if not self.winfo_exists():
                return
            self.after(0, lambda: self._aplicar_importacao_nfe(chave, dados))

        threading.Thread(target=_worker, daemon=True).start()
        return "break"

    def anexar_nfe_xml(self):
        caminho = filedialog.askopenfilename(
            title="Selecionar XML da NF-e",
            filetypes=[("Nota fiscal XML", "*.xml"), ("Todos os arquivos", "*.*")],
        )
        if not caminho:
            return

        self.lbl_alerta.configure(text="🔎 Lendo XML da NF-e selecionado...", text_color="cyan")

        def _worker():
            try:
                from modulo_fiscal import FiscalManager

                dados = FiscalManager().processar_xml_entrada(caminho)
                chave = self._normalizar_chave_nfe(dados.get("chave_nfe", ""))
                if not chave:
                    chave = "XML_ANEXADO"
            except Exception as exc:
                dados = None
                chave = "XML_ANEXADO"
                erro = str(exc)
            else:
                erro = None

            if self.winfo_exists():
                self.after(0, lambda: self._aplicar_importacao_nfe(chave, dados, erro))

        threading.Thread(target=_worker, daemon=True).start()
        return "break"

    def _atualizar_validade_agregada(self, cursor, produto_id: int):
        """
        Define produtos.validade como a MENOR data de validade entre os lotes
        validos do produto (mais proxima do vencimento). A validade por lote
        permanece o dado principal; este campo e apenas o agregado usado pelos
        alertas de validade (modulo_estoque, modulo_main, ia_gestao, exportacao).
        Produtos sem lote com validade mantem o valor atual (edicao manual preservada).
        """
        cursor.execute(
            """
            SELECT MIN(data_validade) FROM produto_lotes
            WHERE produto_id = ? AND COALESCE(TRIM(data_validade), '') <> ''
            """,
            (produto_id,),
        )
        menor_validade = cursor.fetchone()[0]
        if menor_validade:
            cursor.execute(
                "UPDATE produtos SET validade = ? WHERE id = ?",
                (menor_validade, produto_id),
            )

    def _aplicar_importacao_nfe(self, chave_nfe: str, dados_nfe: dict | None, erro: str | None = None):
        if not dados_nfe or not dados_nfe.get("itens"):
            mensagem = f"Não foi possível ler o XML selecionado: {erro}" if erro else "NF-e sem itens válidos."
            self.lbl_alerta.configure(text=mensagem, text_color="orange")
            return

        itens = list(dados_nfe.get("itens") or [])
        cadastrados = 0
        reimportadas = 0

        with get_db_connection() as conn:
            cursor = conn.cursor()
            for item in itens:
                ean = str(item.get("ean") or "").strip()
                descricao = str(item.get("descricao") or "").strip() or "Produto sem descrição"
                ncm = str(item.get("ncm") or "").strip()
                preco = float(item.get("preco") or 0.0)
                quantidade_nfe = float(item.get("quantidade") or 0.0)
                # FASE 1 UN/KG: unidade vinda do XML (uCom/uTrib). Só é usada em
                # cadastro NOVO; produtos existentes preservam a unidade atual
                # (COALESCE/NULLIF abaixo).
                unidade_bruta = str(item.get("unidade") or "").strip().upper()
                unidade_nfe = "KG" if ("KG" in unidade_bruta or "QUILO" in unidade_bruta) else "UN"
                validade_nfe = normalizar_data_iso(item.get("validade"))
                lotes_nfe = list(item.get("lotes") or [])
                codigo = ean if ean else self._gerar_codigo_interno_sequencial()

                # --- Normalizacao dos lotes da NF-e (nLote/dFab/dVal/qLote) --------
                # qLote ausente/vazio/invalido cai para a quantidade do item;
                # nunca cria lote com quantidade 0.
                lotes_para_gravar = []
                for lote in lotes_nfe:
                    try:
                        q_lote = float(lote.get("quantidade") or 0.0)
                    except Exception:
                        q_lote = 0.0
                    if q_lote <= 0:
                        q_lote = quantidade_nfe
                    if q_lote <= 0:
                        continue
                    lotes_para_gravar.append(
                        {
                            "numero_lote": str(lote.get("numero_lote") or "").strip(),
                            "quantidade": q_lote,
                            "data_fabricacao": normalizar_data_iso(lote.get("data_fabricacao")),
                            "data_validade": normalizar_data_iso(lote.get("data_validade")) or validade_nfe,
                        }
                    )

                # NF-e sem <rastro>: lote sintetico com identificador claro "S/LOTE",
                # quantidade real do item e validade do item quando disponivel.
                if not lotes_para_gravar and quantidade_nfe > 0:
                    lotes_para_gravar.append(
                        {
                            "numero_lote": "S/LOTE",
                            "quantidade": quantidade_nfe,
                            "data_fabricacao": "",
                            "data_validade": validade_nfe,
                        }
                    )

                # Agrupa lotes com o mesmo numero_lote (evita colisao no UNIQUE
                # (produto_id, numero_lote, chave_nfe) quando a NF-e repete nLote).
                lotes_agrupados: dict = {}
                for lote in lotes_para_gravar:
                    chave_lote = lote["numero_lote"]
                    atual = lotes_agrupados.get(chave_lote)
                    if atual is None:
                        lotes_agrupados[chave_lote] = dict(lote)
                    else:
                        atual["quantidade"] += lote["quantidade"]
                        if lote["data_validade"] and (not atual["data_validade"] or lote["data_validade"] < atual["data_validade"]):
                            atual["data_validade"] = lote["data_validade"]
                        if lote["data_fabricacao"] and not atual["data_fabricacao"]:
                            atual["data_fabricacao"] = lote["data_fabricacao"]
                lotes_para_gravar = list(lotes_agrupados.values())

                cursor.execute("SELECT id FROM produtos WHERE codigo_barras = ? LIMIT 1", (codigo,))
                existente = cursor.fetchone()

                # --- Deduplicacao de NF-e ------------------------------------------
                # Mecanismo existente: produto_lotes.chave_nfe registra a chave da
                # NF-e processada. A checagem e POR PRODUTO (chave + produto_id):
                # se este produto ja recebeu lote dessa chave, a NF-e ja foi
                # importada para ele: atualiza apenas cadastro, sem somar estoque,
                # lotes ou entradas. Com a chave fallback "XML_ANEXADO" a
                # deduplicacao ocorre no nivel do lote, na gravacao idempotente.
                reimportada = False
                if existente and chave_nfe and chave_nfe != "XML_ANEXADO":
                    cursor.execute(
                        "SELECT 1 FROM produto_lotes WHERE chave_nfe = ? AND produto_id = ? LIMIT 1",
                        (chave_nfe, existente[0]),
                    )
                    reimportada = cursor.fetchone() is not None

                if existente:
                    produto_id = existente[0]
                    if reimportada:
                        reimportadas += 1
                        cursor.execute(
                            """
                            UPDATE produtos
                            SET nome = ?,
                                variacao = COALESCE(NULLIF(variacao, ''), 'NF-e'),
                                ncm = ?,
                                preco_custo = ?,
                                preco_venda = ?,
                                margem_lucro = COALESCE(margem_lucro, 0.0),
                                unidade = COALESCE(NULLIF(unidade, ''), ?)
                            WHERE id = ?
                            """,
                            (descricao, ncm, preco, preco, unidade_nfe, produto_id),
                        )
                        continue

                    # Produto ja cadastrado recebendo mercadoria: a validade NAO e
                    # sobrescrita aqui; passa a ser agregada dos lotes ao final.
                    # O estoque NAO e somado aqui: o delta e calculado pela
                    # reconciliacao dos lotes (gravacao idempotente abaixo), o que
                    # torna a reimportacao sem chave real estavel.
                    cursor.execute(
                        """
                        UPDATE produtos
                        SET nome = ?,
                            variacao = COALESCE(NULLIF(variacao, ''), 'NF-e'),
                            ncm = ?,
                            preco_custo = ?,
                            preco_venda = ?,
                            margem_lucro = COALESCE(margem_lucro, 0.0),
                            unidade = COALESCE(NULLIF(unidade, ''), ?)
                        WHERE id = ?
                        """,
                        (descricao, ncm, preco, preco, unidade_nfe, produto_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO produtos (
                            codigo_barras, nome, variacao, unidade, ncm, preco_custo, margem_lucro, preco_venda,
                            quantidade_atual, quantidade_minima, validade, imagem_path
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                        """,
                        (codigo, descricao, "NF-e", unidade_nfe, ncm, preco, 0.0, preco, validade_nfe, ""),
                    )
                    produto_id = cursor.lastrowid
                    cadastrados += 1

                # --- Gravacao idempotente dos lotes (respeita o UNIQUE) -------------
                # Um lote por (produto_id, numero_lote, chave_nfe). Se ja existe
                # (reimportacao sem chave real, ex. "XML_ANEXADO"), ATUALIZA a
                # quantidade para o valor da NF-e. O estoque agregado do produto e
                # ajustado pelo DELTA TOTAL dos lotes (nunca soma cega), tornando
                # a reimportacao estavel. Nada de INSERT cego.
                if not lotes_para_gravar:
                    continue

                delta_total = 0.0
                for lote in lotes_para_gravar:
                    cursor.execute(
                        """
                        SELECT id, quantidade FROM produto_lotes
                        WHERE produto_id = ? AND numero_lote = ? AND chave_nfe = ?
                        """,
                        (produto_id, lote["numero_lote"], chave_nfe),
                    )
                    lote_existente = cursor.fetchone()

                    if lote_existente:
                        lote_id, qtd_antiga = lote_existente[0], float(lote_existente[1] or 0.0)
                        cursor.execute(
                            """
                            UPDATE produto_lotes
                            SET quantidade = ?,
                                data_validade = COALESCE(NULLIF(?, ''), data_validade),
                                data_fabricacao = COALESCE(NULLIF(?, ''), data_fabricacao)
                            WHERE id = ?
                            """,
                            (lote["quantidade"], lote["data_validade"], lote["data_fabricacao"], lote_id),
                        )
                        delta_total += round(float(lote["quantidade"]) - float(qtd_antiga), 3)
                        # Entrada original ja existe da primeira recepcao:
                        # sem novo lancamento (sem historico falso).
                    else:
                        cursor.execute(
                            """
                            INSERT INTO produto_lotes
                                (produto_id, codigo_barras, numero_lote, quantidade, data_fabricacao, data_validade, chave_nfe, origem)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'NF-e')
                            """,
                            (
                                produto_id,
                                codigo,
                                lote["numero_lote"],
                                lote["quantidade"],
                                lote["data_fabricacao"],
                                lote["data_validade"],
                                chave_nfe,
                            ),
                        )
                        lote_id = cursor.lastrowid
                        # Entrada de mercadoria vinculada ao lote que a originou.
                        cursor.execute(
                            "INSERT INTO entradas (produto_id, lote_id, quantidade) VALUES (?, ?, ?)",
                            (produto_id, lote_id, float(lote["quantidade"])),
                        )
                        delta_total = round(delta_total + float(lote["quantidade"]), 3)

                # Estoque agregado = delta dos lotes desta NF-e (idempotente).
                if abs(float(delta_total)) > 0.0000005:
                    cursor.execute(
                        """
                        UPDATE produtos
                        SET quantidade_atual = MAX(0, CAST(quantidade_atual AS REAL) + ?)
                        WHERE id = ?
                        """,
                        (float(delta_total), produto_id),
                    )

                # Validade agregada do produto = menor validade entre os lotes validos.
                self._atualizar_validade_agregada(cursor, produto_id)

        if itens:
            self._definir_imagem_cadastro()
            primeiro = itens[0]
            self.entry_barcode.delete(0, "end")
            self.entry_barcode.insert(0, str(primeiro.get("ean") or "").strip())
            self.ent_nome.delete(0, "end")
            self.ent_nome.insert(0, str(primeiro.get("descricao") or "").strip())
            self.ent_ncm.delete(0, "end")
            self.ent_ncm.insert(0, str(primeiro.get("ncm") or "").strip())
            self.ent_val.delete(0, "end")
            self.ent_val.insert(0, normalizar_data_iso(primeiro.get("validade")))
            preco_primeiro = float(primeiro.get("preco") or 0.0)
            self._preencher_precificacao(
                custo=f"{preco_primeiro:.2f}".replace(".", ","),
                margem="0",
                preco=f"{preco_primeiro:.2f}".replace(".", ","),
                margem_manual=False,
            )
            self.current_editing_id = None
            self.produto_selecionado = None
            self.btn_save.configure(state="normal")
            self.btn_edit_sel.configure(state="disabled")

        self.recarregar_primeira_pagina()
        self._safe_focus(self.entry_barcode)

        msg = f"Importação concluída com sucesso: {cadastrados} produtos cadastrados"
        if reimportadas:
            msg += f" | {reimportadas} item(ns) de NF-e já importada (estoque não duplicado)"
        self.lbl_alerta.configure(text=msg, text_color="#66ff99")
        messagebox.showinfo("Importação NF-e", msg)
        registrar_log(None, "Importação NF-e", "Sucesso", f"Chave {chave_nfe} | novos={cadastrados} | reimportadas={reimportadas} | total_itens={len(itens)}")

    def _widgets_tab_order(self):
        ordem = [
            self.entry_barcode,
            self.entry_chave_nfe if self.frame_importar_nfe.winfo_ismapped() else None,
            self.ent_nome,
            self.ent_variacao,
            getattr(self, "opt_unidade", None),
            self.ent_ncm,
            self.ent_preco_custo,
            self.ent_margem_lucro,
            self.ent_preco_venda,
            self.ent_qtd,
            self.ent_val,
            self.ent_qtd_min,
            self.btn_save,
            self.btn_edit_sel,
            self.btn_limpar,
        ]
        return [w for w in ordem if w is not None and w.winfo_exists()]

    def _navegar_tab(self, event, step=1):
        widgets = self._widgets_tab_order()
        if not widgets:
            return "break"

        atual = event.widget
        # CTkEntry entrega eventos pelo Entry interno; botões também têm filhos.
        while atual not in widgets and getattr(atual, "master", None) is not None:
            atual = atual.master
        if atual not in widgets:
            self._safe_focus(widgets[0])
            return "break"

        idx = widgets.index(atual)
        prox = widgets[(idx + step) % len(widgets)]
        self._safe_focus(prox)
        return "break"

    def _configurar_navegacao_tab(self):
        for widget in [
            self.entry_barcode,
            self.entry_chave_nfe,
            self.ent_nome,
            self.ent_variacao,
            getattr(self, "opt_unidade", None),
            self.ent_ncm,
            self.ent_preco_custo,
            self.ent_margem_lucro,
            self.ent_preco_venda,
            self.ent_qtd,
            self.ent_val,
            self.ent_qtd_min,
            self.btn_save,
            self.btn_edit_sel,
            self.btn_limpar,
        ]:
            widget.bind("<Tab>", lambda e: self._navegar_tab(e, 1), add="+")
            widget.bind("<Shift-Tab>", lambda e: self._navegar_tab(e, -1), add="+")

    def abrir_janela_produtos_cadastrados(self):
        """Abre janela INDEPENDENTE com a listagem dos produtos em grade
        (ttk.Treeview — formato planilha), dedicada à consulta/seleção.

        Reutiliza os métodos existentes: _consultar_produtos_paginados,
        _classificar_tipo_codigo, _formatar_qtd_exibicao,
        preencher_campos_cadastro (edição) e deletar_produto (exclusão).
        Navegação: mouse, ↑/↓, ENTER seleciona (preenche o cadastro e fecha),
        duplo-clique também seleciona.
        """
        try:
            if self._janela_produtos is not None and self._janela_produtos.winfo_exists():
                self._janela_produtos.deiconify()
                self._janela_produtos.lift()
                self._janela_produtos.focus_set()
                return
        except Exception:
            pass

        janela = ctk.CTkToplevel(self)
        self._janela_produtos = janela
        janela.title("Produtos Cadastrados — Consulta e Seleção")
        janela.geometry("1280x640")
        # CONTROLES NATIVOS DA JANELA (minimizar / maximizar-restaurar / fechar):
        # sem transient() o wrapper do Windows é criado como janela normal
        # (WS_OVERLAPPEDWINDOW). Com transient() o Tk cria o wrapper como
        # WS_POPUP e os botões minimizar/maximizar deixam de existir
        # (medido em Tk 8.6.12 nesta máquina). O grab_set() do bloco abaixo é
        # mantido: ele não interfere nos controles da barra de títulos.
        janela.resizable(True, True)
        janela.minsize(1000, 420)
        try:
            janela.after(150, lambda: (janela.grab_set(), janela.lift()))
        except Exception:
            pass

        topo = ctk.CTkFrame(janela, fg_color="transparent")
        topo.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(topo, text="Selecione um produto e pressione ENTER (ou use ✎ Editar)", font=("Arial", 11, "bold")).pack(side="left")
        ctk.CTkButton(topo, text="ATUALIZAR", width=110, height=28, fg_color="#2c3e50",
                      command=lambda: self._carregar_grade_produtos(arvore, lbl_contagem)).pack(side="right", padx=6)
        ctk.CTkButton(topo, text="PRÓXIMA >>", width=110, height=28, fg_color="#2471a3",
                      command=lambda: _paginar(1)).pack(side="right", padx=6)
        ctk.CTkButton(topo, text="<< ANTERIOR", width=110, height=28, fg_color="#2471a3",
                      command=lambda: _paginar(-1)).pack(side="right", padx=6)
        lbl_contagem = ctk.CTkLabel(topo, text="", font=("Arial", 11))
        lbl_contagem.pack(side="right", padx=10)

        # Grade/planilha: Treeview com colunas rígidas + rolagem V e H.
        frame_grade = ctk.CTkFrame(janela)
        frame_grade.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        colunas = ("id", "codigo", "tipo", "nome", "variacao", "unidade", "ncm", "custo", "margem", "preco", "qtd", "validade")
        arvore = ttk.Treeview(frame_grade, columns=colunas, show="headings", selectmode="browse")
        titulos = {
            "id": "ID", "codigo": "CÓDIGO", "tipo": "TIPO CÓDIGO", "nome": "NOME",
            "variacao": "VARIAÇÃO", "unidade": "UNIDADE", "ncm": "NCM", "custo": "CUSTO", "margem": "MARGEM",
            "preco": "PREÇO", "qtd": "QUANTIDADE", "validade": "VALIDADE",
        }
        larguras = {"id": 55, "codigo": 120, "tipo": 90, "nome": 300, "variacao": 120, "unidade": 70,
                    "ncm": 90, "custo": 90, "margem": 75, "preco": 90, "qtd": 95, "validade": 100}
        ancoras = {"id": "center", "codigo": "w", "tipo": "center", "nome": "w", "variacao": "w", "unidade": "center",
                   "ncm": "center", "custo": "e", "margem": "e", "preco": "e", "qtd": "e", "validade": "center"}
        for c in colunas:
            arvore.heading(c, text=titulos[c])
            arvore.column(c, width=larguras[c], anchor=ancoras[c], stretch=False)

        vsb = ttk.Scrollbar(frame_grade, orient="vertical", command=arvore.yview)
        hsb = ttk.Scrollbar(frame_grade, orient="horizontal", command=arvore.xview)
        arvore.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        arvore.pack(side="left", fill="both", expand=True)

        mapa = {}
        self._mapa_grade_produtos = mapa
        self._grade_arvore = arvore
        self._grade_contagem = lbl_contagem

        def _selecionar_teclado(_event=None):
            sel = arvore.selection()
            if not sel:
                return "break"
            prod = mapa.get(sel[0])
            if prod is None:
                return "break"
            try:
                janela.grab_release()
            except Exception:
                pass
            janela.destroy()
            self.preencher_campos_cadastro(prod)
            self._safe_focus(self.ent_nome)
            return "break"

        def _excluir_teclado(_event=None):
            sel = arvore.selection()
            if not sel:
                return "break"
            prod = mapa.get(sel[0])
            if prod is not None:
                self.deletar_produto(prod[0])  # método existente (com confirmação)
                self._carregar_grade_produtos(arvore, lbl_contagem)
            return "break"

        arvore.bind("<Return>", _selecionar_teclado)
        arvore.bind("<KP_Enter>", _selecionar_teclado)
        arvore.bind("<Double-1>", _selecionar_teclado)
        janela.bind("<Return>", _selecionar_teclado)
        janela.bind("<Escape>", lambda _e: (janela.grab_release(), janela.destroy()))
        janela.bind("<Delete>", _excluir_teclado)

        # AÇÕES da grade (coluna lógica AÇÕES): EDITAR preenche o cadastro e
        # fecha; EXCLUIR reutiliza deletar_produto (confirmação incluída).
        rodape = ctk.CTkFrame(janela, fg_color="transparent")
        rodape.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(rodape, text="↑/↓ navegar  |  ENTER selecionar  |  DEL excluir",
                     font=("Arial", 10, "bold"), text_color="#bfc7d5").pack(side="left")
        ctk.CTkButton(rodape, text="EXCLUIR SELECIONADO", width=170, height=30,
                      fg_color="#c0392b", hover_color="#e74c3c",
                      command=_excluir_teclado).pack(side="right", padx=(6, 0))
        ctk.CTkButton(rodape, text="EDITAR SELECIONADO (ENTER)", width=210, height=30,
                      fg_color="#3B8ED0", hover_color="#5fa8e0",
                      command=_selecionar_teclado).pack(side="right")

        # Navegação da grade: reusa carregar_pagina_anterior/proxima, mas força
        # re-render da janela (carregar_produtos atualiza quando aberta).
        def _paginar(direcao):
            if direcao > 0:
                self.carregar_proxima_pagina()
            else:
                self.carregar_pagina_anterior()
            self._carregar_grade_produtos(arvore, lbl_contagem)

        # Primeira carga (lazy): usa a consulta paginada existente.
        self._carregar_grade_produtos(arvore, lbl_contagem)

    def _carregar_grade_produtos(self, arvore, lbl_contagem):
        """Recarrega a página corrente e renderiza na janela-grade informada."""
        self._grade_arvore = arvore
        self._grade_contagem = lbl_contagem
        self.carregar_produtos()

    def carregar_produtos_ao_abrir_aba(self):
        """Dispara a consulta de produtos somente quando a aba de estoque for aberta."""
        if self._estoque_carregado:
            return
        self.recarregar_primeira_pagina()

    def recarregar_primeira_pagina(self):
        self.current_offset = 0
        self.carregar_produtos()

    def carregar_pagina_anterior(self):
        if self.current_offset <= 0:
            return
        self.current_offset = max(0, self.current_offset - self.page_size)
        self.carregar_produtos()

    def carregar_proxima_pagina(self):
        proximo_offset = self.current_offset + self.page_size
        if proximo_offset >= self.total_produtos:
            return
        self.current_offset = proximo_offset
        self.carregar_produtos()

    def _consultar_produtos_paginados(self):
        """Executa a leitura paginada do estoque com conexão curta ao banco."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM produtos")
            total_produtos = cursor.fetchone()[0]
            cursor.execute(
                """
                  SELECT id, codigo_barras, nome, variacao, preco_venda, quantidade_atual, validade,
                       preco_custo, margem_lucro, quantidade_minima, ncm, imagem_path, unidade
                FROM produtos
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (self.page_size, self.current_offset),
            )
            produtos = cursor.fetchall()

        return produtos, total_produtos

    def _safe_focus(self, widget):
        try:
            if self.winfo_exists() and widget is not None and widget.winfo_exists():
                widget.focus_set()
        except Exception:
            pass

    def _formatar_qtd_exibicao(self, quantidade, unidade=""):
        """Exibe quantidade na listagem: inteiro em UN, decimal + KG em KG."""
        unidade_txt = str(unidade or "").strip().upper()
        if unidade_txt not in ("UN", "KG"):
            unidade_txt = "UN"
        try:
            valor = float(quantidade or 0)
        except (TypeError, ValueError):
            return f"{quantidade}{'' if unidade_txt == 'UN' else ' ' + unidade_txt}"
        if unidade_txt == "KG":
            return f"{valor:.3f}".replace(".", ",") + " KG"
        if valor.is_integer():
            return str(int(valor))
        return f"{valor:g}"

    def _parse_numero(self, texto, nome_campo, permitir_vazio=False, default=0.0, inteiro=False, minimo=0):
        """Converte texto para número com validação amigável para o usuário."""
        return parse_numero(
            texto,
            nome_campo,
            permitir_vazio=permitir_vazio,
            default=default,
            inteiro=inteiro,
            minimo=minimo,
        )

    def _set_preco_venda_texto(self, valor_texto):
        self.var_preco_venda.set(valor_texto)

    def _set_margem_lucro_texto(self, valor_texto):
        self.var_margem_lucro.set(valor_texto)

    def _set_badge_margem_manual(self, ativo):
        self._margem_ajustada_manual = ativo
        if ativo:
            self.lbl_badge_margem_manual.pack(pady=(4, 5), fill="x")
        else:
            self.lbl_badge_margem_manual.pack_forget()

        self.ent_margem_lucro.configure(
            fg_color=["#FFF3D6", "#5B4210"] if ativo else self._cor_margem_padrao,
            border_color=["#D28A00", "#F0B64D"] if ativo else self._cor_borda_margem_padrao,
            text_color=["#7A4B00", "#FFE6A8"] if ativo else self._cor_texto_margem_padrao,
        )

    def _preencher_precificacao(self, custo=None, margem=None, preco=None, margem_manual=False):
        self._atualizando_precificacao = True
        try:
            if custo is not None:
                self.var_preco_custo.set(custo)
            if margem is not None:
                margem_num = self._parse_numero(margem, "Margem", permitir_vazio=True, default=0.0)
                self.var_margem_lucro.set(str(int(round(margem_num))))
            if preco is not None:
                self.var_preco_venda.set(preco)
        finally:
            self._atualizando_precificacao = False
        self._set_badge_margem_manual(margem_manual)

    def _atualizar_preco_venda_automatico(self, *args):
        if self._atualizando_precificacao:
            return

        custo_texto = self.ent_preco_custo.get().strip()
        margem_texto = self.ent_margem_lucro.get().strip()

        if not custo_texto:
            self._set_preco_venda_texto("")
            self._set_badge_margem_manual(False)
            return

        try:
            preco_custo = self._parse_numero(custo_texto, "Preço de custo", permitir_vazio=False)
            margem_lucro = self._parse_numero(margem_texto, "Margem de lucro", permitir_vazio=True, default=0.0)
        except ValueError:
            return

        preco_venda = calcular_preco_venda(preco_custo, margem_lucro)
        self._atualizando_precificacao = True
        try:
            self._set_preco_venda_texto(f"{preco_venda:.2f}".replace('.', ','))
        finally:
            self._atualizando_precificacao = False
        self._set_badge_margem_manual(False)

    def _atualizar_margem_por_preco_manual(self, *args):
        if self._atualizando_precificacao:
            return

        custo_texto = self.ent_preco_custo.get().strip()
        preco_texto = self.ent_preco_venda.get().strip()
        margem_atual_texto = self.ent_margem_lucro.get().strip()

        if not custo_texto or not preco_texto:
            self._set_badge_margem_manual(False)
            return

        try:
            preco_custo = self._parse_numero(custo_texto, "Preço de custo", permitir_vazio=False)
            preco_venda = self._parse_numero(preco_texto, "Preço de venda", permitir_vazio=False)
            margem_atual = self._parse_numero(margem_atual_texto, "Margem de lucro", permitir_vazio=True, default=0.0)
        except ValueError:
            return

        if preco_custo <= 0:
            self._set_badge_margem_manual(False)
            return

        margem_calculada = round(((preco_venda / preco_custo) - 1) * 100, 2)
        margem_manual = abs(margem_calculada - margem_atual) > 0.009

        self._atualizando_precificacao = True
        try:
            self._set_margem_lucro_texto(str(int(round(margem_calculada))))
        finally:
            self._atualizando_precificacao = False

        self._set_badge_margem_manual(margem_manual)

    def _gerar_codigo_interno_sequencial(self):
        """Gera código interno numérico único para produtos sem código de barras informado."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS config_sistema (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                )
                """
            )

            cursor.execute("SELECT valor FROM config_sistema WHERE chave = 'proximo_codigo_interno'")
            seq_row = cursor.fetchone()

            cursor.execute(
                """
                SELECT MAX(CAST(codigo_barras AS INTEGER))
                FROM produtos
                WHERE codigo_barras GLOB '[0-9]*'
                  AND LENGTH(codigo_barras) <= 6
                  AND CAST(codigo_barras AS INTEGER) >= 1000
                """
            )
            max_row = cursor.fetchone()
            max_curto = int(max_row[0]) if max_row and max_row[0] is not None else 999

            if seq_row and str(seq_row[0]).strip().isdigit():
                candidato = max(1000, int(str(seq_row[0]).strip()))
            else:
                candidato = max(1000, max_curto + 1)

            while True:
                codigo = str(candidato)
                cursor.execute("SELECT 1 FROM produtos WHERE codigo_barras = ? LIMIT 1", (codigo,))
                if not cursor.fetchone():
                    break
                candidato += 1

            cursor.execute(
                """
                INSERT INTO config_sistema (chave, valor) VALUES ('proximo_codigo_interno', ?)
                ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
                """,
                (str(candidato + 1),),
            )

            return str(candidato)

    def _classificar_tipo_codigo(self, codigo_barras):
        """Classifica código para badge visual na listagem do estoque."""
        codigo = str(codigo_barras or "").strip()
        if not codigo:
            return "SEM CÓDIGO", "#6c757d"

        if codigo.isdigit():
            try:
                numero = int(codigo)
                if len(codigo) <= 6 and numero >= 1000:
                    return "INTERNO", "#1f6aa5"
            except ValueError:
                pass

        return "REAL", "#2e7d32"

    def _definir_imagem_cadastro(self, caminho=""):
        """Troca o estado da imagem e invalida respostas de cadastros anteriores."""
        self._contexto_imagem = object()
        self.temp_image_path = caminho or ""
        self.lbl_preview_img.configure(text="Produto\nCarregado" if caminho else "Sem Imagem")

    def selecionar_imagem_manual(self):
        from tkinter import filedialog
        caminho = filedialog.askopenfilename(filetypes=[("Imagens", "*.jpg *.png *.jpeg")])
        if caminho:
            self._definir_imagem_cadastro(caminho)
            self.lbl_preview_img.configure(text="Imagem\nSelecionada", text_color="cyan")

    def preencher_campos_cadastro(self, prod):
        """Preenche o painel de cadastro com dados de um produto existente."""
        self.current_editing_id = prod[0]
        self.entry_barcode.delete(0, 'end')
        self.entry_barcode.insert(0, str(prod[1] or ""))
        self.ent_nome.delete(0, 'end')
        self.ent_nome.insert(0, prod[2])
        self.ent_variacao.delete(0, 'end')
        self.ent_variacao.insert(0, str(prod[3] or ""))
        # FASE 1 UN/KG: unidade persistida tem precedência; banco antigo sem
        # coluna/valor cai no fallback legado (variação/categoria/nome).
        unidade_produto = str(prod[12] if len(prod) > 12 and prod[12] is not None else "").strip().upper()
        if unidade_produto not in ("UN", "KG"):
            nome_legado = str(prod[2] or "")
            variacao_legado = str(prod[3] or "")
            unidade_produto = "KG" if produto_e_vendido_por_kg(nome_legado, variacao_legado) else "UN"
        self.var_unidade.set(unidade_produto)
        self._atualizar_label_preco_venda()
        self.ent_ncm.delete(0, 'end')
        self.ent_ncm.insert(0, str(prod[10] or ""))
        custo = float(prod[7] if prod[7] is not None else 0.0)
        margem = float(prod[8] if prod[8] is not None else 0.0)
        preco = float(prod[4] if prod[4] is not None else 0.0)
        preco_regra = calcular_preco_venda(custo, margem)
        margem_manual = abs(preco - preco_regra) > 0.009
        self._preencher_precificacao(
            custo=f"{custo:.2f}".replace('.', ','),
            margem=str(int(round(margem))),
            preco=f"{preco:.2f}".replace('.', ','),
            margem_manual=margem_manual,
        )
        self.ent_qtd.delete(0, 'end')
        self.ent_qtd.insert(0, self._formatar_qtd_tela(prod[5], unidade_produto))
        self.ent_val.delete(0, 'end')
        self.ent_val.insert(0, str(prod[6]) if prod[6] else "")
        self.ent_qtd_min.delete(0, 'end')
        self.ent_qtd_min.insert(0, self._formatar_qtd_tela(prod[9] if prod[9] is not None else 0, unidade_produto))

        self.produto_selecionado = prod
        self.btn_save.configure(state="normal")
        self.btn_edit_sel.configure(state="disabled")
        self.btn_excluir.configure(state="normal")
        self._definir_imagem_cadastro(prod[11] if len(prod) > 11 else "")

    def editar_produto_selecionado(self):
        """Carrega os dados do produto selecionado na listagem para alteração e salvamento."""
        if not self.produto_selecionado:
            messagebox.showwarning(
                "Nenhum produto selecionado",
                "Selecione um produto na listagem para editar.\n"
                "Clique em uma linha da tabela de produtos.",
            )
            return
        self.preencher_campos_cadastro(self.produto_selecionado)

    def _selecionar_linha(self, prod, row_frame=None):
        """Marca o produto selecionado (compatibilidade: a seleção visual hoje
        acontece na janela-grade via Treeview; aqui apenas registra o produto
        e habilita os botões EDITAR/EXCLUIR do cadastro)."""
        self.produto_selecionado = prod
        try:
            self.btn_edit_sel.configure(state="normal")
            self.btn_excluir.configure(state="normal")
        except Exception:
            pass

    def limpar_campos(self):
        self.current_editing_id = None
        self.produto_selecionado = None
        self.row_selecionada = None
        self._definir_imagem_cadastro()
        self.entry_barcode.delete(0, 'end')
        self.ent_nome.delete(0, 'end')
        self.ent_variacao.delete(0, 'end')
        self.ent_ncm.delete(0, 'end')
        self._preencher_precificacao(custo="", margem="", preco="", margem_manual=False)
        self.ent_qtd.delete(0, 'end')
        self.ent_val.delete(0, 'end')
        self.ent_qtd_min.delete(0, 'end')
        # FASE 1 UN/KG: novo cadastro volta ao padrão UN.
        try:
            self.var_unidade.set("UN")
            self._atualizar_label_preco_venda()
        except Exception:
            pass
        self.lbl_preview_img.configure(text="Sem Imagem")
        self.btn_save.configure(state="normal")
        self.btn_edit_sel.configure(state="disabled")
        self.btn_excluir.configure(state="disabled")

    def carregar_produtos(self):
        """Recarrega a página corrente e renderiza na JANELA-GRADE de produtos.

        A listagem não vive mais na tela de cadastro: se a janela-grade não
        estiver aberta, apenas a contagem/estado interno é atualizado (a grade
        será renderizada ao abrir). Reutiliza _consultar_produtos_paginados,
        _classificar_tipo_codigo, _formatar_qtd_exibicao, preencher_campos_cadastro
        e deletar_produto (nenhuma segunda lógica de consulta/edição/exclusão).
        """
        self.row_selecionada = None
        try:
            produtos, self.total_produtos = self._consultar_produtos_paginados()
            self._estoque_carregado = True
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao carregar estoque: {e}")
            return

        arvore = getattr(self, "_grade_arvore", None)
        lbl_contagem = getattr(self, "_grade_contagem", None)
        if arvore is None or not arvore.winfo_exists():
            # Janela-grade fechada: nada a renderizar agora.
            return

        # Limpa a grade e repovoa com a página corrente.
        for iid in arvore.get_children():
            arvore.delete(iid)
        mapa = getattr(self, "_mapa_grade_produtos", {})
        mapa.clear()

        hoje = datetime.now()
        alerta_count = 0

        for prod in produtos:
            cor_validade = ""
            try:
                data_validade = datetime.strptime(prod[6], "%Y-%m-%d")
                if data_validade < hoje + timedelta(days=30):
                    cor_validade = "#FF5555"  # Vermelho claro/alerta
                    alerta_count += 1
            except Exception:
                pass

            tipo_codigo, _cor_badge = self._classificar_tipo_codigo(prod[1])
            unidade_txt = prod[12] if len(prod) > 12 else ""
            # MAPEAMENTO VISUAL DA GRADE (12 colunas do Treeview).
            # A célula de UNIDADE precisa ser montada ANTES da célula de NCM;
            # sem ela a cauda inteira deslocava uma posição (NCM caía em
            # UNIDADE, custo em NCM, margem em CUSTO, preço em MARGEM,
            # quantidade em PREÇO e validade em QUANTIDADE).
            # Unidade persistida tem precedência; banco antigo sem valor cai no
            # mesmo fallback legado usado em preencher_campos_cadastro().
            unidade_exibicao = str(unidade_txt or "").strip().upper()
            if unidade_exibicao not in ("UN", "KG"):
                unidade_exibicao = (
                    "KG" if produto_e_vendido_por_kg(str(prod[2] or ""), str(prod[3] or "")) else "UN"
                )
            valores = (
                str(prod[0]),                                       # ID
                prod[1] or "(sem código)",                          # CÓDIGO
                tipo_codigo,                                        # TIPO CÓDIGO
                prod[2],                                            # NOME
                # VARIAÇÃO: códigos de unidade (UN/UND/UNID/KG/CX/PC)
                # exibem célula vazia; texto real de variação, normal.
                variacao_exibicao_grade(prod[3]),
                unidade_exibicao,                                   # UNIDADE
                str(prod[10] or "-"),                               # NCM
                f"R$ {float(prod[7] or 0):.2f}",                    # CUSTO
                formatar_percentual_inteiro(prod[8]),               # MARGEM
                f"R$ {float(prod[4] or 0):.2f}",                    # PREÇO
                self._formatar_qtd_exibicao(prod[5], unidade_txt),  # QUANTIDADE
                str(prod[6] or "-"),                                # VALIDADE
            )
            iid = str(prod[0])
            mapa[iid] = prod
            arvore.insert("", "end", iid=iid, values=valores)
            if cor_validade:
                try:
                    arvore.tag_configure("validade", foreground="#FF5555")
                    arvore.item(iid, tags=("validade",))
                except Exception:
                    pass

            # AÇÕES por linha: EDITAR (preenche o cadastro) e EXCLUIR.
            # Em Treeview não há widgets embutidos: ações ficam nos botões
            # da janela (reuso de preencher_campos_cadastro / deletar_produto).

        if lbl_contagem is not None and lbl_contagem.winfo_exists():
            if self.total_produtos > 0:
                inicio = self.current_offset + 1
                fim = min(self.current_offset + self.page_size, self.total_produtos)
                txt = f"Exibindo {inicio}-{fim} de {self.total_produtos} produtos"
            else:
                txt = "Nenhum produto cadastrado"
            if alerta_count > 0:
                txt += f"  |  ⚠️ {alerta_count} com validade próxima/vencidos"
            lbl_contagem.configure(text=txt)

        # Seleciona o primeiro item para navegação imediata por ↑/↓.
        filhos = arvore.get_children()
        if filhos:
            arvore.selection_set(filhos[0])
            arvore.focus(filhos[0])

    def buscar_por_barcode(self, event=None):
        """Filtra ou destaca o produto pelo código de barras."""
        code = self.entry_barcode.get().strip()
        if not code:
            return "break"
        self.lbl_alerta.configure(text="")

        produto = None
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                          SELECT id, codigo_barras, nome, variacao, preco_venda, quantidade_atual, validade,
                           preco_custo, margem_lucro, quantidade_minima, ncm, imagem_path, unidade
                    FROM produtos
                    WHERE codigo_barras = ?
                    """,
                    (code,),
                )
                produto = cursor.fetchone()
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao consultar produto no banco: {e}")
            registrar_log(None, "Busca de Produto por Código de Barras", "Falha", f"Erro: {e}")
            return

        if produto:
            registrar_log(None, "Busca de Produto por Código de Barras", "Sucesso", f"Produto {code} encontrado.")
            self.preencher_campos_cadastro(produto)
        else:
            # Produto novo: abre cadastro imediatamente e segue com consulta inteligente em background.
            self._definir_imagem_cadastro()
            self.current_editing_id = None
            self.produto_selecionado = None
            self.ent_nome.delete(0, "end")
            self.ent_variacao.delete(0, "end")
            self.ent_ncm.delete(0, "end")
            self._preencher_precificacao(custo="", margem="", preco="", margem_manual=False)
            self.ent_qtd.delete(0, "end")
            self.ent_qtd_min.delete(0, "end")
            self.ent_val.delete(0, "end")
            try:
                self.var_unidade.set("UN")
                self._atualizar_label_preco_venda()
            except Exception:
                pass
            self.btn_save.configure(state="normal")
            self.btn_edit_sel.configure(state="disabled")
            self._safe_focus(self.ent_nome)

            self.lbl_alerta.configure(text=f"Produto não encontrado no banco. Cadastro aberto para o código {code}.", text_color="orange")
            threading.Thread(target=self.consultar_api_inteligente, args=(code, self._contexto_imagem), daemon=True).start()

        return "break"

    def consultar_api_inteligente(self, barcode, contexto):
        """Consulta a API Open Food Facts e abre o cadastro pré-preenchido."""
        import requests

        url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == 1:
                    p = data["product"]
                    nome = p.get("product_name", "")
                    marca = p.get("brands", "Fabricante Desconhecido")
                    img_url = p.get("image_front_url")
                    
                    path_local = ""
                    if img_url:
                        path_local = self.baixar_imagem(img_url, barcode)
                    
                    if self.winfo_exists():
                        self.after(0, lambda: self._aplicar_resposta_api(contexto, barcode, (nome, marca, path_local)))
                    return
        except Exception as e:
            print(f"Erro na API: {e}")
        
        # Se falhar ou não encontrar, abre manual
        if self.winfo_exists():
            self.after(0, lambda: self._aplicar_resposta_api(contexto, barcode))

    def _aplicar_resposta_api(self, contexto, barcode, dados=None):
        """Valida o contexto na thread da interface antes de alterar o cadastro."""
        if not self.winfo_exists():
            return
        if contexto is not getattr(self, "_contexto_imagem", None):
            return
        if self.current_editing_id is not None or self.entry_barcode.get().strip() != barcode:
            return
        if dados is None:
            self.lbl_alerta.configure(text="Produto não encontrado na API. Cadastre manualmente.", text_color="orange")
        else:
            self._preencher_api(*dados)

    def _preencher_api(self, nome, marca, img_path):
        self.ent_nome.delete(0, 'end')
        self.ent_nome.insert(0, f"{nome} ({marca})")
        self._preencher_precificacao(custo="0", margem="0", preco="0,00", margem_manual=False)
        self.temp_image_path = img_path
        self.lbl_preview_img.configure(text="Imagem API", text_color="green")

    def baixar_imagem(self, url, barcode):
        """Faz o download da imagem e salva na pasta de dados do usuário."""
        import requests

        try:
            ext = url.split(".")[-1]
            nome_img = f"{barcode}.{ext}"
            caminho_completo = os.path.join(self.pasta_imagens, nome_img)
            
            img_data = requests.get(url, timeout=8).content
            with open(caminho_completo, 'wb') as handler:
                handler.write(img_data)
            return caminho_completo
        except:
            return ""

    def salvar_produto(self):
        """Salva novo produto ou atualiza o existente usando dados do painel."""
        try:
            barcode = self.entry_barcode.get().strip()
            nome = self.ent_nome.get().strip()
            variacao = self.ent_variacao.get().strip()
            barcode_gerado = False

            if not barcode:
                barcode = self._gerar_codigo_interno_sequencial()
                barcode_gerado = True
                self.entry_barcode.insert(0, barcode)

            preco_custo = self._parse_numero(self.ent_preco_custo.get(), "Preço de custo", permitir_vazio=False)
            margem_lucro = self._parse_numero(self.ent_margem_lucro.get(), "Margem de lucro", permitir_vazio=True, default=0.0)
            preco_venda_digitado = self.ent_preco_venda.get().strip()
            preco_venda = self._parse_numero(
                preco_venda_digitado,
                "Preço de venda",
                permitir_vazio=not bool(preco_venda_digitado),
                default=calcular_preco_venda(preco_custo, margem_lucro),
            )
            ncm = self.ent_ncm.get().strip()
            unidade = self._obter_unidade_tela()
            if unidade == "KG":
                qtd = self._parse_numero(self.ent_qtd.get(), "Estoque", permitir_vazio=False, minimo=0)
                qtd = round(float(qtd), 3)
                qtd_min = self._parse_numero(self.ent_qtd_min.get(), "Quantidade mínima", permitir_vazio=True, default=0, minimo=0)
                qtd_min = round(float(qtd_min), 3)
            else:
                qtd = self._parse_numero(self.ent_qtd.get(), "Estoque", permitir_vazio=False, inteiro=True)
                qtd_min = self._parse_numero(self.ent_qtd_min.get(), "Quantidade mínima", permitir_vazio=True, default=0, inteiro=True)
            # Normaliza para AAAA-MM-DD quando a data for interpretável; preserva o
            # texto digitado apenas se não corresponder a nenhum formato conhecido.
            validade_digitada = self.ent_val.get().strip()
            validade = normalizar_data_iso(validade_digitada) or validade_digitada

            self.ent_preco_venda.delete(0, 'end')
            self.ent_preco_venda.insert(0, f"{preco_venda:.2f}")

            with get_db_connection() as conn:
                cursor = conn.cursor()
                if self.current_editing_id:
                    if produto_tem_lotes_com_saldo(cursor, self.current_editing_id):
                        # 4B-4: estoque gerido por lotes — quantidade_atual não
                        # pode ser sobrescrita pelo formulário. Edição cadastral
                        # prossegue apenas com a quantidade digitada igual ao
                        # agregado atual (coluna quantidade_atual omitida).
                        linha_estoque = cursor.execute(
                            "SELECT quantidade_atual FROM produtos WHERE id = ?",
                            (self.current_editing_id,),
                        ).fetchone()
                        estoque_atual = float(linha_estoque[0] or 0) if linha_estoque else 0.0
                        if abs(float(qtd) - estoque_atual) > 0.0005:
                            raise ValueError(
                                f"Estoque do produto ID {self.current_editing_id} é controlado por lotes "
                                f"(saldo atual: {estoque_atual}). Alteração manual de quantidade bloqueada "
                                "(4B-4): use a entrada de mercadoria (NF-e) para movimentar o estoque e "
                                "manter produtos/produto_lotes coerentes."
                            )
                        cursor.execute("""
                            UPDATE produtos
                            SET codigo_barras = ?, nome = ?, variacao = ?, unidade = ?, ncm = ?, preco_custo = ?, margem_lucro = ?, preco_venda = ?,
                                quantidade_minima = ?, validade = ?, imagem_path = COALESCE(NULLIF(?, ''), imagem_path)
                            WHERE id = ?
                        """, (barcode, nome, variacao, unidade, ncm, preco_custo, margem_lucro, preco_venda, qtd_min, validade, self.temp_image_path, self.current_editing_id))
                        if hasattr(self, "_atualizar_validade_agregada"):
                            self._atualizar_validade_agregada(cursor, self.current_editing_id)
                        registrar_log(None, "Edição Produto", "Sucesso", f"ID {self.current_editing_id} atualizado (estoque gerido por lotes; quantidade_atual preservada).", conn=conn)
                    else:
                        cursor.execute("""
                            UPDATE produtos
                            SET codigo_barras = ?, nome = ?, variacao = ?, unidade = ?, ncm = ?, preco_custo = ?, margem_lucro = ?, preco_venda = ?,
                                quantidade_atual = ?, quantidade_minima = ?, validade = ?, imagem_path = COALESCE(NULLIF(?, ''), imagem_path)
                            WHERE id = ?
                        """, (barcode, nome, variacao, unidade, ncm, preco_custo, margem_lucro, preco_venda, qtd, qtd_min, validade, self.temp_image_path, self.current_editing_id))
                        registrar_log(None, "Edição Produto", "Sucesso", f"ID {self.current_editing_id} atualizado.", conn=conn)
                else:
                    cursor.execute("""
                        INSERT INTO produtos (
                            codigo_barras, nome, variacao, unidade, ncm, preco_custo, margem_lucro, preco_venda,
                            quantidade_atual, quantidade_minima, validade, imagem_path
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (barcode, nome, variacao, unidade, ncm, preco_custo, margem_lucro, preco_venda, qtd, qtd_min, validade, self.temp_image_path))
                    registrar_log(None, "Novo Produto", "Sucesso", f"Barcode {barcode} cadastrado.", conn=conn)

            if barcode_gerado:
                messagebox.showinfo("Sucesso", f"Produto processado com sucesso! Código interno gerado: {barcode}")
            else:
                messagebox.showinfo("Sucesso", "Produto processado com sucesso!")
            self.limpar_campos()
            self.recarregar_primeira_pagina()
            self._safe_focus(self.entry_barcode)
        except ValueError as e:
            messagebox.showwarning("Campos numéricos inválidos", str(e))
            self._safe_focus(self.ent_preco_custo)
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao salvar: {e}")

    def excluir_produto_selecionado(self):
        """Exclui o produto selecionado (listagem ou formulário) com confirmação prévia."""
        prod = self.produto_selecionado
        id_produto = prod[0] if prod else self.current_editing_id
        if not id_produto:
            messagebox.showwarning(
                "Nenhum produto selecionado",
                "Selecione um produto na listagem para excluir.\n"
                "Clique em uma linha da tabela de produtos.",
                parent=self,
            )
            return

        nome = str(prod[2]) if prod else ""
        if not messagebox.askyesno(
            "Confirmação",
            f"Excluir o produto '{nome}' (ID {id_produto})?\nEsta ação não pode ser desfeita.",
            parent=self,
        ):
            return

        self.deletar_produto(id_produto, confirmar=False)
        self.limpar_campos()

    def deletar_produto(self, id_produto, confirmar=True):
        """Remove o produto do banco local (com confirmação prévia por padrão)."""
        if confirmar and not messagebox.askyesno(
            "Confirmação",
            "Tem certeza que deseja excluir este produto?\nEsta ação não pode ser desfeita.",
            parent=self,
        ):
            return
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                dependencias = dependencias_exclusao_produto(cursor, id_produto)
                if dependencias:
                    raise ValueError(
                        f"Produto ID {id_produto} não pode ser excluído: existem registros "
                        f"vinculados em {', '.join(dependencias)}. Exclusão bloqueada (4B-4) "
                        "para preservar a rastreabilidade de lotes/vendas/entradas."
                    )
                cursor.execute("DELETE FROM produtos WHERE id = ?", (id_produto,))
            if hasattr(self, "recarregar_primeira_pagina"):
                self.recarregar_primeira_pagina()
            registrar_log(None, "Exclusão de Produto", "Sucesso", f"Produto ID {id_produto} excluído.")
        except ValueError as e:
            messagebox.showwarning("Exclusão bloqueada", str(e), parent=self)
            registrar_log(None, "Exclusão de Produto", "Bloqueada", str(e))
        except sqlite3.IntegrityError:
            messagebox.showerror("Erro", "Não é possível deletar: produto possui histórico de vendas/entradas.", parent=self)
            registrar_log(None, "Exclusão de Produto", "Falha", f"Produto ID {id_produto} não pode ser excluído devido a FK.")
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao deletar: {e}", parent=self)
            registrar_log(None, "Exclusão de Produto", "Falha", f"Erro inesperado ao excluir produto ID {id_produto}: {e}")

if __name__ == "__main__":
    # Script de teste
    root = ctk.CTk()
    def abrir(): ModuloEstoque()
    ctk.CTkButton(root, text="Abrir Estoque", command=abrir).pack(pady=50, padx=50)
    root.mainloop()