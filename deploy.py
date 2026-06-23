import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from urllib import error, parse, request

from release_manager import read_version


ROOT_DIR = Path(__file__).resolve().parent


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT_DIR), text=True, capture_output=True, check=check)


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [s/N]: ").strip().lower()
    return ans in {"s", "sim", "y", "yes"}


def _git_repo_from_remote() -> str | None:
    proc = _run(["git", "remote", "get-url", "origin"], check=False)
    if proc.returncode != 0:
        return None

    remote = proc.stdout.strip()
    if remote.endswith(".git"):
        remote = remote[:-4]

    # git@github.com:owner/repo
    if remote.startswith("git@github.com:"):
        return remote.split(":", 1)[1]

    # https://github.com/owner/repo
    if "github.com/" in remote:
        return remote.split("github.com/", 1)[1].strip("/")

    return None


def _ensure_git_commit(version: str) -> None:
    _run(["git", "add", "-A"])
    commit_msg = f"Release {version}"
    proc = _run(["git", "commit", "-m", commit_msg], check=False)

    if proc.returncode == 0:
        print(f"Commit criado: {commit_msg}")
        return

    out = (proc.stdout + proc.stderr).lower()
    if "nothing to commit" in out or "nada a commitar" in out:
        print("Nenhuma alteração para commit.")
        return

    raise RuntimeError(f"Falha no commit:\n{proc.stdout}\n{proc.stderr}")


def _ensure_tag(version: str) -> str:
    tag = f"v{version}"
    proc = _run(["git", "tag", "-l", tag], check=False)
    if proc.returncode == 0 and proc.stdout.strip() == tag:
        print(f"Tag já existe: {tag}")
        return tag

    _run(["git", "tag", "-a", tag, "-m", f"Release {version}"])
    print(f"Tag criada: {tag}")
    return tag


def _push(branch: str, tag: str) -> None:
    _run(["git", "push", "origin", branch])
    _run(["git", "push", "origin", tag])


def _github_api_request(url: str, token: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None):
    req_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FRS-Mercado-Deploy",
    }
    if headers:
        req_headers.update(headers)

    req = request.Request(url, data=data, method=method, headers=req_headers)
    with request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def _create_or_get_release(repo: str, tag: str, version: str, token: str) -> dict:
    owner, name = repo.split("/", 1)
    base = f"https://api.github.com/repos/{owner}/{name}"

    # Tenta buscar release existente pela tag.
    try:
        return _github_api_request(f"{base}/releases/tags/{tag}", token)
    except error.HTTPError as e:
        if e.code != 404:
            raise

    payload = {
        "tag_name": tag,
        "name": f"Release {version}",
        "body": f"Release automatizada {version}.",
        "draft": False,
        "prerelease": False,
    }
    data = json.dumps(payload).encode("utf-8")
    return _github_api_request(
        f"{base}/releases",
        token,
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"},
    )


def _upload_asset(upload_url: str, token: str, asset_path: Path) -> None:
    asset_name = parse.quote(asset_path.name)
    url = upload_url.split("{", 1)[0] + f"?name={asset_name}"
    content_type, _ = mimetypes.guess_type(asset_path.name)
    if not content_type:
        content_type = "application/octet-stream"

    data = asset_path.read_bytes()
    _github_api_request(
        url,
        token,
        method="POST",
        data=data,
        headers={"Content-Type": content_type},
    )
    print(f"Asset enviado: {asset_path.name}")


def _release_assets(version: str) -> list[Path]:
    candidates = [
        ROOT_DIR / "dist" / "FRS_Mercado.exe",
        ROOT_DIR / "installer" / f"FRS_Mercado_Setup_{version}.exe",
        ROOT_DIR / "installer" / f"FRS_Mercado_Portable_{version}.zip",
    ]
    return [p for p in candidates if p.exists()]


def main() -> None:
    version = read_version()
    tag = f"v{version}"

    print(f"Versão detectada: {version}")

    if not _confirm("Executar build antes do deploy?"):
        print("Deploy cancelado: build não autorizado.")
        return

    subprocess.run([sys.executable, "build_exe.py"], cwd=str(ROOT_DIR), check=True)

    _ensure_git_commit(version)
    _ensure_tag(version)

    if not _confirm("Confirmar push para o remoto (branch + tag)?"):
        print("Push cancelado pelo operador.")
        return

    branch_proc = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_proc.stdout.strip()
    _push(branch, tag)
    print(f"Push concluído em origin/{branch} e tag {tag}.")

    if not _confirm("Deseja publicar/atualizar release no GitHub e subir artefatos?"):
        print("Publicação de release ignorada pelo operador.")
        return

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN não definido. Release no GitHub não foi publicada.")
        return

    repo = os.getenv("GITHUB_REPOSITORY", "").strip() or _git_repo_from_remote()
    if not repo or "/" not in repo:
        print("Não foi possível resolver owner/repo. Defina GITHUB_REPOSITORY=owner/repo.")
        return

    release = _create_or_get_release(repo, tag, version, token)
    upload_url = release.get("upload_url")
    if not upload_url:
        print("Release criada/encontrada, mas sem upload_url retornado pela API.")
        return

    for asset in _release_assets(version):
        _upload_asset(upload_url, token, asset)

    print("Deploy completo finalizado com sucesso.")


if __name__ == "__main__":
    main()
