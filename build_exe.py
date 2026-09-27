
import argparse
import importlib.util
import json
import os
import sqlite3
import sysconfig
import stat
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from subprocess import CalledProcessError

import PyInstaller.__main__
from release_manager import prepare_release_artifacts


ROOT_DIR = Path(__file__).resolve().parent
RUNTIME_HOOK_PATH = ROOT_DIR / "_runtime_hook_error_logger.py"
SUPPORT_DIR = ROOT_DIR / "_build_support"
APP_EXE_NAME = "FRS_Mercado.exe"
APP_DIST_DIR = ROOT_DIR / "dist" / "FRS_Mercado"
# O Portable público recebe uma base de distribuição criada pelo schema oficial,
# sempre sem dados operacionais ou cadastro comercial de clientes.
PUBLIC_DELIVERY_DB_PATH = ROOT_DIR / "_build_staging" / "mercado_1.0.19_distribuicao_limpa.db"
PUBLIC_ASSET_FILES = ("logo.ico", "frsMercado.ico", "frsMercado.jpeg")
FORBIDDEN_PUBLIC_NAMES = {
    "credentials.json",
    "google-services.json",
    "firebase-admin-key.json",
    "client_credentials.sec.json",
    "logo_mercado_mario.jpeg",
}
WINDOWS_VERSION_INFO_PATH = ROOT_DIR / "_build_support" / "version_info.txt"
SECURE_OBFUSCATED_DIR = ROOT_DIR / "_secure_obf"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Desktop FRS Mercado")
    parser.add_argument(
        "--secure-obfuscation",
        action="store_true",
        help="Ativa ofuscacao com PyArmor antes do PyInstaller.",
    )
    parser.add_argument(
        "--nuitka",
        action="store_true",
        help="Compila o executável final com Nuitka usando a configuração oficial do projeto.",
    )
    parser.add_argument(
        "--skip-deploy",
        action="store_true",
        help="Nao pergunta deploy ao final do build.",
    )
    return parser.parse_args()


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} ").strip().lower()
    return ans in {"s", "sim", "y", "yes"}


