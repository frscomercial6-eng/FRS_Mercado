import json
import os
import subprocess
import sys
import threading
import base64
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox
from urllib import error, request
from urllib.parse import urlparse

import customtkinter as ctk

from app_paths import obter_caminho_dados


UPDATE_STATE_FILE = Path(obter_caminho_dados("update_state.json"))


def _github_token() -> str:
    return (os.getenv("GITHUB_TOKEN", "").strip() or os.getenv("GH_TOKEN", "").strip())


def _request_headers(accept: str = "application/json") -> dict:
    headers = {
        "User-Agent": "FRS-Mercado-AutoUpdate",
        "Accept": accept,
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class ReleaseInfo:
    version: str
    asset_name: str
    asset_url: str


class AutoUpdateManager:
    def __init__(self, app, repo: str, enabled: bool = True, remind_hours: int = 24):
        self.app = app
        self.repo = (repo or "").strip()
        self.enabled = bool(enabled)
        self.remind_hours = max(1, int(remind_hours))
        self._check_running = False

    def start_silent_check(self):
        if not self.enabled or not self.repo or self._check_running:
            return

        self._check_running = True
        thread = threading.Thread(target=self._check_worker, daemon=True)
        thread.start()

    def force_check_now(self):
        """Executa verificação imediata sob demanda do usuário."""
        if not self.repo:
            messagebox.showwarning(
                "Atualização",
                "Repositório de atualização não configurado.",
                parent=self.app,
            )
            return

        if self._check_running:
            messagebox.showinfo(
                "Atualização",
                "Já existe uma verificação em andamento. Aguarde alguns segundos.",
                parent=self.app,
            )
            return

        self._check_running = True
        threading.Thread(target=self._force_check_worker, daemon=True).start()

    def _force_check_worker(self):
        try:
            local_version = get_local_version()
            release = fetch_update_manifest(self.repo)
            if release is None:
                self.app.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Atualização",
                        "Não foi possível consultar o GitHub agora. Tente novamente em instantes.",
                        parent=self.app,
                    ),
                )
                return

            if compare_versions(release.version, local_version) > 0:
                self.app.after(0, lambda: self._show_update_dialog(local_version, release))
                return

            self.app.after(
                0,
                lambda: messagebox.showinfo(
                    "Atualização",
                    "Você já está usando a versão mais atual do FRS Mercado",
                    parent=self.app,
                ),
            )
        except Exception:
            self.app.after(
                0,
                lambda: messagebox.showwarning(
                    "Atualização",
                    "Falha ao verificar atualização no momento.",
                    parent=self.app,
                ),
            )
        finally:
            self._check_running = False

    def _check_worker(self):
        try:
            local_version = get_local_version()
            release = fetch_update_manifest(self.repo)
            if release is None:
                return

            if compare_versions(release.version, local_version) <= 0:
                return

            if self._should_defer(release.version):
                return

            self.app.after(0, lambda: self._show_update_dialog(local_version, release))
        except Exception:
            # Falhas de rede/API nunca podem travar o sistema.
            return
        finally:
            self._check_running = False

    def _show_update_dialog(self, local_version: str, release: ReleaseInfo):
        if not self.app.winfo_exists():
            return
        mensagem = (
            "Uma nova versão está disponível. Deseja atualizar agora?\n\n"
            f"Versão atual: {local_version}\n"
            f"Nova versão: {release.version}"
        )
        if messagebox.askyesno("Update Disponível", mensagem, parent=self.app):
            self._run_update_flow(release)
        else:
            self._save_defer_state(release.version)

    def _run_update_flow(self, release: ReleaseInfo):
        def worker():
            try:
                download_dir = Path(obter_caminho_dados("updates"))
                download_dir.mkdir(parents=True, exist_ok=True)
                installer_path = download_dir / release.asset_name
                download_file(release.asset_url, installer_path)
                self.app.after(0, lambda: self._launch_installer(installer_path))
            except Exception:
                self.app.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Atualização",
                        "Não foi possível baixar a atualização agora. O sistema continuará normalmente.",
                        parent=self.app,
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _launch_installer(self, installer_path: Path):
        try:
            subprocess.Popen([str(installer_path)], cwd=str(installer_path.parent))
        except Exception:
            messagebox.showwarning(
                "Atualização",
                "Falha ao iniciar o instalador da atualização. O sistema continuará normalmente.",
                parent=self.app,
            )
            return

        try:
            self.app.fechar_sistema()
        except Exception:
            try:
                self.app.destroy()
            except Exception:
                pass

    def _should_defer(self, version: str) -> bool:
        data = read_update_state()
        if data.get("deferred_version") != version:
            return False

        until = int(data.get("defer_until_ts", 0) or 0)
        return until > current_ts()

    def _save_defer_state(self, version: str):
        defer_seconds = self.remind_hours * 3600
        payload = {
            "deferred_version": version,
            "defer_until_ts": current_ts() + defer_seconds,
        }
        write_update_state(payload)


