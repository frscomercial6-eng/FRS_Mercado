import argparse
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from _build_support.build_paths import load_build_paths
from release_manager import read_version


ROOT_DIR = Path(__file__).resolve().parent
MOBILE_DIR = ROOT_DIR / "mobile_app"
INNO_SCRIPT = ROOT_DIR / "setup_flet_windows_bundle.iss"


def _paths():
    # 1.0.23 — fail-closed: sem FRS_HOMELOG_ROOT válido, BLOQUEIA em vez de
    # devolver caminhos da raiz original (evita escrita silenciosa fora do
    # isolamento). Chamadores validam com _require_not_protected antes de mkdir.
    p = load_build_paths()  # levanta RuntimeError se protegido/ausente
    if p.homol_root is None:
        raise RuntimeError(
            "Isolamento de homologação ausente para bundle Flet. "
            "Defina FRS_HOMELOG_ROOT."
        )
    return p.homol_root / "_flet_windows_stage", p.installer_dir


def _require_not_protected(path: Path, label: str) -> None:
    try:
        p = load_build_paths()
        if p.is_protected(path):
            raise RuntimeError(f"Caminho protegido detectado no bundle Flet ({label}): {path}")
        if p.homol_root is not None:
            try:
                path.resolve().relative_to(p.homol_root.resolve())
            except ValueError:
                raise RuntimeError(
                    f"{label} fora da raiz de homologação: {path} "
                    f"(esperado dentro de {p.homol_root})"
                )
    except RuntimeError:
        raise
    except Exception:
        # Sem homologação configurada: bloqueia escrita na raiz original.
        raise RuntimeError(
            f"Isolamento de homologação ausente para {label} ({path}). "
            "Defina FRS_HOMELOG_ROOT."
        )


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
    STAGE_DIR, _INSTALLER = _paths()
    # 1.0.23 — fail-closed ANTES de qualquer rmtree/mkdir/copy.
    _require_not_protected(STAGE_DIR, "stage_dir")
    if STAGE_DIR.exists():
        if os.getenv("FRS_SKIP_CLEAN", "0").strip().lower() in {"1", "true", "yes", "sim"}:
            print("[ISOLAMENTO] FRS_SKIP_CLEAN=1: preservando stage Flet anterior (sem rmtree).")
        else:
            shutil.rmtree(STAGE_DIR)

    app_stage = STAGE_DIR / "app"
    acbr_stage = STAGE_DIR / "acbr"
    app_stage.mkdir(parents=True, exist_ok=True)
    acbr_stage.mkdir(parents=True, exist_ok=True)

    shutil.copytree(app_release_dir, app_stage, dirs_exist_ok=True)
    shutil.copy2(acbr_installer, acbr_stage / acbr_installer.name)
    return app_stage


def _build_portable_zip(version: str, app_stage_dir: Path) -> Path:
    _, INSTALLER_DIR = _paths()
    _require_not_protected(INSTALLER_DIR, "installer_dir")
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = INSTALLER_DIR / f"FRS_Mercado_FletPortable_{version}.zip"
    # 1.0.23 — ZipFile("w") trunca ZIP anterior: com SKIP_CLEAN=1 preserva.
    if zip_path.exists() and os.getenv("FRS_SKIP_CLEAN", "0").strip().lower() in {"1", "true", "yes", "sim"}:
        print(f"[ISOLAMENTO] FRS_SKIP_CLEAN=1: preservando ZIP Flet anterior: {zip_path}")
        return zip_path

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in app_stage_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(app_stage_dir))

    return zip_path


def _compile_inno_bundle(version: str, acbr_installer_name: str) -> Path:
    STAGE_DIR, INSTALLER_DIR = _paths()
    _require_not_protected(INSTALLER_DIR, "installer_dir")
    _require_not_protected(STAGE_DIR, "stage_dir")
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