def _read_current_file(path: Path) -> str:
    """Read file content directly from disk to avoid stale cache assumptions."""
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def _validate_security_files() -> None:
    """Hard-stop quando a infraestrutura segura obrigatória estiver ausente."""
    required_files = {
        "login local": ROOT_DIR / "modulo_login.py",
        "licença assinada": ROOT_DIR / "license_manager.py",
        "verificador": ROOT_DIR / "licensing" / "license_verifier.py",
        "identidade de máquina": ROOT_DIR / "licensing" / "machine_identity.py",
        "atualizador seguro": ROOT_DIR / "updater_secure.py",
        "helper externo": ROOT_DIR / "updater_helper.py",
        "chaves públicas de licença": ROOT_DIR / "licensing" / "trusted_keys.json",
        "chaves públicas do updater": ROOT_DIR / "updater_public_keys.json",
    }
    missing_files = [str(path) for path in required_files.values() if not path.is_file()]
    if missing_files:
        raise RuntimeError(f"Infraestrutura de segurança ausente: {missing_files}")

    login_src = _read_current_file(required_files["login local"])
    license_src = _read_current_file(required_files["licença assinada"])
    updater_src = _read_current_file(required_files["atualizador seguro"])
    helper_src = _read_current_file(required_files["helper externo"])
    required_login_markers = (
        "Autenticação é estritamente local",
        "self.start_silent_check" if "self.start_silent_check" in login_src else "O login nunca consulta o",
        "SELECT id, nome, permissao FROM usuarios",
    )
    missing_login = [marker for marker in required_login_markers if marker not in login_src]
    if missing_login:
        raise RuntimeError(f"Login local não passou na validação de segurança: {missing_login}")

    for source, label, markers in (
        (license_src, "licença", ("LicenseService", "invalid_signed_license", "create_challenge", "activate")),
        (updater_src, "updater", ("FRS-MERCADO-UPDATE-MANIFEST-V1", "Ed25519PublicKey", ".part", "WAITING_FOR_EXIT", "ROLLING_BACK")),
        (helper_src, "helper", ("--update-health-check", "PROTECTED_TOP_LEVEL", "_restore_protected_data", "_exclusive_lock")),
    ):
        absent = [marker for marker in markers if marker not in source]
        if absent:
            raise RuntimeError(f"Segurança de {label} incompleta: {absent}")

    try:
        license_keys = json.loads(required_files["chaves públicas de licença"].read_text(encoding="utf-8")).get("keys", {})
        updater_keys = json.loads(required_files["chaves públicas do updater"].read_text(encoding="utf-8")).get("keys", {})
    except Exception as exc:
        raise RuntimeError("Arquivos de chaves públicas estão ausentes ou inválidos.") from exc
    if not isinstance(license_keys, dict) or not license_keys:
        raise RuntimeError("Configure ao menos uma chave pública oficial de licença antes do build.")
    if not isinstance(updater_keys, dict) or not updater_keys:
        raise RuntimeError("Configure ao menos uma chave pública oficial do updater antes do build.")

    private_markers = ("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "BEGIN EC PRIVATE KEY")
    client_files = list(required_files.values()) + [ROOT_DIR / "gerador_licenca.py"]
    for path in client_files:
        if path.suffix not in {".py", ".json"}:
            continue
        source = _read_current_file(path)
        if any(marker in source for marker in private_markers):
            raise RuntimeError(f"Material de chave privada não pode estar no cliente: {path.name}")


def _resolve_entrypoint(base_dir: Path = ROOT_DIR) -> str:
    main_py = base_dir / "main.py"
    if not main_py.exists():
        raise FileNotFoundError(
            "main.py não encontrado. Ajuste o entrypoint antes do build para manter o padrão solicitado."
        )
    return str(main_py)


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


def _build_pyinstaller_args(
    app_version: str,
    entrypoint: str | None = None,
    runtime_hook: Path | None = None,
    extra_paths: list[Path] | None = None,
) -> list[str]:
    entrypoint = entrypoint or _resolve_entrypoint()
    hook_path = runtime_hook or _ensure_runtime_hook()
    version_file = _ensure_windows_version_file(app_version)
    icon_file = ROOT_DIR / "assets" / "logo.ico"
    if not icon_file.exists():
        raise FileNotFoundError(
            "assets/logo.ico é obrigatório para customizar o executável e não foi encontrado."
        )

    args = [
        entrypoint,
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--disable-windowed-traceback",
        "--name=FRS_Mercado",
        f"--icon={ROOT_DIR / 'assets' / 'logo.ico'}",
        f"--version-file={version_file}",
        f"--add-data={ROOT_DIR / 'assets'};assets",
        f"--add-data={ROOT_DIR / 'version.txt'};.",
        f"--add-data={ROOT_DIR / 'EULA.txt'};.",
        f"--add-data={ROOT_DIR / 'updater_public_keys.json'};.",
        f"--add-data={ROOT_DIR / 'licensing' / 'trusted_keys.json'};licensing",
        "--collect-submodules=licensing",
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

    if extra_paths:
        for path in extra_paths:
            if path.exists() and path.is_dir():
                args.append(f"--paths={path}")

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
        "cryptography",
        "openpyxl",
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


def _build_updater_helper(app_version: str) -> Path:
    """Compila o helper externo; a aplicação nunca se substitui sozinha."""
    helper_entry = ROOT_DIR / "updater_helper.py"
    if not helper_entry.is_file():
        raise FileNotFoundError(f"Helper de atualização ausente: {helper_entry}")
    helper_dist = ROOT_DIR / "dist" / "UpdateHelper"
    helper_work = ROOT_DIR / "build" / "UpdateHelper"
    helper_spec = ROOT_DIR / "build" / "UpdateHelperSpec"
    version_file = _ensure_windows_version_file(app_version)
    cmd = [
        str(helper_entry), "--noconfirm", "--onefile", "--console",
        "--name=FRS_Mercado_UpdateHelper",
        f"--distpath={helper_dist}",
        f"--workpath={helper_work}",
        f"--specpath={helper_spec}",
        f"--paths={ROOT_DIR}",
        f"--add-data={ROOT_DIR / 'updater_public_keys.json'};.",
        f"--version-file={version_file}",
        "--collect-submodules=licensing",
        "--collect-all=cryptography",
    ]
    print("[NUITKA/HELPER] Compilando helper seguro de atualização...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", *cmd],
        cwd=str(ROOT_DIR), check=True,
    )
    produced = helper_dist / "FRS_Mercado_UpdateHelper.exe"
    if not produced.is_file():
        raise FileNotFoundError(f"Helper de atualização não foi gerado: {produced}")
    APP_DIST_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(produced, APP_DIST_DIR / produced.name)
    return APP_DIST_DIR / produced.name


def _terminate_stale_app_processes() -> None:
    """Fecha qualquer instância anterior do app para liberar arquivos em dist/."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "FRS_Mercado.exe"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _clean_previous_builds() -> None:
    """Remove artefatos antigos para evitar empacotamento sujo."""
    _terminate_stale_app_processes()

    def _on_rm_error(func, path, _exc_info):
        # Alguns artefatos do Flutter/Flet ficam read-only no Windows.
        try:
            os.chmod(path, stat.S_IWRITE)
        except Exception:
            pass
        try:
            func(path)
        except FileNotFoundError:
            pass

    for folder_name in ["build", "dist", "_secure_obf"]:
        target = ROOT_DIR / folder_name
        if target.exists() and target.is_dir():
            for _ in range(3):
                try:
                    shutil.rmtree(target, onerror=_on_rm_error)
                    print(f"Pasta removida: {target}")
                    break
                except PermissionError:
                    _terminate_stale_app_processes()
                    continue
            else:
                raise PermissionError(f"Não foi possível remover o diretório em uso: {target}")


def _find_pyarmor_cli() -> Path:
    scripts_dir = Path(sys.executable).resolve().parent / "Scripts"
    candidate = scripts_dir / "pyarmor.exe"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"PyArmor nao encontrado em {candidate}. Instale com: {sys.executable} -m pip install pyarmor"
    )


def _list_sources_for_obfuscation() -> list[str]:
    excluded = {
        "build_exe.py",
        "build_portable.py",
        "build_secure_exe.py",
        "build_flet_windows_bundle.py",
        "deploy.py",
        "release_master.py",
        "run_tests.py",
        "smoke_test_fiscal.py",
        "smoke_test_webhook_token.py",
        "test_update_flow.py",
    }
    sources = [p.name for p in sorted(ROOT_DIR.glob("*.py")) if p.name not in excluded]
    if "main.py" not in sources:
        raise FileNotFoundError("main.py nao encontrado na lista de fontes para ofuscacao")
    return sources


def _obfuscate_sources_with_pyarmor() -> tuple[str, Path, Path | None]:
    pyarmor = _find_pyarmor_cli()
    sources = _list_sources_for_obfuscation()

    cmd = [
        str(pyarmor),
        "gen",
        "-O",
        str(SECURE_OBFUSCATED_DIR),
        "-r",
        "-i",
        "--obf-module",
        "1",
        "--obf-code",
        "1",
    ] + sources

    print("Executando ofuscacao com PyArmor...")
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)

    obf_entrypoint = SECURE_OBFUSCATED_DIR / "main.py"
    if not obf_entrypoint.exists():
        raise FileNotFoundError(f"Entrypoint ofuscado não encontrado: {obf_entrypoint}")

    obf_runtime_hook = SECURE_OBFUSCATED_DIR / "_runtime_hook_error_logger.py"
    return str(obf_entrypoint), SECURE_OBFUSCATED_DIR, (obf_runtime_hook if obf_runtime_hook.exists() else None)


def _build_with_nuitka_secure_fallback() -> None:
    print("[NUITKA] Compilação final oficial com Nuitka.")
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--enable-plugin=tk-inter",
        "--windows-icon-from-ico=assets/logo.ico",
        "--company-name=FRS Solutions",
        "--product-name=FRS Mercado",
        "--file-version=1.0.20",
        "--product-version=1.0.20",
        "--include-data-files=assets/logo.ico=assets/logo.ico",
        "--include-data-files=assets/frsMercado.ico=assets/frsMercado.ico",
        "--include-data-files=assets/frsMercado.jpeg=assets/frsMercado.jpeg",
        "--include-data-file=version.txt=version.txt",
        "--include-data-file=EULA.txt=EULA.txt",
        "--include-data-file=updater_public_keys.json=updater_public_keys.json",
        "--include-data-file=licensing/trusted_keys.json=licensing/trusted_keys.json",
        "--include-package=licensing",
        "--output-dir=dist",
        "--output-filename=FRS_Mercado.exe",
        "main.py",
    ]

    config_dir = ROOT_DIR / "config"
    if config_dir.exists() and config_dir.is_dir():
        cmd.append("--include-data-dir=config=config")

    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)

    produced_dir = ROOT_DIR / "dist" / "main.dist"
    target_dir = ROOT_DIR / "dist" / "FRS_Mercado"
    if not produced_dir.exists() or not produced_dir.is_dir():
        raise FileNotFoundError("Saída esperada do Nuitka não encontrada em dist/main.dist")

    if target_dir.exists():
        shutil.rmtree(target_dir)
    produced_dir.rename(target_dir)


def _copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _es_nome_instalador_acbr(nome: str) -> bool:
    """True se o nome corresponde a um instalador (-I/DEMO/installer),
    nunca ao binario real do motor fiscal (ACBrMonitor.exe/ACBrMonitorPLUS.exe)."""
    nome_low = str(nome or "").lower()
    return (
        "installer" in nome_low
        or "instalador" in nome_low
        or "setup" in nome_low
        or "demo" in nome_low
        or nome_low.endswith("-i.exe")
    )


def _montar_instala_acbr_portatil(portable_dir: Path) -> None:
    """Prepara <portatil>/instala/ para o runtime fiscal.

    1) Com o motor REAL disponível (_build_support/acbr/ACBrMonitor.exe), copia
       o binário para instala/ACBrMonitor.exe (motor pronto, sem instalador).
    2) Sem o motor real, copia o instalador oficial DEMO
       (_build_support/acbr/ACBrMonitor_Installer.exe) para instala/, para que
       o runtime (modulo_fiscal/system_monitor) possa instalá-lo no primeiro
       uso com os mesmos parâmetros do setup Inno (/VERYSILENT /NORESTART).
    O instalador (-I/DEMO) jamais é usado como motor fiscal."""
    monitor_src = SUPPORT_DIR / "acbr" / "ACBrMonitor.exe"
    if monitor_src.exists() and monitor_src.is_file():
        instala_portatil = portable_dir / "instala"
        instala_portatil.mkdir(parents=True, exist_ok=True)
        shutil.copy2(monitor_src, instala_portatil / "ACBrMonitor.exe")
        print(
            f"- instala/ACBrMonitor.exe do portátil gerado com o motor real: {monitor_src}"
        )
        return

    instalador_src = SUPPORT_DIR / "acbr" / "ACBrMonitor_Installer.exe"
    if instalador_src.exists() and instalador_src.is_file():
        instala_portatil = portable_dir / "instala"
        instala_portatil.mkdir(parents=True, exist_ok=True)
        shutil.copy2(instalador_src, instala_portatil / "ACBrMonitor_Installer.exe")
        print(
            "- instala/ACBrMonitor_Installer.exe do portátil gerado com o "
            f"instalador DEMO oficial (instala no primeiro uso): {instalador_src}"
        )
        return

    print(
        "[AVISO] Nem o motor REAL nem o instalador DEMO disponíveis em "
        "_build_support/acbr/. O portátil ficará sem motor fiscal até que o "
        "instalador oficial seja incluído no payload de suporte antes do build."
    )


def _create_portable_package(app_version: str) -> Path:
    """Monta uma versão portátil e gera ZIP em installer/."""
    if not APP_DIST_DIR.exists() or not APP_DIST_DIR.is_dir():
        raise FileNotFoundError(f"Saída onedir não encontrada para pacote portátil: {APP_DIST_DIR}")

    portable_dir = ROOT_DIR / "portable_build"
    if portable_dir.exists():
        shutil.rmtree(portable_dir)
    portable_dir.mkdir(parents=True, exist_ok=True)

    for path in APP_DIST_DIR.rglob("*"):
        if path.is_file():
            rel_path = path.relative_to(APP_DIST_DIR)
            destino = portable_dir / rel_path
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destino)

    for asset_name in PUBLIC_ASSET_FILES:
        _copy_if_exists(ROOT_DIR / "assets" / asset_name, portable_dir / "assets" / asset_name)
    helper_path = APP_DIST_DIR / "FRS_Mercado_UpdateHelper.exe"
    if not helper_path.is_file():
        raise FileNotFoundError("Helper seguro de atualização ausente antes do empacotamento.")
    _copy_if_exists(ROOT_DIR / "version.txt", portable_dir / "version.txt")
    _copy_if_exists(ROOT_DIR / "EULA.txt", portable_dir / "EULA.txt")
    _copy_if_exists(SUPPORT_DIR, portable_dir)
    _copy_if_exists(ROOT_DIR / "version.txt", portable_dir / "version.txt")
    _copy_if_exists(ROOT_DIR / "EULA.txt", portable_dir / "EULA.txt")
    _copy_if_exists(SUPPORT_DIR, portable_dir)
    # Base pública limpa; nunca usa o banco operacional do desenvolvedor.
    _seed_delivery_db(portable_dir)
    _montar_instala_acbr_portatil(portable_dir)

    installer_dir = ROOT_DIR / "installer"
    installer_dir.mkdir(parents=True, exist_ok=True)
    zip_path = installer_dir / f"FRS_Mercado_Portable_{app_version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in portable_dir.rglob("*"):
            if path.is_file():
                # Nunca embarcar arquivos journal/WAL/SHM no ZIP de entrega.
                if path.suffix.lower() in {".wal", ".shm"} or path.name.endswith("-wal") or path.name.endswith("-shm") or path.name.endswith("-journal"):
                    continue
                # Allowlist defensiva de segurança para o artefato público.
                if path.name.lower() in FORBIDDEN_PUBLIC_NAMES:
                    continue
                arcname = path.relative_to(portable_dir)
                zf.write(path, arcname)

    _validate_public_portable(zip_path)
    return zip_path


def _validate_public_portable(zip_path: Path) -> None:
    """Falha se o Portable público contiver segredos, dados privados ou sidecars."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [name.replace("\\", "/") for name in zf.namelist()]
        forbidden = [
            name for name in names
            if Path(name).name.lower() in FORBIDDEN_PUBLIC_NAMES
            or "logo_mercado_mario" in name.lower()
            or "_mario" in name.lower()
            or name.lower().endswith(("-wal", "-shm", "-journal", ".tmp", "~"))
        ]
        if forbidden:
            raise RuntimeError(f"Portable público contém artefatos privados/temporários: {forbidden[:10]}")
        if sum(1 for name in names if Path(name).name.lower() == "frs_mercado_updatehelper.exe") != 1:
            raise RuntimeError("Portable público deve conter FRS_Mercado_UpdateHelper.exe")
        if sum(1 for name in names if Path(name).name.lower() == "updater_public_keys.json") != 1:
            raise RuntimeError("Portable público deve conter updater_public_keys.json")
        if not any(name.lower().endswith("licensing/trusted_keys.json") for name in names):
            raise RuntimeError("Portable público deve conter licensing/trusted_keys.json")
        db_names = [name for name in names if name.lower() == "data/mercado.db"]
        if len(db_names) != 1:
            raise RuntimeError(f"Portable público deve conter exatamente um data/mercado.db: {db_names}")
        db_bytes = zf.read(db_names[0])
        with tempfile.TemporaryDirectory(prefix="frs_validate_public_") as temp_dir:
            db_path = Path(temp_dir) / "mercado.db"
            db_path.write_bytes(db_bytes)
            conn = sqlite3.connect(str(db_path))
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Banco público inválido.")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("Banco público com foreign keys inválidas.")
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in (
                    "produtos", "vendas", "vendas_dia", "itens_venda", "clientes", "vales",
                    "vale_itens", "orcamentos", "orcamento_itens", "financeiro",
                    "caixa_operacao", "caixa_conferencia", "sangrias", "logs_auditoria",
                    "logs_mentoria", "entradas", "usuarios", "licenca", "fornecedores",
                    "fornecedor_produtos", "produto_lotes",
                ):
                    if table in tables and conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != 0:
                        raise RuntimeError(f"Banco público contém dados em {table}.")
            finally:
                conn.close()


