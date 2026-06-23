import subprocess
import sys
from pathlib import Path

from release_manager import prepare_release_artifacts, read_version


ROOT_DIR = Path(__file__).resolve().parent


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [s/N]: ").strip().lower()
    return ans in {"s", "sim", "y", "yes"}


def main() -> None:
    version = read_version()
    print(f"Versão atual em version.txt: {version}")

    version = prepare_release_artifacts()
    print(f"Metadados, contrato, EULA e changelog sincronizados para versão {version}.")

    if _confirm("Deseja executar o build agora?"):
        subprocess.run([sys.executable, "build_exe.py"], cwd=str(ROOT_DIR), check=True)
        print("Build concluído.")

    if _confirm("Deseja iniciar o deploy (commit/tag/push/release)?"):
        subprocess.run([sys.executable, "deploy.py"], cwd=str(ROOT_DIR), check=True)


if __name__ == "__main__":
    main()
