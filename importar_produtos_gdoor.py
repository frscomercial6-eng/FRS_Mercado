import os
import sys
from datetime import datetime

from migrar_produtos_gdoor import executar_migracao

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_ROOT = r"F:\\"
DEFAULT_OUTPUT_DIR = os.path.abspath(os.path.join(BASE_DIR, "exportacao_fiscal"))


def gerar_nome_planilha_importacao() -> str:
    agora = datetime.now()
    return f"produtos_importados_{agora.strftime('%d%m%Y_%H%M%S')}.xlsx"


def importar_produtos(root_path: str = DEFAULT_ROOT, output_excel: str | None = None):
    if output_excel is None:
        os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
        output_excel = os.path.join(DEFAULT_OUTPUT_DIR, gerar_nome_planilha_importacao())

    return executar_migracao(root=root_path, output_excel=output_excel)


if __name__ == "__main__":
    root = DEFAULT_ROOT
    output = None

    if len(sys.argv) > 1:
        root = os.path.abspath(sys.argv[1])
    if len(sys.argv) > 2:
        output = os.path.abspath(sys.argv[2])

    try:
        resultado = importar_produtos(root_path=root, output_excel=output)
        print("=== IMPORTACAO CONCLUIDA ===")
        print(f"Origem: {resultado.get('origem', 'desconhecida')}")
        print(f"Produtos lidos: {resultado.get('quantidade_produtos', 0)}")
        print(f"Produtos inseridos: {resultado.get('produtos_inseridos', 0)}")
        print(f"Planilha: {resultado.get('arquivo_excel', '')}")
    except Exception as exc:
        print(f"Erro ao importar produtos: {exc}", file=sys.stderr)
        sys.exit(1)
