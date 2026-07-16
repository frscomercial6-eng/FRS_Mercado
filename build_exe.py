import os
import importlib.util
import sysconfig
import stat
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import PyInstaller.__main__
from release_manager import prepare_release_artifacts


ROOT_DIR = Path(__file__).resolve().parent
RUNTIME_HOOK_PATH = ROOT_DIR / "_runtime_hook_error_logger.py"
SUPPORT_DIR = ROOT_DIR / "_build_support"
APP_EXE_NAME = "FRS_Mercado.exe"
WINDOWS_VERSION_INFO_PATH = ROOT_DIR / "_build_support" / "version_info.txt"


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} ").strip().lower()
    return ans in {"s", "sim", "y", "yes"}


def _read_current_file(path: Path) -> str:
    """Read file content directly from disk to avoid stale cache assumptions."""
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def _validate_security_files() -> None:
    """Hard-stop build if mandatory security fixes are not present in current sources."""
    modulo_login = ROOT_DIR / "modulo_login.py"
    database_file = ROOT_DIR / "database.py"

    if not modulo_login.exists():
        raise FileNotFoundError(f"Arquivo de segurança ausente: {modulo_login}")
    if not database_file.exists():
        raise FileNotFoundError(f"Arquivo de segurança ausente: {database_file}")

    login_src = _read_current_file(modulo_login)
    db_src = _read_current_file(database_file)

    required_login_markers = [
        "hash_parte = partes_codigo[3]",
        "expected_hash[:16] == hash_parte[:16]",
    ]
    missing_login = [m for m in required_login_markers if m not in login_src]
    if missing_login:
        raise RuntimeError(
            "Falha na verificação de segurança em modulo_login.py. "
            f"Trechos ausentes: {missing_login}"
        )

    required_db_markers = [
        "assinatura TEXT",
        "ALTER TABLE licenca ADD COLUMN assinatura TEXT",
    ]
    missing_db = [m for m in required_db_markers if m not in db_src]
    if missing_db:
        raise RuntimeError(
            "Falha na verificação de segurança em database.py. "
            f"Trechos ausentes: {missing_db}"
        )


def _resolve_entrypoint() -> str:
    main_py = ROOT_DIR / "main.py"
    if not main_py.exists():
        raise FileNotFoundError(
            "main.py não encontrado. Ajuste o entrypoint antes do build para manter o padrão solicitado."
        )
    return "main.py"


def _ensure_runtime_hook() -> Path:
    """Cria runtime hook para registrar falhas de import/execucao no executavel."""
    hook_code = '''import datetime
import pathlib
import sys
import traceback

from error_notifier import notify_error
from app_paths import obter_caminho_log


def _log_runtime_error(exc_type, exc_value, exc_tb):
    try:
        log_file = pathlib.Path(obter_caminho_log("FRS_Mercado_runtime_error.log"))
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stack = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))

        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] Falha nao tratada no executavel\\n")
            f.write(f"Tipo: {getattr(exc_type, '__name__', str(exc_type))}\\n")
            f.write(f"Mensagem: {exc_value}\\n")
            if isinstance(exc_value, ModuleNotFoundError):
                f.write(f"Modulo ausente: {getattr(exc_value, 'name', 'desconhecido')}\\n")
            f.write("Traceback:\\n")
            f.write(stack)
            f.write("\\n" + ("-" * 80) + "\\n")

        notify_error("runtime_hook", exc_value, stack)
    except Exception:
        pass


sys.excepthook = _log_runtime_error
'''
    RUNTIME_HOOK_PATH.write_text(hook_code, encoding="utf-8")
    return RUNTIME_HOOK_PATH


def _ensure_windows_version_file(app_version: str) -> Path:
    parts = app_version.split(".")
    while len(parts) < 4:
        parts.append("0")
    version_tuple = ", ".join(parts[:4])

    version_file_content = f'''# UTF-8
VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=({version_tuple}),
        prodvers=({version_tuple}),
        mask=0x3f,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0)
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    '040904B0',
                    [
                        StringStruct('CompanyName', 'FRS Solutions'),
                        StringStruct('FileDescription', 'FRS Mercado'),
                        StringStruct('FileVersion', '{app_version}'),
                        StringStruct('InternalName', 'FRS_Mercado'),
                        StringStruct('OriginalFilename', 'FRS_Mercado.exe'),
                        StringStruct('ProductName', 'FRS Mercado'),
                        StringStruct('ProductVersion', '{app_version}')
                    ]
                )
            ]
        ),
        VarFileInfo([VarStruct('Translation', [1046, 1200])])
    ]
)
'''
    WINDOWS_VERSION_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    WINDOWS_VERSION_INFO_PATH.write_text(version_file_content, encoding="utf-8")
    return WINDOWS_VERSION_INFO_PATH


def _resolve_customtkinter_assets_dir() -> Path | None:
    """Resolve a pasta de assets do CustomTkinter para inclusão explícita no build."""
    spec = importlib.util.find_spec("customtkinter")
    if spec is None:
        return None

    origin = getattr(spec, "origin", None)
    if not origin:
        return None

    package_dir = Path(origin).resolve().parent
    assets_dir = package_dir / "assets"
    if assets_dir.exists() and assets_dir.is_dir():
        return assets_dir
    return None


