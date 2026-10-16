"""Gera apenas o pacote portátil do FRS Mercado (ZIP) em diretório isolado.

Orquestra unicamente o fluxo necessário ao pacote portátil, reaproveitando a
lógica do build_exe.py: validação de segurança, limpeza de builds anteriores,
build PyInstaller (onedir) e montagem do pacote portátil em diretório de
homologação + ZIP em diretório de instalador isolado.

Não compila o instalador Inno Setup e não realiza deploy. Também não interage
no terminal (sem prompts), com exceção da etapa de limpeza que pode ser
pulada com --skip-clean.

Durante esta etapa de isolamento, prepare_release_artifacts() é bloqueado
por FRS_SKIP_VERSION_SYNC=1.

Uso:
    python build_portable.py                # build completo + pacote portátil
    python build_portable.py --skip-clean   # reaproveita build/dist existentes
"""

import argparse
import sys
from pathlib import Path

try:  # PyInstaller é opcional para os testes de isolamento.
    import PyInstaller.__main__  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - venv sem PyInstaller
    import types as _types
    import sys as _sys

    _pyi_pkg = _types.ModuleType("PyInstaller")
    _pyi_main = _types.ModuleType("PyInstaller.__main__")

    def _missing_pyinstaller_run(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("PyInstaller não instalado; compilação real bloqueada.")

    _pyi_main.run = _missing_pyinstaller_run  # type: ignore[attr-defined]
    _pyi_pkg.__main__ = _pyi_main  # type: ignore[attr-defined]
    _sys.modules.setdefault("PyInstaller", _pyi_pkg)
    _sys.modules.setdefault("PyInstaller.__main__", _pyi_main)
    PyInstaller = _pyi_pkg  # type: ignore[no-redef]

import build_exe as exe
from _build_support.build_paths import load_build_paths


PATHS = load_build_paths()


def reload_paths() -> None:
    """Recarrega PATHS a partir do ambiente atual (usado pelos testes)."""
    global PATHS
    PATHS = load_build_paths()


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

    # 1.0.23 — fail-closed: valida _iss_temp + destinos ANTES de mkdir/write.
    for label, p in (
        ("portable_dir", exe.PORTABLE_DIR),
        ("installer_dir", exe.INSTALLER_DIR),
        ("iss_temp", exe.PATHS.iss_temp),
    ):
        if exe.PATHS.is_protected(p):
            raise RuntimeError(f"Caminho protegido detectado no build portátil ({label}): {p}")
    if exe.PATHS.homol_root is not None:
        try:
            exe.PATHS.iss_temp.resolve().relative_to(exe.PATHS.homol_root.resolve())
        except ValueError:
            raise RuntimeError(
                f"_iss_temp fora da raiz de homologação: {exe.PATHS.iss_temp} "
                f"(esperado dentro de {exe.PATHS.homol_root})"
            )
    print("=" * 70)
    print("FRS Mercado - Build do Pacote Portatil (ZIP) [isolado]")
    print("=" * 70)
    print(f"Python em uso no build: {sys.executable}")
    print(f"Versao Python: {sys.version}")
    print(f"Caminho portátil (isolado): {exe.PORTABLE_DIR}")
    print(f"Caminho do instalador (isolado): {exe.INSTALLER_DIR}")
    print(f"Limpeza desabilitada: {exe.SKIP_CLEAN or cli_args.skip_clean}")

    # Bloqueia prepare_release_artifacts() durante a etapa de isolamento.
    if exe.SKIP_VERSION_SYNC:
        app_version = exe.read_version()
        print("[ISOLAMENTO] prepare_release_artifacts() bloqueado por FRS_SKIP_VERSION_SYNC=1.")
    else:
        app_version = exe.prepare_release_artifacts()

    exe._validate_security_files()

    assets_dir = exe.ROOT_DIR / "assets"
    if not assets_dir.exists() or not assets_dir.is_dir():
        raise FileNotFoundError("A pasta assets é obrigatória para o build e não foi encontrada.")

    skip_clean_effective = exe.SKIP_CLEAN or cli_args.skip_clean
    if cli_args.skip_clean:
        print("[AVISO] --skip-clean ativo: build/dist anteriores serão reaproveitados.")
    exe._clean_previous_builds(skip_clean=skip_clean_effective)

    exe._prepare_support_payload(skip_clean=skip_clean_effective)

    args = exe._build_pyinstaller_args(app_version)
    print("\nComando interno do PyInstaller:")
    for item in args:
        print(f"  {item}")
    PyInstaller.__main__.run(args)

    zip_path = exe._create_portable_package(app_version, skip_clean=skip_clean_effective)

    print("\n" + "=" * 70)
    print("Build do pacote portátil concluído com sucesso.")
    print(f"Pasta portátil : {exe.PORTABLE_DIR}")
    print(f"Pacote ZIP     : {zip_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()