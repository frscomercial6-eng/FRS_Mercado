from __future__ import annotations

import csv
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, List, Sequence

from database_manager import get_db_connection


DEFAULT_GDOOR_ROOT = Path(r"C:\Users\Filipe\Desktop\GDOOR Sistemas")
DEFAULT_OUTPUT_EXCEL = Path("produtos_gdoor.xlsx")


def _buscar_fbclient_dll() -> str | None:
    candidatos = []
    env_path = os.environ.get("FIREBIRD_CLIENT") or os.environ.get("FBCLIENT")
    if env_path:
        candidatos.append(env_path)

    roots = [
        r"C:\Program Files\Firebird",
        r"C:\Program Files\Firebird\Firebird_5_0",
        r"C:\Program Files\Firebird\Firebird_5_0\bin",
        r"C:\Program Files\Firebird\Firebird_3_0",
        r"C:\Program Files\Firebird\Firebird_3_0\bin",
        r"C:\Program Files (x86)\Firebird",
        r"C:\Program Files\FirebirdSQL",
        r"C:\Program Files (x86)\FirebirdSQL",
        r"C:\Firebird",
        os.path.dirname(sys.executable),
        str(Path(__file__).resolve().parent),
    ]

    for root in roots:
        if root and os.path.exists(root):
            for base, _, files in os.walk(root):
                for nome in files:
                    if nome.lower() == "fbclient.dll":
                        candidatos.append(os.path.join(base, nome))

    validos = []
    for candidato in candidatos:
        if candidato and os.path.exists(candidato):
            validos.append(candidato)

    if not validos:
        return None

    def _pontuar(caminho: str) -> tuple[int, int, int]:
        lower = caminho.lower()
        score = 0
        if "firebird_5_0" in lower:
            score += 500
        elif "firebird_4_0" in lower:
            score += 400
        elif "firebird_3_0" in lower:
            score += 300
        elif "firebird" in lower:
            score += 100

        # Prefere cliente nativo ao WOW64 quando ambos existem.
        if "wow64" in lower:
            score -= 50

        # Caminho explicitamente fornecido pelo ambiente tem prioridade.
        env_score = 0
        env_path = (os.environ.get("FIREBIRD_CLIENT") or os.environ.get("FBCLIENT") or "").lower()
        if env_path and lower == env_path:
            env_score = 1000

        # Desempate estável por tamanho do caminho.
        return (env_score + score, -len(caminho), 0)

    validos.sort(key=_pontuar, reverse=True)
    return validos[0]


def _preparar_firebird_runtime() -> str | None:
    """Adiciona o diretório do cliente Firebird ao PATH/Windows DLL search path."""
    fb_dll = _buscar_fbclient_dll()
    if not fb_dll:
        return None

    dll_dir = str(Path(fb_dll).parent)
    os.environ["FIREBIRD_CLIENT"] = fb_dll
    os.environ["FBCLIENT"] = fb_dll
    if dll_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")

    if os.name == "nt":
        try:
            os.add_dll_directory(dll_dir)
        except Exception:
            pass
    return fb_dll