def _create_public_distribution_db() -> Path:
    """Cria base pública limpa usando database.init_db em APPDATA temporário."""
    PUBLIC_DELIVERY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(PUBLIC_DELIVERY_DB_PATH) + suffix)
        if candidate.exists():
            candidate.unlink()

    with tempfile.TemporaryDirectory(prefix="frs_public_db_") as temp_dir:
        env = os.environ.copy()
        env["APPDATA"] = temp_dir
        code = (
            "import database; database.init_db(); "
            "from database_manager import get_db_connection; "
            "exec('with get_db_connection() as conn:\\n    conn.execute(\"SELECT 1\")')"
        )
        subprocess.run([sys.executable, "-c", code], cwd=str(ROOT_DIR), env=env, check=True)
        source_db = Path(temp_dir) / "FRS_Mercado" / "data" / "mercado.db"
        if not source_db.exists():
            raise FileNotFoundError(f"Base pública criada sem arquivo principal: {source_db}")
        shutil.copy2(source_db, PUBLIC_DELIVERY_DB_PATH)

    conn = sqlite3.connect(str(PUBLIC_DELIVERY_DB_PATH))
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Base pública de distribuição passou em integrity_check com falha.")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Base pública de distribuição contém violação de foreign key.")
        required_zero = (
            "produtos", "vendas", "vendas_dia", "itens_venda", "clientes", "vales",
            "vale_itens", "orcamentos", "orcamento_itens", "financeiro",
            "caixa_operacao", "caixa_conferencia", "sangrias", "logs_auditoria",
            "logs_mentoria", "entradas", "usuarios", "licenca", "fornecedores",
            "fornecedor_produtos", "produto_lotes",
        )
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in required_zero:
            if table in tables and conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != 0:
                raise RuntimeError(f"Base pública contém dados em {table}.")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(PUBLIC_DELIVERY_DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    print(f"- Base pública limpa criada: {PUBLIC_DELIVERY_DB_PATH}")
    return PUBLIC_DELIVERY_DB_PATH


def _seed_delivery_db(portable_dir: Path) -> None:
    """Cria e copia apenas a base pública limpa para o Portable."""
    source_db = _create_public_distribution_db()
    data_dir = portable_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    destino = data_dir / "mercado.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(destino) + suffix)
        if candidate.exists():
            candidate.unlink()
    shutil.copy2(source_db, destino)


def _prepare_support_payload() -> None:
    """Prepara payload de suporte para instalador/portátil."""
    if SUPPORT_DIR.exists() and SUPPORT_DIR.is_dir():
        shutil.rmtree(SUPPORT_DIR)
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Credenciais e documentos de apoio
    _copy_if_exists(ROOT_DIR / "checklist_homologacao.md", SUPPORT_DIR / "checklist_homologacao.md")

    # ACBrMonitor: el binario REAL del motor y el instalador se empaquetan por
    # separado. El instalador (-I/DEMO) nunca debe usarse ni renombrarse como motor.
    acbr_dir = SUPPORT_DIR / "acbr"
    acbr_dir.mkdir(parents=True, exist_ok=True)
    instala_dir = ROOT_DIR / "instala"

    # 1) Binario real del motor fiscal -> nombre canonico ACBrMonitor.exe.
    monitor_real = None
    for nombre in ("ACBrMonitor.exe", "ACBrMonitorPLUS.exe"):
        candidato = instala_dir / nombre
        if candidato.exists() and candidato.is_file():
            monitor_real = candidato
            break
    if monitor_real is None and instala_dir.is_dir():
        for arq in instala_dir.glob("*ACBrMonitor*.exe"):
            if _es_nome_instalador_acbr(arq.name):
                # Saltar instaladores/demo: no son el motor.
                continue
            monitor_real = arq
            break

    if monitor_real is not None:
        _copy_if_exists(monitor_real, acbr_dir / "ACBrMonitor.exe")
        print(f"- Motor fiscal REAL incluido: {monitor_real.name} -> {acbr_dir / 'ACBrMonitor.exe'}")
    else:
        print(
            "[AVISO] Binario REAL del motor fiscal no encontrado en instala/. "
            "Coloca instala/ACBrMonitor.exe (extraído) antes del build; el payload "
            "contendrá solo el instalador."
        )

    # 2) Instalador oficial DEMO (task all-in-one do Inno + runtime portátil).
    # Jamás se usa como motor. Nome oficial atual primeiro; se a versão mudar,
    # cai para os mesmos globs do runtime (localizar_instalador_acbr_empacotado).
    acbr_instalador = instala_dir / "ACBrMonitorPLUS-DEMO-1.4.0.467-x86-I.exe"
    if not (acbr_instalador.exists() and acbr_instalador.is_file()):
        acbr_instalador = None
        if instala_dir.is_dir():
            for padrao in (
                "ACBrMonitorPLUS-DEMO-*-I.exe",
                "ACBrMonitorPLUS*DEMO*.exe",
                "*ACBrMonitor*Installer*.exe",
            ):
                try:
                    for arq in sorted(instala_dir.glob(padrao)):
                        if arq.is_file():
                            acbr_instalador = arq
                            break
                except Exception:
                    continue
                if acbr_instalador is not None:
                    break
    if acbr_instalador is not None and acbr_instalador.exists() and acbr_instalador.is_file():
        # Fonte única da verdade: instala/ guarda o binário oficial. O payload
        # de suporte (_build_support/acbr) é apenas a cópia de STAGING para o
        # instalador/portátil — nunca uma segunda fonte, para evitar divergência.
        _copy_if_exists(acbr_instalador, acbr_dir / "ACBrMonitor_Installer.exe")
        print(f"- Instalador ACBr (task Inno + runtime portátil): {acbr_instalador.name} -> {acbr_dir / 'ACBrMonitor_Installer.exe'}")
    else:
        # Failsafe: sem o instalador o cliente NÃO consegue emitir NF-e, e o
        # task 'instalaracbr' do Inno fica sem payload. Falha explícita impede
        # publicar um instalador público quebrado silenciosamente.
        print("[ERRO] Instalador do ACBr (ACBrMonitorPLUS-DEMO-*-I.exe) não encontrado em "
              "instala/. O instalador/portátil será gerado SEM o motor fiscal.")
        print("       Para gerar um release público completo, restaure o binário oficial em "
              "instala/ antes de rodar o build.")

    # Banco público nunca é copiado do APPDATA local.
    print("- Banco local não incluído; o Portable usa base pública limpa criada pelo schema oficial.")


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
    cli_args = _parse_args()
    app_version = prepare_release_artifacts()
    _validate_security_files()

    print(f"Python em uso no build: {sys.executable}")
    print(f"Versão Python: {sys.version}")

    assets_dir = ROOT_DIR / "assets"
    if not assets_dir.exists() or not assets_dir.is_dir():
        raise FileNotFoundError("A pasta assets é obrigatória para o build e não foi encontrada.")

    _clean_previous_builds()
    _prepare_support_payload()
    if cli_args.nuitka:
        _build_with_nuitka_secure_fallback()
    elif cli_args.secure_obfuscation:
        try:
            entrypoint, obf_path, obf_hook = _obfuscate_sources_with_pyarmor()
            args = _build_pyinstaller_args(
                app_version,
                entrypoint=entrypoint,
                runtime_hook=obf_hook,
                extra_paths=[obf_path],
            )
            print("\nComando interno do PyInstaller:")
            for item in args:
                print(f"  {item}")
            PyInstaller.__main__.run(args)
        except Exception as exc:
            print(f"[AVISO] PyArmor falhou: {exc}")
            _build_with_nuitka_secure_fallback()
    else:
        args = _build_pyinstaller_args(app_version)
        print("\nComando interno do PyInstaller:")
        for item in args:
            print(f"  {item}")
        PyInstaller.__main__.run(args)

    helper_path = _build_updater_helper(app_version)
    print(f"Helper seguro de atualização gerado: {helper_path}")

    print("Arquivos/recursos que serão empacotados:")
    print("- Entrypoint: main.py")
    print("- Saída PyInstaller: dist/FRS_Mercado/")
    print("- Executável principal: dist/FRS_Mercado/FRS_Mercado.exe")
    print("- Ícone: assets/logo.ico")
    print("- Pasta assets -> assets")
    print("- Runtime hook de log -> FRS_Mercado_runtime_error.log em dist/")
    print("- Coleta completa (quando instalado): customtkinter, PIL, reportlab, googleapiclient, google_auth_oauthlib, google.auth, httplib2, requests, bcrypt")
    print("- Payload suporte (_build_support): checklist_homologacao.md, acbr/ACBrMonitor_Installer.exe; credenciais nunca incluídas")
    if (ROOT_DIR / "config").exists():
        print("- config/ -> config/")

    zip_path = _create_portable_package(app_version)
    print(f"Pacote portátil gerado: {zip_path}")

    installer_path = _build_installer(app_version)
    if installer_path:
        print(f"Instalador gerado: {installer_path}")

    if cli_args.skip_deploy:
        print("Build concluido com --skip-deploy. Artefatos mantidos localmente para revisão")
        return

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
