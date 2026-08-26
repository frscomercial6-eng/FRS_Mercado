import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from release_manager import prepare_release_artifacts


ROOT_DIR = Path(__file__).resolve().parent
OBFUSCATED_DIR = ROOT_DIR / "_secure_obf"
BUILD_DIR = ROOT_DIR / "build_secure"
DIST_DIR = ROOT_DIR / "dist_secure"
INSTALLER_DIR = ROOT_DIR / "installer"
APP_NAME = "FRS_Mercado"


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("\n>>", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, cwd=str(cwd or ROOT_DIR), check=True)


def _find_tool(tool_name: str) -> Path:
    scripts_dir = Path(sys.executable).resolve().parent / "Scripts"
    candidate = scripts_dir / f"{tool_name}.exe"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Ferramenta não encontrada: {candidate}")


def _clean_dirs() -> None:
    for path in [OBFUSCATED_DIR, BUILD_DIR, DIST_DIR]:
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
    obf_main = OBFUSCATED_DIR / "main.py"
    if not obf_main.exists():
        raise FileNotFoundError(f"Entrypoint obfuscado não encontrado: {obf_main}")

    args = [
        str(pyinstaller_exe),
        str(obf_main),
        "--noconfirm",
        "--clean",
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
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = INSTALLER_DIR / f"FRS_Mercado_SecurePortable_{app_version}.zip"
    if zip_path.exists():
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
    iscc = _find_iscc()
    if iscc is None:
        print("[AVISO] Inno Setup não encontrado. Setup .exe não será gerado neste ambiente.")
        return None

    dist_default = ROOT_DIR / "dist"
    backup_dist = ROOT_DIR / "dist_backup_before_secure_setup"

    if backup_dist.exists():
        shutil.rmtree(backup_dist)

    if dist_default.exists():
        dist_default.rename(backup_dist)

    try:
        shutil.copytree(DIST_DIR, dist_default)
        _run([str(iscc), str(ROOT_DIR / "setup_frs.iss")])
    finally:
        if dist_default.exists():
            shutil.rmtree(dist_default)
        if backup_dist.exists():
            backup_dist.rename(dist_default)

    setup_path = INSTALLER_DIR / "FRS_Mercado_Setup.exe"
    return setup_path if setup_path.exists() else None


def main() -> None:
    app_version = prepare_release_artifacts()
    pyarmor_exe = _find_tool("pyarmor")
    pyinstaller_exe = _find_tool("pyinstaller")

    print(f"Python em uso: {sys.executable}")
    print(f"Versão alvo: {app_version}")

    _clean_dirs()
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