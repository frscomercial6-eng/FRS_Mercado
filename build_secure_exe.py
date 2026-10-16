import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from _build_support.build_paths import load_build_paths
from release_manager import prepare_release_artifacts, read_version


PATHS = load_build_paths()
ROOT_DIR = PATHS.root
OBFUSCATED_DIR = PATHS.obf_dir
BUILD_DIR = PATHS.build_dir
DIST_DIR = PATHS.dist_dir
INSTALLER_DIR = PATHS.installer_dir
APP_NAME = "FRS_Mercado"
SKIP_CLEAN = PATHS.skip_clean
SKIP_VERSION_SYNC = PATHS.skip_version_sync


def reload_paths() -> None:
    """Recarrega PATHS e constantes derivadas a partir do ambiente atual."""
    global PATHS, ROOT_DIR, OBFUSCATED_DIR, BUILD_DIR, DIST_DIR
    global INSTALLER_DIR, SKIP_CLEAN, SKIP_VERSION_SYNC
    PATHS = load_build_paths()
    ROOT_DIR = PATHS.root
    OBFUSCATED_DIR = PATHS.obf_dir
    BUILD_DIR = PATHS.build_dir
    DIST_DIR = PATHS.dist_dir
    INSTALLER_DIR = PATHS.installer_dir
    SKIP_CLEAN = PATHS.skip_clean
    SKIP_VERSION_SYNC = PATHS.skip_version_sync


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("\n>>", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, cwd=str(cwd or ROOT_DIR), check=True)


def _find_tool(tool_name: str) -> Path:
    scripts_dir = Path(sys.executable).resolve().parent / "Scripts"
    candidate = scripts_dir / f"{tool_name}.exe"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Ferramenta não encontrada: {candidate}")


def _clean_dirs(skip_clean: bool = False) -> None:
    # 1.0.23 — FRS_SKIP_CLEAN=1 (global SKIP_CLEAN) bloqueia TODA limpeza.
    if skip_clean or SKIP_CLEAN:
        print("[ISOLAMENTO] Limpeza desabilitada (--skip-clean / FRS_SKIP_CLEAN=1).")
        return
    for path in [OBFUSCATED_DIR, BUILD_DIR, DIST_DIR]:
        if PATHS.is_protected(path):
            raise RuntimeError(f"Caminho protegido detectado na limpeza segura: {path}")
        if path.exists():
            shutil.rmtree(path)


def _list_app_sources() -> list[str]:
    excluded = {
        "build_exe.py",
        "build_secure_exe.py",
        "build_flet_windows_bundle.py",
        "deploy.py",
        "release_master.py",
        "run_tests.py",
        "smoke_test_fiscal.py",
        "smoke_test_webhook_token.py",
        "test_update_flow.py",
    }
    sources: list[str] = []
    for path in sorted(ROOT_DIR.glob("*.py")):
        if path.name in excluded:
            continue
        sources.append(path.name)
    if "main.py" not in sources:
        raise FileNotFoundError("main.py não encontrado para build seguro")
    return sources


def _obfuscate_sources(pyarmor_exe: Path, sources: list[str]) -> None:
    # 1.0.23 — fail-closed: PyArmor escreve OBFUSCATED_DIR; valida antes.
    if PATHS.is_protected(OBFUSCATED_DIR):
        raise RuntimeError(f"Caminho protegido detectado na ofuscação: {OBFUSCATED_DIR}")
    cmd = [
        str(pyarmor_exe),
        "gen",
        "-O",
        str(OBFUSCATED_DIR),
        "-r",
        "-i",
        "--obf-module",
        "1",
        "--obf-code",
        "2",
    ] + sources
    _run(cmd)


