import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox
from urllib import error, request

import customtkinter as ctk

from app_paths import obter_caminho_dados


UPDATE_STATE_FILE = Path(obter_caminho_dados("update_state.json"))


@dataclass
class ReleaseInfo:
    version: str
    notes: str
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
            release = fetch_latest_release(self.repo)
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
            release = fetch_latest_release(self.repo)
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

        win = ctk.CTkToplevel(self.app)
        win.title("Update Disponível")
        win.geometry("520x320")
        win.transient(self.app)

        ctk.CTkLabel(
            win,
            text="Nova versão disponível",
            font=("Roboto", 20, "bold"),
            text_color="#2ecc71",
        ).pack(pady=(20, 10))

        ctk.CTkLabel(
            win,
            text=f"Versão atual: {local_version} | Nova versão: {release.version}",
            font=("Roboto", 13),
        ).pack(pady=(0, 10))

        txt = ctk.CTkTextbox(win, width=470, height=150)
        txt.pack(padx=18, pady=8)
        txt.insert("0.0", release.notes or "Sem notas de atualização.")
        txt.configure(state="disabled")

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=14)

        def lembrar_depois():
            self._save_defer_state(release.version)
            win.destroy()

        def atualizar_agora():
            win.destroy()
            self._run_update_flow(release)

        ctk.CTkButton(btns, text="Atualizar Agora", fg_color="#27ae60", command=atualizar_agora).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Lembrar mais tarde", fg_color="#7f8c8d", command=lembrar_depois).pack(side="left", padx=8)

        win.protocol("WM_DELETE_WINDOW", lembrar_depois)

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


def fetch_latest_release(repo: str) -> ReleaseInfo | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "FRS-Mercado-AutoUpdate",
        },
        method="GET",
    )

    try:
        with request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.URLError:
        return None
    except Exception:
        return None

    tag = str(payload.get("tag_name") or "").strip()
    latest_version = normalize_version(tag)
    notes = str(payload.get("body") or "").strip()

    assets = payload.get("assets") or []
    installer = choose_installer_asset(assets)
    if installer is None:
        return None

    return ReleaseInfo(
        version=latest_version,
        notes=notes,
        asset_name=installer["name"],
        asset_url=installer["url"],
    )


def choose_installer_asset(assets: list[dict]) -> dict | None:
    for a in assets:
        name = str(a.get("name") or "")
        if name.lower().endswith(".exe") and "setup" in name.lower():
            return {
                "name": name,
                "url": str(a.get("browser_download_url") or "").strip(),
            }
    return None


def download_file(url: str, destination: Path) -> None:
    if not url:
        raise RuntimeError("URL de download inválida")

    req = request.Request(url, headers={"User-Agent": "FRS-Mercado-AutoUpdate"}, method="GET")
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
