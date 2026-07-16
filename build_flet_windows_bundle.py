import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path

from release_manager import read_version


ROOT_DIR = Path(__file__).resolve().parent
MOBILE_DIR = ROOT_DIR / "mobile_app"
STAGE_DIR = ROOT_DIR / "_flet_windows_stage"
INSTALLER_DIR = ROOT_DIR / "installer"
INNO_SCRIPT = ROOT_DIR / "setup_flet_windows_bundle.iss"


def _find_iscc() -> Path | None:
    candidates = [
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
        Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _run_flet_build_windows() -> None:
    cmd = ["flet", "build", "windows"]
    subprocess.run(cmd, cwd=str(MOBILE_DIR), check=True)


def _locate_flet_release_dir() -> Path:
    windows_dir = MOBILE_DIR / "build" / "windows"
    if not windows_dir.exists():
        raise FileNotFoundError("Pasta mobile_app/build/windows não encontrada. Execute o build Flet primeiro.")

    direct_release = list(windows_dir.rglob("runner/Release"))
    for path in direct_release:
        if any(path.glob("*.exe")):
            return path

    exe_candidates = [p.parent for p in windows_dir.rglob("*.exe") if "flutter" not in str(p).lower()]
    if exe_candidates:
        return max(exe_candidates, key=lambda p: len(list(p.glob("*"))))

    raise FileNotFoundError("Não foi possível localizar os binários do Flet em mobile_app/build/windows.")


def _prepare_stage(app_release_dir: Path, acbr_installer: Path) -> Path:
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)

    app_stage = STAGE_DIR / "app"
    acbr_stage = STAGE_DIR / "acbr"
    app_stage.mkdir(parents=True, exist_ok=True)
    acbr_stage.mkdir(parents=True, exist_ok=True)

    shutil.copytree(app_release_dir, app_stage, dirs_exist_ok=True)
    shutil.copy2(acbr_installer, acbr_stage / acbr_installer.name)
    return app_stage


def _build_portable_zip(version: str, app_stage_dir: Path) -> Path:
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = INSTALLER_DIR / f"FRS_Mercado_FletPortable_{version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in app_stage_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(app_stage_dir))

    return zip_path


def _compile_inno_bundle(version: str, acbr_installer_name: str) -> Path:
    iscc = _find_iscc()
    if iscc is None:
        raise FileNotFoundError("ISCC.exe não encontrado. Instale Inno Setup 6.")

    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DAppSourceDir={str((STAGE_DIR / 'app').resolve())}",
        f"/DACBrInstallerName={acbr_installer_name}",
        str(INNO_SCRIPT),
    ]
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)

    return INSTALLER_DIR / f"FRS_Mercado_FletBundle_{version}.exe"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Flet Windows + bundle único com ACBrMonitor")
    parser.add_argument(
        "--acbr-installer",
        required=True,
        help="Caminho para o instalador do ACBrMonitor (.exe)",
    )
    parser.add_argument(
        "--skip-flet-build",
        action="store_true",
        help="Não executa 'flet build windows'; usa build já existente em mobile_app/build/windows.",
    )
    args = parser.parse_args()

    acbr_installer = Path(args.acbr_installer).expanduser().resolve()
    if not acbr_installer.exists() or acbr_installer.suffix.lower() != ".exe":
        raise FileNotFoundError(f"Instalador ACBr inválido: {acbr_installer}")

    version = read_version()

    if not args.skip_flet_build:
        _run_flet_build_windows()

    app_release_dir = _locate_flet_release_dir()
    app_stage = _prepare_stage(app_release_dir, acbr_installer)
    portable_zip = _build_portable_zip(version, app_stage)
    bundle_installer = _compile_inno_bundle(version, acbr_installer.name)

    print(f"Build Flet Windows preparado: {app_release_dir}")
    print(f"ZIP portátil gerado: {portable_zip}")
    print(f"Instalador único gerado: {bundle_installer}")


if __name__ == "__main__":
    main()