def _build_executable(pyinstaller_exe: Path) -> Path:
    # 1.0.23 — fail-closed: PyInstaller escreve BUILD_DIR/DIST_DIR; valida antes.
    for label, p in (("build_dir", BUILD_DIR), ("dist_dir", DIST_DIR)):
        if PATHS.is_protected(p):
            raise RuntimeError(f"Caminho protegido detectado no executável seguro ({label}): {p}")
    obf_main = OBFUSCATED_DIR / "main.py"
    if not obf_main.exists():
        raise FileNotFoundError(f"Entrypoint obfuscado não encontrado: {obf_main}")

    args = [
        str(pyinstaller_exe),
        str(obf_main),
        "--noconfirm",
        # 1.0.23 — --clean é limpeza de build: só quando SKIP_CLEAN=0.
        *([] if SKIP_CLEAN else ["--clean"]),
        "--onedir",
        "--windowed",
        "--disable-windowed-traceback",
        f"--name={APP_NAME}",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={BUILD_DIR}",
        f"--icon={ROOT_DIR / 'assets' / 'logo.ico'}",
        f"--add-data={ROOT_DIR / 'assets'};assets",
        "--collect-submodules=encodings",
        "--hidden-import=hashlib",
        "--hidden-import=uuid",
        "--hidden-import=encodings",
        "--hidden-import=codecs",
        "--hidden-import=importlib",
        "--hidden-import=importlib.util",
        "--hidden-import=pkgutil",
        "--hidden-import=zipimport",
        "--hidden-import=site",
        "--hidden-import=sysconfig",
        "--collect-all=customtkinter",
        "--collect-all=PIL",
        "--collect-all=reportlab",
        "--collect-all=googleapiclient",
        "--collect-all=google_auth_oauthlib",
        "--collect-all=google.auth",
        "--collect-all=httplib2",
        "--collect-all=requests",
        "--collect-all=bcrypt",
        "--collect-all=setuptools",
    ]

    obf_runtime_hook = OBFUSCATED_DIR / "_runtime_hook_error_logger.py"
    if obf_runtime_hook.exists():
        args.append(f"--runtime-hook={obf_runtime_hook}")

    config_dir = ROOT_DIR / "config"
    if config_dir.exists() and config_dir.is_dir():
        args.append(f"--add-data={config_dir};config")

    _run(args)
    app_dir = DIST_DIR / APP_NAME
    if not app_dir.exists():
        raise FileNotFoundError(f"Saída esperada não encontrada: {app_dir}")
    return app_dir


def _assert_no_py_sources(app_dir: Path) -> None:
    leaked_sources = [p for p in app_dir.rglob("*.py")]
    if leaked_sources:
        print("[ALERTA] Arquivos .py encontrados no build final:")
        for item in leaked_sources[:20]:
            print("-", item)
        raise RuntimeError("Build seguro inválido: há código-fonte .py exposto no pacote final")


def _create_portable_zip(app_version: str, app_dir: Path) -> Path:
    # 1.0.23 — fail-closed: valida destino ANTES de qualquer mkdir/unlink/Zip.
    for label, p in (("installer_dir", INSTALLER_DIR), ("iss_temp", PATHS.iss_temp)):
        if PATHS.is_protected(p):
            raise RuntimeError(f"Caminho protegido detectado no ZIP seguro ({label}): {p}")
    if PATHS.homol_root is not None:
        try:
            PATHS.iss_temp.resolve().relative_to(PATHS.homol_root.resolve())
        except ValueError:
            raise RuntimeError(
                f"_iss_temp fora da raiz de homologação: {PATHS.iss_temp} "
                f"(esperado dentro de {PATHS.homol_root})"
            )
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = INSTALLER_DIR / f"FRS_Mercado_SecurePortable_{app_version}.zip"
    # 1.0.23 — unlink do ZIP anterior é limpeza de build: respeita SKIP_CLEAN.
    if zip_path.exists():
        if SKIP_CLEAN:
            print(f"[ISOLAMENTO] FRS_SKIP_CLEAN=1: preservando ZIP anterior: {zip_path}")
            return zip_path
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in app_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(app_dir))

    return zip_path