def current_ts() -> int:
    import time

    return int(time.time())


def read_update_state() -> dict:
    try:
        if not UPDATE_STATE_FILE.exists():
            return {}
        return json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_update_state(data: dict) -> None:
    try:
        UPDATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_local_version() -> str:
    candidates = []

    exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    candidates.append(exe_dir / "version.txt")
    candidates.append(Path.cwd() / "version.txt")
    candidates.append(Path(__file__).resolve().parent / "version.txt")

    for path in candidates:
        try:
            if path.exists():
                version = path.read_text(encoding="utf-8").strip()
                if version:
                    return normalize_version(version)
        except Exception:
            continue

    try:
        from release_info import APP_VERSION

        return normalize_version(APP_VERSION)
    except Exception:
        return "0.0.0"


def _manifest_urls(repo: str) -> list[str]:
    repo = str(repo or "").strip().strip("/")
    if "/" not in repo:
        return []

    owner, name = repo.split("/", 1)
    return [
        f"https://api.github.com/repos/{owner}/{name}/contents/version.json?ref=main",
        f"https://api.github.com/repos/{owner}/{name}/contents/version.json?ref=master",
        f"https://raw.githubusercontent.com/{owner}/{name}/main/version.json",
        f"https://raw.githubusercontent.com/{owner}/{name}/master/version.json",
    ]


def _asset_name_from_url(url: str) -> str:
    path = urlparse(str(url or "").strip()).path
    name = Path(path).name
    return name or "FRS_Mercado_Update.exe"


def fetch_manifest_payload(repo: str) -> dict | None:
    """Retorna o payload bruto do manifesto de atualização no GitHub."""
    for url in _manifest_urls(repo):
        req = request.Request(
            url,
            headers=_request_headers("application/vnd.github+json, application/json"),
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except error.URLError:
            continue
        except Exception:
            continue

        if isinstance(payload, dict):
            if "content" in payload and "encoding" in payload:
                try:
                    raw = str(payload.get("content") or "")
                    if str(payload.get("encoding") or "").lower() == "base64":
                        decoded = base64.b64decode(raw).decode("utf-8")
                        nested = json.loads(decoded)
                        if isinstance(nested, dict):
                            return nested
                except Exception:
                    continue
            return payload

    return None


def fetch_update_manifest(repo: str) -> ReleaseInfo | None:
    payload = fetch_manifest_payload(repo)
    if not isinstance(payload, dict):
        return None

    latest_version = normalize_version(payload.get("latest_version"))
    download_url = str(payload.get("download_url") or "").strip()
    if not latest_version or not download_url:
        return None

    return ReleaseInfo(
        version=latest_version,
        asset_name=_asset_name_from_url(download_url),
        asset_url=download_url,
    )


def fetch_latest_release(repo: str) -> ReleaseInfo | None:
    """Compatibilidade retroativa com chamadas legadas do projeto."""
    return fetch_update_manifest(repo)


def download_file(url: str, destination: Path) -> None:
    if not url:
        raise RuntimeError("URL de download inválida")

    req = request.Request(url, headers=_request_headers("application/octet-stream, */*"), method="GET")
    with request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError("Download vazio")

    destination.write_bytes(data)


def normalize_version(version: str) -> str:
    raw = str(version or "").strip().lstrip("vV")
    parts = [p for p in raw.split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def compare_versions(a: str, b: str) -> int:
    pa = [int(x) for x in normalize_version(a).split(".")]
    pb = [int(x) for x in normalize_version(b).split(".")]
    if pa > pb:
        return 1
    if pa < pb:
        return -1
    return 0