def normalize_key(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_ean(value: Any) -> str:
    text = normalize_text(value)
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return digits


def normalize_ncm(value: Any) -> str:
    text = normalize_text(value)
    digits = re.sub(r"\D", "", text)
    return digits[:8]


def normalize_decimal(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    text = str(value).replace(".", "").replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    text = str(value).replace(".", "").replace(",", "")
    text = re.sub(r"[^0-9\-]", "", text)
    try:
        return int(float(text))
    except ValueError:
        return 0


def find_matching_column(columns: Sequence[str], aliases: Sequence[str]) -> str | None:
    normalized_columns = {normalize_key(col): col for col in columns}
    for alias in aliases:
        key = normalize_key(alias)
        if key in normalized_columns:
            return normalized_columns[key]
    for alias in aliases:
        alias_key = normalize_key(alias)
        for col_name in columns:
            col_key = normalize_key(col_name)
            if alias_key in col_key or col_key in alias_key:
                return col_name
    return None


def encontrar_arquivos_csv(root: Path) -> List[Path]:
    if not root.exists():
        return []
    arquivos = []
    for caminho in root.rglob("*.csv"):
        nome = caminho.name.lower()
        if any(token in nome for token in ("estoque", "produto", "produtos", "cadastro", "item")):
            arquivos.append(caminho)
    if arquivos:
        return sorted(arquivos, key=lambda p: str(p).lower())

    # Fallback: se não houver nomes óbvios, tenta todos os CSVs da pasta e subpastas.
    todos_csv = [c for c in root.rglob("*.csv")]
    return sorted(todos_csv, key=lambda p: str(p).lower())


def encontrar_banco_firebird(root: Path) -> Path | None:
    if not root.exists():
        return None
    candidatos = list(root.rglob("*.FDB")) + list(root.rglob("*.fdb"))
    for caminho in candidatos:
        if caminho.name.upper() == "DATAGES.FDB":
            return caminho
    if candidatos:
        return sorted(candidatos, key=lambda p: str(p).lower())[0]
    return None


def carregar_csv_produtos(caminho: Path) -> List[dict[str, Any]]:
    with caminho.open("r", encoding="utf-8-sig", newline="") as f:
        amostra = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(amostra, delimiters=";,\t,")
            delimitador = dialect.delimiter
        except Exception:
            delimitador = ";"
        reader = csv.DictReader(f, delimiter=delimitador)
        linhas = list(reader)

    if not linhas:
        return []

    colunas = [str(col).strip() for col in linhas[0].keys()]

    ean_col = find_matching_column(colunas, ["EAN", "CODIGOBARRAS", "CODBAR", "CODIGO_DE_BARRAS", "BARCODE", "GTIN", "CODIGO"])
    desc_col = find_matching_column(colunas, ["DESCRICAO", "DESCRICAO_PRODUTO", "NOME", "NOME_PRODUTO", "PRODUTO", "DESCRICAOITEM", "ITEM"])
    preco_col = find_matching_column(colunas, ["PRECO_VENDA", "PRECODEVENDA", "VALOR_VENDA", "VENDAPRECO", "PRECO", "PRECOUNITARIO", "PV", "VALORUNITARIO"])
    ncm_col = find_matching_column(colunas, ["NCM", "CODIGONCM", "NCM_PRODUTO"])
    estoque_col = find_matching_column(colunas, ["QUANTIDADE", "ESTOQUE", "SALDO", "QTDE", "QTD", "QTD_ESTOQUE", "DISPONIVEL"])

    if not desc_col:
        raise ValueError(f"Não foi possível identificar a coluna de descrição do produto em: {caminho}")

    produtos: List[dict[str, Any]] = []
    for linha in linhas:
        ean = normalize_ean(linha.get(ean_col, "") if ean_col else "")
        nome = normalize_text(linha.get(desc_col, "") if desc_col else "")
        if not nome:
            continue
        preco = normalize_decimal(linha.get(preco_col, 0.0) if preco_col else 0.0)
        ncm = normalize_ncm(linha.get(ncm_col, "") if ncm_col else "")
        quantidade = normalize_int(linha.get(estoque_col, 0) if estoque_col else 0)

        produtos.append(
            {
                "codigo_barras": ean or f"GDOOR-{len(produtos)+1:06d}",
                "nome": nome,
                "preco_venda": round(preco, 2),
                "ncm": ncm,
                "quantidade_atual": quantidade,
            }
        )

    return produtos


def _conectar_firebird(path: Path):
    fb_dll = _preparar_firebird_runtime()

    for module_name in ("fdb", "firebird", "firebird.driver"):
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue

        try:
            if module_name == "fdb":
                conn_kwargs = {"dsn": str(path), "user": "SYSDBA", "password": "masterkey"}
                if fb_dll:
                    conn_kwargs["fb_library_name"] = fb_dll
                try:
                    return mod.connect(**conn_kwargs)
                except TypeError:
                    if fb_dll:
                        return mod.connect(str(path), "SYSDBA", "masterkey", fb_library_name=fb_dll)
                    return mod.connect(str(path), "SYSDBA", "masterkey")

            if module_name == "firebird":
                if fb_dll:
                    try:
                        return mod.connect(str(path), "SYSDBA", "masterkey", fb_library_name=fb_dll)
                    except TypeError:
                        pass
                try:
                    return mod.connect(str(path), "SYSDBA", "masterkey")
                except TypeError:
                    return mod.connect(database=str(path), user="SYSDBA", password="masterkey")

            if module_name == "firebird.driver":
                if fb_dll:
                    try:
                        return mod.connect(database=str(path), user="SYSDBA", password="masterkey", fb_library_name=fb_dll)
                    except TypeError:
                        pass
                try:
                    return mod.connect(database=str(path), user="SYSDBA", password="masterkey")
                except TypeError:
                    return mod.connect(str(path), "SYSDBA", "masterkey")
        except Exception:
            continue

    raise RuntimeError(
        "Falha ao conectar ao banco Firebird do backup. "
        "O arquivo DATAGES.FDB foi encontrado, mas a biblioteca cliente do Firebird (fbclient.dll) não está disponível neste ambiente. "
        "Instale o cliente Firebird 3.0/4.0/5.0 do Windows e certifique-se de que a DLL esteja em C:\\Program Files\\Firebird\\Firebird_3_0\\bin\\fbclient.dll."
    )


def _listar_tabelas_firebird(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT RDB$RELATION_NAME FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NULL ORDER BY RDB$RELATION_NAME"
    )
    tabelas = []
    for row in cur.fetchall():
        valor = row[0]
        if isinstance(valor, bytes):
            valor = valor.decode("latin1", errors="ignore")
        nome = str(valor).strip()
        if nome:
            tabelas.append(nome)
    return tabelas


def _descobrir_tabela_produtos_firebird(conn) -> str | None:
    tabelas = _listar_tabelas_firebird(conn)
    nomes = {t.upper(): t for t in tabelas}
    candidatos = [
        "PRODUTOS", "PRODUTO", "ESTOQUE", "ITEMS", "ITENS", "P_PRODUTO", "PRODUTO_ESTOQUE",
        "CAD_PRODUTO", "CADASTRO_PRODUTO", "PRODUTO_ESTOQUE", "ESTOQUE_PRODUTOS"
    ]
    for nome in candidatos:
        if nome in nomes:
            return nomes[nome]
    for nome in tabelas:
        upper = nome.upper()
        if "PRODUT" in upper or "ESTOQUE" in upper:
            return nome
    return None


def _buscar_coluna_por_alias(colunas: Sequence[str], aliases: Sequence[str]) -> str | None:
    return find_matching_column(colunas, aliases)


def extrair_produtos_firebird(path: Path) -> List[dict[str, Any]]:
    conn = _conectar_firebird(path)
    try:
        tabela_produto = _descobrir_tabela_produtos_firebird(conn)
        if not tabela_produto:
            raise ValueError(f"Não foi possível identificar uma tabela de produtos no banco Firebird: {path}")

        cur = conn.cursor()
        cur.execute(f'SELECT FIRST 1 * FROM "{tabela_produto}"')
        if not cur.description:
            return []

        colunas = [str(col[0]).strip() for col in cur.description]
        ean_col = _buscar_coluna_por_alias(colunas, ["EAN", "CODIGOBARRAS", "CODBAR", "CODIGO_DE_BARRAS", "BARCODE", "GTIN", "BARRAS", "CODIGO"])
        desc_col = _buscar_coluna_por_alias(colunas, ["DESCRICAO", "DESCRICAO_PRODUTO", "NOME", "NOME_PRODUTO", "PRODUTO", "ITEM"])
        preco_col = _buscar_coluna_por_alias(colunas, ["PRECO_VENDA", "PRECODEVENDA", "VALOR_VENDA", "PRECO", "PRECO_UNITARIO", "PV", "VALORUNITARIO"])
        ncm_col = _buscar_coluna_por_alias(colunas, ["NCM", "CODIGONCM", "NCM_PRODUTO", "COD_NCM"])
        estoque_col = _buscar_coluna_por_alias(colunas, ["QTD_DISPONIVEL", "QUANTIDADE", "ESTOQUE", "SALDO", "QTDE", "QTD", "QTD_ESTOQUE", "DISPONIVEL"])

        if not desc_col:
            raise ValueError(f"Tabela de produtos '{tabela_produto}' sem coluna de descrição reconhecível.")

        colunas_select = []
        for c in (ean_col, desc_col, preco_col, ncm_col, estoque_col):
            if c and c not in colunas_select:
                colunas_select.append(c)

        if colunas_select:
            select_cols_sql = ", ".join(f'"{c}"' for c in colunas_select)
            cur.execute(f'SELECT {select_cols_sql} FROM "{tabela_produto}"')
            colunas_lidas = colunas_select
        else:
            cur.execute(f'SELECT * FROM "{tabela_produto}"')
            colunas_lidas = [str(col[0]).strip() for col in cur.description]

        produtos: List[dict[str, Any]] = []
        while True:
            linhas = cur.fetchmany(5000)
            if not linhas:
                break

            for linha in linhas:
                registro = {colunas_lidas[idx]: linha[idx] for idx in range(len(colunas_lidas))}
                ean = normalize_ean(registro.get(ean_col) if ean_col else "")
                nome = normalize_text(registro.get(desc_col) if desc_col else "")
                if not nome:
                    continue
                preco = normalize_decimal(registro.get(preco_col) if preco_col else 0.0)
                ncm = normalize_ncm(registro.get(ncm_col) if ncm_col else "")
                quantidade = normalize_int(registro.get(estoque_col) if estoque_col else 0)
                produtos.append(
                    {
                        "codigo_barras": ean or f"GDOOR-{len(produtos)+1:06d}",
                        "nome": nome,
                        "preco_venda": round(preco, 2),
                        "ncm": ncm,
                        "quantidade_atual": quantidade,
                    }
                )
        return produtos
    finally:
        conn.close()


def salvar_planilha(produtos: Iterable[dict[str, Any]], output_path: Path) -> None:
    colunas = ["codigo_barras", "nome", "preco_venda", "ncm", "quantidade_atual"]
    unicos = {}
    for p in produtos:
        codigo = str(p.get("codigo_barras", "") or "").strip()
        chave = codigo or str(len(unicos))
        unicos[chave] = {
            "codigo_barras": p.get("codigo_barras", ""),
            "nome": p.get("nome", ""),
            "preco_venda": p.get("preco_venda", 0.0),
            "ncm": p.get("ncm", ""),
            "quantidade_atual": p.get("quantidade_atual", 0),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.suffix.lower() == ".csv":
        with output_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=colunas, delimiter=";")
            writer.writeheader()
            for item in unicos.values():
                writer.writerow(item)
        return

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError(
            "Para gerar relatório .xlsx é necessário openpyxl. Use .csv ou instale: python -m pip install openpyxl"
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    ws.append(colunas)
    for item in unicos.values():
        ws.append([item[c] for c in colunas])
    wb.save(output_path)


def inserir_produtos_frs(produtos: Iterable[dict[str, Any]]) -> int:
    total = 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for produto in produtos:
            codigo = normalize_ean(produto.get("codigo_barras")) or f"GDOOR-{abs(hash(produto.get('nome','')))%100000000:08d}"
            nome = normalize_text(produto.get("nome"))
            preco_venda = round(float(produto.get("preco_venda") or 0.0), 2)
            ncm = normalize_ncm(produto.get("ncm"))
            quantidade = int(produto.get("quantidade_atual") or 0)
            if not nome:
                continue

            cursor.execute(
                "SELECT id FROM produtos WHERE codigo_barras = ? LIMIT 1",
                (codigo,),
            )
            existente = cursor.fetchone()

            if existente:
                cursor.execute(
                    """
                    UPDATE produtos
                    SET nome = ?,
                        ncm = ?,
                        preco_custo = ?,
                        preco_venda = ?,
                        margem_lucro = 0.0,
                        quantidade_atual = ?
                    WHERE id = ?
                    """,
                    (nome, ncm, preco_venda, preco_venda, quantidade, existente[0]),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO produtos (
                        codigo_barras,
                        nome,
                        variacao,
                        ncm,
                        preco_custo,
                        margem_lucro,
                        preco_venda,
                        quantidade_atual,
                        quantidade_minima,
                        validade,
                        imagem_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        codigo,
                        nome,
                        "GDOOR",
                        ncm,
                        preco_venda,
                        0.0,
                        preco_venda,
                        quantidade,
                        0,
                        "",
                        "",
                    ),
                )
                total += 1
    return total


def inserir_produtos_frs_local(produtos: Iterable[dict[str, Any]], output_excel: Path | str | None = None) -> dict[str, Any]:
    """Importa os produtos para o SQLite local do FRS Mercado e, opcionalmente, exporta para Excel."""
    lista = list(produtos)
    if output_excel:
        salvar_planilha(lista, Path(output_excel))

    inseridos = inserir_produtos_frs(lista)
    return {
        "quantidade_produtos": len(lista),
        "produtos_inseridos": inseridos,
        "arquivo_excel": str(Path(output_excel).resolve()) if output_excel else None,
    }


def executar_migracao(root: Path | str = DEFAULT_GDOOR_ROOT, output_excel: Path | str = DEFAULT_OUTPUT_EXCEL) -> dict[str, Any]:
    root_path = Path(root)
    output_path = Path(output_excel)

    banco = encontrar_banco_firebird(root_path)
    if banco:
        print(f"Banco Firebird localizado: {banco}")
        try:
            produtos = extrair_produtos_firebird(banco)
        except Exception as exc:
            raise RuntimeError(
                f"Não foi possível ler o banco Firebird {banco}. Instale o cliente Firebird (fbclient.dll) e tente novamente. Detalhe: {exc}"
            ) from exc
        if not produtos:
            raise ValueError(f"Nenhum produto válido foi encontrado no banco Firebird: {banco}")
        origem = "FIREBIRD"
    else:
        csvs = encontrar_arquivos_csv(root_path)
        if not csvs:
            raise FileNotFoundError(
                f"Nenhum CSV de produto foi encontrado em {root_path} e também não foi localizado o arquivo DATAGES.FDB."
            )
        produtos = []
        for csv_path in csvs:
            try:
                produtos.extend(carregar_csv_produtos(csv_path))
            except Exception as exc:
                print(f"Falha ao processar CSV {csv_path}: {exc}")
        if not produtos:
            raise ValueError(f"Nenhum produto válido foi encontrado nos CSVs de: {root_path}")
        origem = "CSV"

    resultado_local = inserir_produtos_frs_local(produtos, output_excel=output_path)

    return {
        "arquivo_excel": str(output_path.resolve()) if output_path.exists() else str(output_path),
        "quantidade_produtos": resultado_local["quantidade_produtos"],
        "produtos_inseridos": resultado_local["produtos_inseridos"],
        "origem": origem,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Migra cadastro de produtos do GDOOR para o FRS Mercado.")
    parser.add_argument("--root", default=str(DEFAULT_GDOOR_ROOT), help="Pasta do backup GDOOR.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_EXCEL), help="Arquivo Excel de saída.")
    args = parser.parse_args()

    try:
        resultado = executar_migracao(args.root, args.output)
        print("=== MIGRAÇÃO CONCLUÍDA ===")
        print(f"Arquivo Excel: {resultado['arquivo_excel']}")
        print(f"Produtos lidos: {resultado['quantidade_produtos']}")
        print(f"Produtos inseridos: {resultado['produtos_inseridos']}")
        print(f"Origem: {resultado['origem']}")
    except Exception as exc:
        print(f"Erro na migração: {exc}")
        raise