def _find_iscc() -> Path | None:
    candidates = [
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
        Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _build_setup_if_available() -> Path | None:
    # 1.0.23 — fail-closed: valida _iss_temp + destinos ANTES de mkdir/write.
    for label, p in (
        ("iss_temp", PATHS.iss_temp),
        ("installer_dir", INSTALLER_DIR),
        ("dist_dir", DIST_DIR),
    ):
        if PATHS.is_protected(p):
            raise RuntimeError(f"Caminho protegido detectado no setup seguro ({label}): {p}")
    if PATHS.homol_root is not None:
        try:
            PATHS.iss_temp.resolve().relative_to(PATHS.homol_root.resolve())
        except ValueError:
            raise RuntimeError(
                f"_iss_temp fora da raiz de homologação: {PATHS.iss_temp} "
                f"(esperado dentro de {PATHS.homol_root})"
            )
    iscc = _find_iscc()
    if iscc is None:
        print("[AVISO] Inno Setup não encontrado. Setup .exe não será gerado neste ambiente.")
        return None

    iss_template = ROOT_DIR / "setup_frs.iss"
    if not iss_template.exists():
        raise FileNotFoundError("Arquivo setup_frs.iss não encontrado.")

    # Gera .iss temporário com caminhos isolados (usa PATHS.iss_temp canônico).
    iss_temp = PATHS.iss_temp
    iss_temp.parent.mkdir(parents=True, exist_ok=True)
    template_content = iss_template.read_text(encoding="utf-8")
    iss_content = template_content.replace(
        'OutputDir=installer',
        f'OutputDir={INSTALLER_DIR}'
    ).replace(
        'OutputBaseFilename=FRS_Mercado_Setup',
        f'OutputBaseFilename=FRS_Mercado_Setup_Secure'
    ).replace(
        'Source: "dist\\FRS_Mercado\\*";',
        f'Source: "{DIST_DIR}\\FRS_Mercado\\*";'
    )
    iss_temp.write_text(iss_content, encoding="utf-8")

    cmd = [str(iscc), str(iss_temp)]
    print(f"Executando Inno Setup (iss temporário seguro isolado): {' '.join(cmd)}")
    _run(cmd)

    setup_path = INSTALLER_DIR / "FRS_Mercado_Setup_Secure.exe"
    return setup_path if setup_path.exists() else None


def main() -> None:
    # Bloqueia prepare_release_artifacts() durante a etapa de isolamento.
    if SKIP_VERSION_SYNC:
        app_version = read_version()
        print("[ISOLAMENTO] prepare_release_artifacts() bloqueado por FRS_SKIP_VERSION_SYNC=1.")
    else:
        app_version = prepare_release_artifacts()

    pyarmor_exe = _find_tool("pyarmor")
    pyinstaller_exe = _find_tool("pyinstaller")

    print(f"Python em uso: {sys.executable}")
    print(f"Versão alvo: {app_version}")
    print(f"Caminho de ofuscação (isolado): {OBFUSCATED_DIR}")
    print(f"Caminho de build (isolado): {BUILD_DIR}")
    print(f"Caminho de dist (isolado): {DIST_DIR}")
    print(f"Caminho do instalador (isolado): {INSTALLER_DIR}")
    print(f"Limpeza desabilitada: {SKIP_CLEAN}")

    _clean_dirs(skip_clean=SKIP_CLEAN)
    sources = _list_app_sources()
    print(f"Total de arquivos Python do app para ofuscar: {len(sources)}")

    _obfuscate_sources(pyarmor_exe, sources)
    app_dir = _build_executable(pyinstaller_exe)
    _assert_no_py_sources(app_dir)

    portable_zip = _create_portable_zip(app_version, app_dir)
    setup_path = _build_setup_if_available()

    print("\nBuild seguro concluído com sucesso.")
    print(f"Executável seguro (onedir): {app_dir / (APP_NAME + '.exe')}")
    print(f"Pacote portátil seguro: {portable_zip}")
    if setup_path:
        print(f"Setup seguro: {setup_path}")


if __name__ == "__main__":
    main()