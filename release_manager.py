import re
import subprocess
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
VERSION_FILE = ROOT_DIR / "version.txt"
CHANGELOG_FILE = ROOT_DIR / "CHANGELOG.md"
EULA_TEMPLATE = ROOT_DIR / "EULA.template.txt"
EULA_OUTPUT = ROOT_DIR / "EULA.txt"
CONTRATO_TEMPLATE = ROOT_DIR / "contrato_template.md"
CONTRATO_OUTPUT = ROOT_DIR / "Contrato_FRS_Atual.md"
RELEASE_INFO_FILE = ROOT_DIR / "release_info.py"


def read_version() -> str:
    if not VERSION_FILE.exists():
        VERSION_FILE.write_text("1.0.0\n", encoding="utf-8")

    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(
            f"Versão inválida em {VERSION_FILE.name}: '{version}'. Use formato X.Y.Z (ex.: 1.0.1)."
        )
    return version


def _today_br() -> str:
    return datetime.now().strftime("%d/%m/%Y")


def _render_template(template_path: Path, output_path: Path, replacements: dict[str, str]) -> None:
    if not template_path.exists():
        raise FileNotFoundError(f"Template não encontrado: {template_path}")

    content = template_path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        content = content.replace(f"{{{{{key}}}}}", value)

    output_path.write_text(content, encoding="utf-8")


def _replace_first_regex(file_path: Path, pattern: str, replacement: str) -> None:
    content = file_path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count == 0:
        raise RuntimeError(f"Não foi possível atualizar '{file_path.name}' com padrão: {pattern}")
    file_path.write_text(updated, encoding="utf-8")


def _sync_setup_iss(version: str) -> None:
    setup_file = ROOT_DIR / "setup_frs.iss"
    _replace_first_regex(setup_file, r'^#define MyAppVersion ".*"$', f'#define MyAppVersion "{version}"')
    _replace_first_regex(
        setup_file,
        r"^OutputBaseFilename=FRS_Mercado_Setup_.*$",
        f"OutputBaseFilename=FRS_Mercado_Setup_{version}",
    )


def _sync_checklist(version: str) -> None:
    checklist = ROOT_DIR / "checklist_homologacao.md"
    content = checklist.read_text(encoding="utf-8")
    content = re.sub(
        r"installer/FRS_Mercado_Portable_[0-9]+\.[0-9]+\.[0-9]+\.zip",
        f"installer/FRS_Mercado_Portable_{version}.zip",
        content,
    )
    content = re.sub(
        r"installer/FRS_Mercado_Setup_[0-9]+\.[0-9]+\.[0-9]+\.exe",
        f"installer/FRS_Mercado_Setup_{version}.exe",
        content,
    )
    checklist.write_text(content, encoding="utf-8")


def _write_release_info(version: str, date_str: str) -> None:
    repo = _infer_github_repo()
    RELEASE_INFO_FILE.write_text(
        (
            '"""Arquivo gerado automaticamente pelo release_manager. Não editar manualmente."""\n\n'
            f'APP_VERSION = "{version}"\n'
            f'RELEASE_DATE = "{date_str}"\n'
            f'GITHUB_REPOSITORY = "{repo}"\n'
        ),
        encoding="utf-8",
    )


def _infer_github_repo() -> str:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(ROOT_DIR),
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            return ""

        remote = proc.stdout.strip()
        if remote.endswith(".git"):
            remote = remote[:-4]
        if remote.startswith("git@github.com:"):
            return remote.split(":", 1)[1].strip("/")
        if "github.com/" in remote:
            return remote.split("github.com/", 1)[1].strip("/")
    except Exception:
        return ""
    return ""


def _append_changelog(version: str, date_str: str) -> None:
    if not CHANGELOG_FILE.exists():
        CHANGELOG_FILE.write_text("# Changelog\n\n", encoding="utf-8")

    content = CHANGELOG_FILE.read_text(encoding="utf-8")
    header = f"## {version} - {date_str}"
    if header in content:
        return

    with CHANGELOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{header}\n")
        f.write("- Release automatizada gerada pelo Mestre de Release.\n\n")


def prepare_release_artifacts() -> str:
    version = read_version()
    date_str = _today_br()

    _sync_setup_iss(version)
    _sync_checklist(version)

    replacements = {
        "VERSION": version,
        "DATE": date_str,
    }
    _render_template(EULA_TEMPLATE, EULA_OUTPUT, replacements)
    _render_template(CONTRATO_TEMPLATE, CONTRATO_OUTPUT, replacements)
    _write_release_info(version, date_str)
    _append_changelog(version, date_str)

    return version
