import base64
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox
from urllib import error, request
from urllib.parse import urlparse

import customtkinter as ctk

from modulo_config import carregar_configuracoes
from app_paths import obter_caminho_dados


UPDATE_STATE_FILE = Path(obter_caminho_dados("update_state.json"))


@dataclass
class ReleaseInfo:
    version: str
    asset_name: str
    asset_url: str
    auto_update: bool = True


def _github_token() -> str:
    return (os.getenv("GITHUB_TOKEN", "").strip() or os.getenv("GH_TOKEN", "").strip())


def _request_headers(accept: str = "application/json") -> dict:
    headers = {
        "User-Agent": "FRS-Mercado-Updater",
        "Accept": accept,
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def current_ts() -> int:
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
    auto_update = bool(payload.get("auto_update", True))
    if not latest_version or not download_url:
        return None

    return ReleaseInfo(
        version=latest_version,
        asset_name=_asset_name_from_url(download_url),
        asset_url=download_url,
        auto_update=auto_update,
    )


def fetch_latest_release(repo: str) -> ReleaseInfo | None:
    return fetch_update_manifest(repo)


def check_and_apply_startup_update(repo: str) -> bool:
    """Aplica atualização de forma síncrona no bootstrap quando há versão remota maior."""
    repo = str(repo or "").strip()
    if not repo:
        return False

    release = fetch_update_manifest(repo)
    if release is None:
        return False
    if not release.auto_update:
        return False

    local_version = get_local_version()
    if compare_versions(release.version, local_version) <= 0:
        return False

    updater = Updater(parent=None)
    destino = updater._baixar_release_com_progresso(release.asset_url, release.asset_name)
    updater._executar_instalador(destino)
    updater._reiniciar_aplicacao()
    return True


class Updater:
    def __init__(self, parent=None):
        self.parent = parent
        self._janela = None
        self._barra = None
        self._lbl_status = None
        self._check_running = False

    def checar_atualizacao(self, repo: str, considerar_adiamento=False):
        release = fetch_update_manifest(repo)
        if release is None:
            return None
        if not release.auto_update:
            return None

        local = get_local_version()
        if compare_versions(release.version, local) <= 0:
            return None
        if considerar_adiamento and self._should_defer(release.version):
            return None
        return release

    def aplicar_atualizacao(self, repo: str):
        release = self.checar_atualizacao(repo, considerar_adiamento=False)
        if release is None:
            return False

        self._aplicar_release(release)
        return True

    def start_silent_check(self, repo: str, enabled=True, remind_hours=24):
        if not enabled or not repo or self._check_running:
            return

        self._check_running = True

        def worker():
            try:
                release = self.checar_atualizacao(repo, considerar_adiamento=True)
                if release is None:
                    return

                self._run_on_ui(lambda: self._show_update_dialog(release, remind_hours))
            finally:
                self._check_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_dialog(self, release, remind_hours):
        if self.parent is not None and hasattr(self.parent, "winfo_exists") and not self.parent.winfo_exists():
            return

        local_version = get_local_version()
        mensagem = (
            "Uma nova versão está disponível. Deseja atualizar agora?\n\n"
            f"Versão atual: {local_version}\n"
            f"Nova versão: {release.version}"
        )
        try:
            confirmar = messagebox.askyesno("Update Disponível", mensagem, parent=self.parent)
        except Exception:
            confirmar = messagebox.askyesno("Update Disponível", mensagem)

        if confirmar:
            self._aplicar_release(release)
            return

        self._save_defer_state(release.version, remind_hours)

    def _aplicar_release(self, release):
        if release is None:
            return False

        self._abrir_janela_progresso("Atualizando Sistema...", "Preparando atualização...")

        def worker():
            try:
                destino = self._baixar_release_com_progresso(release.asset_url, release.asset_name)
                self._atualizar_status("Instalando atualização...")
                self._executar_instalador(destino)
                self._atualizar_status("Reiniciando aplicação...")
                self._reiniciar_aplicacao()
            except Exception as e:
                self._fechar_janela_progresso()
                self._alerta("Atualização", f"Falha na atualização: {e}")

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _should_defer(self, version: str) -> bool:
        data = read_update_state()
        if data.get("deferred_version") != version:
            return False
        until = int(data.get("defer_until_ts", 0) or 0)
        return until > current_ts()

    def _save_defer_state(self, version: str, remind_hours=24):
        defer_seconds = max(1, int(remind_hours)) * 3600
        payload = {
            "deferred_version": version,
            "defer_until_ts": current_ts() + defer_seconds,
        }
        write_update_state(payload)

    def _run_on_ui(self, callback):
        if self.parent is not None and hasattr(self.parent, "after"):
            try:
                self.parent.after(0, callback)
                return
            except Exception:
                pass
        callback()

    def _abrir_janela_progresso(self, titulo, mensagem):
        def _open():
            self._janela = ctk.CTkToplevel(self.parent) if self.parent is not None else ctk.CTkToplevel()
            self._janela.title(titulo)
            self._janela.geometry("420x150")
            self._janela.resizable(False, False)
            self._janela.grab_set()

            self._lbl_status = ctk.CTkLabel(self._janela, text=mensagem, font=("Arial", 12, "bold"))
            self._lbl_status.pack(pady=(24, 10), padx=16)

            self._barra = ctk.CTkProgressBar(self._janela, width=360)
            self._barra.pack(pady=8)
            self._barra.set(0)

        if self.parent is not None and hasattr(self.parent, "after"):
            self.parent.after(0, _open)
        else:
            _open()

    def _atualizar_progresso(self, valor):
        def _update():
            if self._barra is not None and self._barra.winfo_exists():
                self._barra.set(max(0.0, min(1.0, float(valor))))

        if self.parent is not None and hasattr(self.parent, "after"):
            self.parent.after(0, _update)
        else:
            _update()

    def _atualizar_status(self, mensagem):
        def _update():
            if self._lbl_status is not None and self._lbl_status.winfo_exists():
                self._lbl_status.configure(text=mensagem)

        if self.parent is not None and hasattr(self.parent, "after"):
            self.parent.after(0, _update)
        else:
            _update()

    def _fechar_janela_progresso(self):
        def _close():
            try:
                if self._janela is not None and self._janela.winfo_exists():
                    self._janela.grab_release()
                    self._janela.destroy()
            except Exception:
                pass

        if self.parent is not None and hasattr(self.parent, "after"):
            self.parent.after(0, _close)
        else:
            _close()

    def _alerta(self, titulo, mensagem):
        def _show():
            try:
                messagebox.showwarning(titulo, mensagem, parent=self.parent)
            except Exception:
                messagebox.showwarning(titulo, mensagem)

        if self.parent is not None and hasattr(self.parent, "after"):
            self.parent.after(0, _show)
        else:
            _show()

    def _baixar_release_com_progresso(self, url, asset_name):
        pasta_updates = Path(obter_caminho_dados("updates"))
        pasta_updates.mkdir(parents=True, exist_ok=True)
        destino = pasta_updates / asset_name

        req = request.Request(url, headers=_request_headers("application/octet-stream, */*"), method="GET")
        with request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", "0") or 0)
            baixado = 0
            bloco = 64 * 1024
            with open(destino, "wb") as f:
                while True:
                    chunk = resp.read(bloco)
                    if not chunk:
                        break
                    f.write(chunk)
                    baixado += len(chunk)
                    if total > 0:
                        self._atualizar_progresso(baixado / total)

        self._atualizar_progresso(1.0)
        return destino

    def _executar_instalador(self, caminho_instalador: Path):
        if not caminho_instalador.exists():
            raise FileNotFoundError(f"Instalador não encontrado: {caminho_instalador}")

        subprocess.Popen([str(caminho_instalador)], cwd=str(caminho_instalador.parent))

    def _resolver_executavel_reinicio(self):
        cfg = carregar_configuracoes()
        candidato = str(cfg.get("update_executable_path") or cfg.get("app_executable_path") or "").strip()
        if candidato and os.path.exists(candidato):
            return candidato

        if getattr(sys, "frozen", False):
            return sys.executable

        return ""

    def _reiniciar_aplicacao(self):
        exe = self._resolver_executavel_reinicio()

        try:
            if exe:
                subprocess.Popen([exe], cwd=str(Path(exe).resolve().parent))
            else:
                subprocess.Popen([sys.executable, "main.py"], cwd=str(Path(__file__).resolve().parent))
        except Exception as e:
            raise RuntimeError(f"Falha ao reiniciar aplicação: {e}")
        finally:
            # Encerramento forçado para finalizar update sem deixar processos pendentes.
            os._exit(0)