def _build_pyinstaller_args(app_version: str) -> list[str]:
    entrypoint = _resolve_entrypoint()
    hook_path = _ensure_runtime_hook()
    version_file = _ensure_windows_version_file(app_version)
    icon_file = ROOT_DIR / "assets" / "logo.ico"
    if not icon_file.exists():
        raise FileNotFoundError(
            "assets/logo.ico é obrigatório para customizar o executável e não foi encontrado."
        )

    args = [
        entrypoint,
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--disable-windowed-traceback",
        "--name=FRS_Mercado",
        "--icon=assets/logo.ico",
        f"--version-file={version_file}",
        "--add-data=assets;assets",
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
        "--collect-submodules=encodings",
        f"--runtime-hook={hook_path}",
    ]

    # Inclui explicitamente temas do CustomTkinter para evitar falhas em _MEI temporário.
    customtk_assets_dir = _resolve_customtkinter_assets_dir()
    if customtk_assets_dir is not None:
        args.append(f"--add-data={customtk_assets_dir};customtkinter/assets")
        print(f"- CustomTkinter themes incluídos: {customtk_assets_dir} -> customtkinter/assets")
    else:
        print("[AVISO] Pasta de assets do CustomTkinter não encontrada para inclusão explícita.")

    # Reforça resolução de stdlib/site-packages em ambientes sem Python instalado.
    std_paths = {
        str(Path(sysconfig.get_path("stdlib") or "").resolve()),
        str(Path(sysconfig.get_path("platstdlib") or "").resolve()),
        str(Path(sysconfig.get_path("purelib") or "").resolve()),
        str(Path(sysconfig.get_path("platlib") or "").resolve()),
        str((Path(sys.executable).resolve().parent / "DLLs").resolve()),
    }
    for std_path in sorted(p for p in std_paths if p and Path(p).exists()):
        args.append(f"--paths={std_path}")

    collect_modules = [
        "customtkinter",
        "PIL",
        "reportlab",
        "googleapiclient",
        "google_auth_oauthlib",
        "google.auth",
        "httplib2",
        "requests",
        "bcrypt",
        "setuptools",
    ]
    for mod_name in collect_modules:
        if importlib.util.find_spec(mod_name) is not None:
            args.append(f"--collect-all={mod_name}")

    optional_hidden = [
        "altgraph",
        "macholib",
        "pywintypes",
        "pythoncom",
        "win32api",
        "win32com",
        "win32con",
        "win32gui",
    ]
    for mod_name in optional_hidden:
        if importlib.util.find_spec(mod_name) is not None:
            args.append(f"--hidden-import={mod_name}")

    config_dir = ROOT_DIR / "config"
    if config_dir.exists() and config_dir.is_dir():
        args.append("--add-data=config;config")

    return args


def _clean_previous_builds() -> None:
    """Remove artefatos antigos para evitar empacotamento sujo."""
    def _on_rm_error(func, path, _exc_info):
        # Alguns artefatos do Flutter/Flet ficam read-only no Windows.
        os.chmod(path, stat.S_IWRITE)
        func(path)

    for folder_name in ["build", "dist"]:
        target = ROOT_DIR / folder_name
        if target.exists() and target.is_dir():
            shutil.rmtree(target, onerror=_on_rm_error)
            print(f"Pasta removida: {target}")


def _copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _create_portable_package(app_version: str) -> Path:
    """Monta uma versão portátil e gera ZIP em installer/."""
    dist_exe = ROOT_DIR / "dist" / APP_EXE_NAME
    if not dist_exe.exists():
        raise FileNotFoundError(f"Executável não encontrado para pacote portátil: {dist_exe}")

    portable_dir = ROOT_DIR / "portable_build"
    if portable_dir.exists():
        shutil.rmtree(portable_dir)
    portable_dir.mkdir(parents=True, exist_ok=True)

    _copy_if_exists(dist_exe, portable_dir / APP_EXE_NAME)
    _copy_if_exists(ROOT_DIR / "assets", portable_dir / "assets")
    _copy_if_exists(ROOT_DIR / "version.txt", portable_dir / "version.txt")
    _copy_if_exists(ROOT_DIR / "EULA.txt", portable_dir / "EULA.txt")
    _copy_if_exists(SUPPORT_DIR, portable_dir)

    installer_dir = ROOT_DIR / "installer"
    installer_dir.mkdir(parents=True, exist_ok=True)
    zip_path = installer_dir / f"FRS_Mercado_Portable_{app_version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in portable_dir.rglob("*"):
            if path.is_file():
                arcname = path.relative_to(portable_dir)
                zf.write(path, arcname)

    return zip_path


