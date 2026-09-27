"""Gera apenas o pacote portátil do FRS Mercado (ZIP).

Orquestra unicamente o fluxo necessário ao pacote portátil, reaproveitando a
lógica do build_exe.py: validação de segurança, limpeza de builds anteriores,
build PyInstaller (onedir) e montagem do pacote portátil em portable_build/ +
ZIP em installer/.

Não compila o instalador Inno Setup e não realiza deploy. Também não interage
no terminal (sem prompts), com exceção da etapa de limpeza que pode ser
pulada com --skip-clean.

Uso:
    python build_portable.py                # build completo + pacote portátil
    python build_portable.py --skip-clean   # reaproveita build/dist existentes
"""

import argparse
import sys
from pathlib import Path

import PyInstaller.__main__

import build_exe as exe


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build do pacote portátil FRS Mercado (ZIP).",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Nao limpa build/dist anteriores antes de empacotar.",
    )
    return parser.parse_args()


def main() -> None:
    cli_args = _parse_args()

    print("=" * 70)
    print("FRS Mercado - Build do Pacote Portatil (ZIP)")
    print("=" * 70)
    print(f"Python em uso no build: {sys.executable}")
    print(f"Versao Python: {sys.version}")

    app_version = exe.prepare_release_artifacts()
    exe._validate_security_files()

    assets_dir = exe.ROOT_DIR / "assets"
    if not assets_dir.exists() or not assets_dir.is_dir():
        raise FileNotFoundError("A pasta assets é obrigatória para o build e não foi encontrada.")

    if cli_args.skip_clean:
        print("[AVISO] --skip-clean ativo: build/dist anteriores serão reaproveitados.")
    else:
        exe._clean_previous_builds()

    exe._prepare_support_payload()

    args = exe._build_pyinstaller_args(app_version)
    print("\nComando interno do PyInstaller:")
    for item in args:
        print(f"  {item}")
    PyInstaller.__main__.run(args)

    zip_path = exe._create_portable_package(app_version)

    print("\n" + "=" * 70)
    print("Build do pacote portátil concluído com sucesso.")
    print(f"Pasta portátil : {exe.ROOT_DIR / 'portable_build'}")
    print(f"Pacote ZIP     : {zip_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()