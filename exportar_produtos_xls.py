import os
import sqlite3
import sys
from datetime import datetime

try:
    import xlwt
except ImportError as exc:
    raise SystemExit(
        "Biblioteca xlwt não encontrada. Instale com: python -m pip install xlwt"
    ) from exc

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.abspath(os.path.join(BASE_DIR, "data", "mercado.db"))
DEFAULT_OUTPUT_PATH = os.path.abspath(os.path.join(BASE_DIR, "produtos_exportados.xls"))


def gerar_nome_arquivo_exportacao():
    agora = datetime.now()
    return f"produtos_{agora.strftime('%d%m%Y_%H%M%S')}.xls"


def exportar_produtos_xls(output_path: str = DEFAULT_OUTPUT_PATH, db_path: str = DB_PATH):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Banco de dados não encontrado: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        query = """
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
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

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

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Produtos")

    header_style = xlwt.easyxf("font: bold on; pattern: pattern solid, fore_colour gray25; align: horiz center")
    body_style = xlwt.easyxf("align: vert center")

    for col_index, header in enumerate(headers):
        ws.write(0, col_index, header, header_style)

    for row_index, row in enumerate(rows, start=1):
        values = [
            row["id"],
            row["codigo_barras"],
            row["nome"],
            row["variacao"],
            row["ncm"],
            row["aliquota_icms"],
            row["aliquota_pis"],
            row["aliquota_cofins"],
            row["aliquota_ibs"],
            row["aliquota_cbs"],
            row["preco_custo"],
            row["margem_lucro"],
            row["preco_venda"],
            row["quantidade_atual"],
            row["quantidade_minima"],
            row["validade"],
            row["categoria"],
            row["preco_base"],
            row["inicio_promocao"],
            row["fim_promocao"],
            row["imagem_path"],
        ]

        for col_index, value in enumerate(values):
            if value is None:
                value = ""
            ws.write(row_index, col_index, value, body_style)

    for col_index in range(len(headers)):
        ws.col(col_index).width = 2200

    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    wb.save(output_path)
    return output_path


if __name__ == "__main__":
    output_path = os.path.abspath(os.path.join(BASE_DIR, gerar_nome_arquivo_exportacao()))
    if len(sys.argv) > 1:
        output_path = os.path.abspath(sys.argv[1])

    try:
        file_path = exportar_produtos_xls(output_path=output_path)
        print(f"Arquivo XLS gerado com sucesso: {file_path}")
        print(f"Produtos exportados em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    except Exception as exc:
        print(f"Erro ao exportar produtos: {exc}", file=sys.stderr)
        sys.exit(1)