def _prepare_support_payload() -> None:
    """Prepara payload de suporte para instalador/portátil."""
    if SUPPORT_DIR.exists() and SUPPORT_DIR.is_dir():
        shutil.rmtree(SUPPORT_DIR)
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Credenciais e documentos de apoio
    _copy_if_exists(ROOT_DIR / "credentials.json", SUPPORT_DIR / "credentials.json")
    _copy_if_exists(ROOT_DIR / "google-services.json", SUPPORT_DIR / "google-services.json")
    _copy_if_exists(ROOT_DIR / "checklist_homologacao.md", SUPPORT_DIR / "checklist_homologacao.md")

    # ACBrMonitor (motor fiscal) para instalador all-in-one.
    acbr_dir = SUPPORT_DIR / "acbr"
    acbr_dir.mkdir(parents=True, exist_ok=True)
    acbr_candidates = [
        ROOT_DIR / "instala" / "ACBrMonitorPLUS-DEMO-1.4.0.467-x86-I.exe",
        ROOT_DIR / "instala" / "ACBrMonitor.exe",
        ROOT_DIR / "instala" / "ACBrMonitorPLUS.exe",
    ]
    acbr_found = None
    for acbr in acbr_candidates:
        if acbr.exists() and acbr.is_file():
            acbr_found = acbr
            break
    if acbr_found is not None:
        _copy_if_exists(acbr_found, acbr_dir / "ACBrMonitor_Installer.exe")
        print(f"- ACBr incluído no payload: {acbr_found} -> {acbr_dir / 'ACBrMonitor_Installer.exe'}")
    else:
        print("[AVISO] Instalador do ACBr não encontrado para inclusão no setup all-in-one.")

    # Banco principal vai para a pasta data (mesmo layout esperado em runtime).
    data_dir = SUPPORT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    db_candidates: list[Path] = []
    try:
        from database_manager import get_db_path

        db_candidates.append(Path(get_db_path()))
    except Exception:
        pass

    db_candidates.extend(
        [
            ROOT_DIR / "mercado.db",
            ROOT_DIR / "database.db",
            ROOT_DIR / "banco.db",
        ]
    )

    db_found = None
    for db_path in db_candidates:
        if db_path.exists() and db_path.is_file():
            db_found = db_path
            break

    if db_found is not None:
        _copy_if_exists(db_found, data_dir / "mercado.db")
        print(f"- Banco incluído no payload: {db_found} -> {data_dir / 'mercado.db'}")
    else:
        print("[AVISO] Nenhum arquivo de banco encontrado para inclusão automática no pacote.")


def _find_iscc() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if str(candidate) and candidate.exists():
            return candidate
    return None


def _build_installer(app_version: str) -> Path | None:
    """Compila setup_frs.iss se o Inno Setup estiver instalado."""
    iscc = _find_iscc()
    if iscc is None:
        print("[AVISO] Inno Setup não encontrado. Instalador não foi gerado automaticamente.")
        return None

    script = ROOT_DIR / "setup_frs.iss"
    if not script.exists():
        raise FileNotFoundError("Arquivo setup_frs.iss não encontrado.")

    cmd = [str(iscc), str(script)]
    print(f"Executando Inno Setup: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)

    return ROOT_DIR / "installer" / "FRS_Mercado_Setup.exe"


def main() -> None:
    app_version = prepare_release_artifacts()
    _validate_security_files()

    print(f"Python em uso no build: {sys.executable}")
    print(f"Versão Python: {sys.version}")

    assets_dir = ROOT_DIR / "assets"
    if not assets_dir.exists() or not assets_dir.is_dir():
        raise FileNotFoundError("A pasta assets é obrigatória para o build e não foi encontrada.")

    _clean_previous_builds()
    _prepare_support_payload()
    args = _build_pyinstaller_args(app_version)

    print("Arquivos/recursos que serão empacotados:")
    print("- Entrypoint: main.py")
    print("- Ícone: assets/logo.ico")
    print("- Pasta assets -> assets")
    print("- Runtime hook de log -> FRS_Mercado_runtime_error.log em dist/")
    print("- Coleta completa (quando instalado): customtkinter, PIL, reportlab, googleapiclient, google_auth_oauthlib, google.auth, httplib2, requests, bcrypt")
    print("- Payload suporte (_build_support): credentials.json, google-services.json, checklist_homologacao.md, data/mercado.db, acbr/ACBrMonitor_Installer.exe, mobile/mercado.apk")
    if (ROOT_DIR / "config").exists():
        print("- config/ -> config/")

    print("\nComando interno do PyInstaller:")
    for item in args:
        print(f"  {item}")

    PyInstaller.__main__.run(args)

    zip_path = _create_portable_package(app_version)
    print(f"Pacote portátil gerado: {zip_path}")

    installer_path = _build_installer(app_version)
    if installer_path:
        print(f"Instalador gerado: {installer_path}")

    pergunta_deploy = "Build concluído com sucesso. Deseja realizar o deploy para o GitHub agora? [S/N]"
    if not _confirm(pergunta_deploy):
        print("Deploy cancelado. Artefatos mantidos localmente para revisão")
        return

    subprocess.run(
        [sys.executable, "deploy.py", "--skip-build", "--yes"],
        cwd=str(ROOT_DIR),
        check=True,
    )


if __name__ == "__main__":
    main()
