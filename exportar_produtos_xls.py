import csv
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.abspath(os.path.join(BASE_DIR, "data", "mercado.db"))
DEFAULT_OUTPUT_PATH = os.path.abspath(os.path.join(BASE_DIR, "produtos_exportados.csv"))


def gerar_nome_arquivo_exportacao():
    agora = datetime.now()
    return f"produtos_{agora.strftime('%d%m%Y_%H%M%S')}.csv"


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

    dados = []
    for row in rows:
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
        dados.append(["" if v is None else v for v in values])

    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".csv":
        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(headers)
            writer.writerows(dados)
    elif ext == ".xlsx":
        try:
            from openpyxl import Workbook
        except ImportError as exc:
            raise RuntimeError(
                "Para exportar em .xlsx é necessário openpyxl. Use .csv ou instale: python -m pip install openpyxl"
            ) from exc

        wb = Workbook()
        ws = wb.active
        ws.title = "Produtos"
        ws.append(headers)
        for linha in dados:
            ws.append(linha)
        wb.save(output_path)
    else:
        raise ValueError("Formato de saída inválido. Use .csv ou .xlsx.")
    return output_path


if __name__ == "__main__":
    output_path = os.path.abspath(os.path.join(BASE_DIR, gerar_nome_arquivo_exportacao()))
    if len(sys.argv) > 1:
        output_path = os.path.abspath(sys.argv[1])

    try:
        file_path = exportar_produtos_xls(output_path=output_path)
        print(f"Arquivo gerado com sucesso: {file_path}")
        print(f"Produtos exportados em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    except Exception as exc:
        print(f"Erro ao exportar produtos: {exc}", file=sys.stderr)
        sys.exit(1)
