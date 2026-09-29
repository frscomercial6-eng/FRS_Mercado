"""Núcleo seguro do atualizador do FRS Mercado.

O cliente nunca substitui o runtime por um arquivo remoto não validado. Um
manifesto Ed25519 autenticado descreve o asset; o download é temporário,
validado por tamanho/SHA-256 e só então enviado ao helper externo.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tkinter import messagebox
from urllib import request
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app_paths import e_compilado, executavel_atual, obter_caminho_dados
from licensing.canonical_json import canonical_json_bytes

MANIFEST_SCHEMA = "FRS-MERCADO-UPDATE-MANIFEST-V1"
PRODUCT = "FRS-Mercado"
DEFAULT_CHANNEL = "stable"
MANIFEST_NAMES = ("update-manifest.json", "update_manifest.json")
UPDATE_STATE_FILE = Path(obter_caminho_dados("update_state.json"))
UPDATE_ROOT = Path(obter_caminho_dados("updates"))
UPDATE_LOCK_FILE = UPDATE_ROOT / "update.lock"
PUBLIC_KEYS_FILE = Path(__file__).with_name("updater_public_keys.json")


class UpdateState(str, Enum):
    CHECKING = "CHECKING"
    AVAILABLE = "AVAILABLE"
    DEFERRED = "DEFERRED"
    DOWNLOADING = "DOWNLOADING"
    STAGED = "STAGED"
    WAITING_FOR_EXIT = "WAITING_FOR_EXIT"
    BACKING_UP = "BACKING_UP"
    INSTALLING = "INSTALLING"
    VERIFYING = "VERIFYING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class UpdateSecurityError(RuntimeError):
    """Manifesto, asset ou estado de atualização não confiável."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    asset_name: str
    asset_url: str
    auto_update: bool = True
    product: str = PRODUCT
    channel: str = DEFAULT_CHANNEL
    sequence: int = 0
    issued_at: str = ""
    size: int = 0
    sha256: str = ""
    key_id: str = ""
    asset_type: str = "portable_zip"
    manifest: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _github_token() -> str:
    return (os.getenv("FRS_UPDATER_GITHUB_TOKEN", "").strip()
            or os.getenv("GITHUB_TOKEN", "").strip()
            or os.getenv("GH_TOKEN", "").strip())


def _request_headers(accept: str = "application/json") -> dict[str, str]:
    headers = {"User-Agent": "FRS-Mercado-SecureUpdater/2", "Accept": accept}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def current_ts() -> int:
    return int(time.time())


def datetime_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_version(version: str) -> str:
    raw = str(version or "").strip().lstrip("vV")
    parts = [p for p in raw.split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def compare_versions(a: str, b: str) -> int:
    pa = [int(x) for x in normalize_version(a).split(".")]
    pb = [int(x) for x in normalize_version(b).split(".")]
    return (pa > pb) - (pa < pb)


def get_local_version() -> str:
    roots = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
    roots.append(Path.cwd())
    for root in roots:
        for filename in ("version.json", "version.txt", "release_info.py"):
            path = root / filename
            if not path.exists():
                continue
            try:
                if filename == "version.json":
                    value = json.loads(path.read_text(encoding="utf-8")).get("latest_version", "")
                elif filename == "version.txt":
                    value = path.read_text(encoding="utf-8").strip()
                else:
                    import re
                    match = re.search(r"APP_VERSION\s*=\s*[\"']([^\"']+)", path.read_text(encoding="utf-8"))
                    value = match.group(1) if match else ""
                if value:
                    return normalize_version(value)
            except Exception:
                continue
    return "0.0.0"


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def read_update_state() -> dict:
    try:
        payload = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def write_update_state(data: dict) -> None:
    _atomic_json_write(UPDATE_STATE_FILE, dict(data))


def _set_state(state: UpdateState, **values) -> dict:
    payload = dict(read_update_state())
    payload.update(values)
    payload["state"] = state.value
    payload["updated_at"] = datetime_now_iso()
    write_update_state(payload)
    return payload


def _manifest_urls(repo: str, manifest_name: str = MANIFEST_NAMES[0]) -> list[str]:
    repo = str(repo or "").strip().strip("/")
    if "/" not in repo:
        return []
    owner, name = repo.split("/", 1)
    return [
        f"https://api.github.com/repos/{owner}/{name}/contents/{manifest_name}",
        f"https://raw.githubusercontent.com/{owner}/{name}/main/{manifest_name}",
        f"https://raw.githubusercontent.com/{owner}/{name}/master/{manifest_name}",
    ]


def _asset_name_from_url(url: str) -> str:
    return Path(urlparse(str(url or "").strip()).path).name or "FRS_Mercado_Update.bin"


def _load_public_keys() -> dict[str, str]:
    try:
        payload = json.loads(PUBLIC_KEYS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UpdateSecurityError("Lista de chaves públicas do updater ausente ou inválida.") from exc
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, dict) or not keys:
        raise UpdateSecurityError("Nenhuma chave pública do updater está configurada.")
    return {str(k): str(v).strip() for k, v in keys.items() if str(v).strip()}


def _public_key(key_id: str, trusted: dict[str, str] | None = None) -> Ed25519PublicKey:
    values = trusted if trusted is not None else _load_public_keys()
    encoded = str(values.get(str(key_id or "").strip()) or "").strip()
    if not encoded:
        raise UpdateSecurityError(f"Chave pública desconhecida: {key_id or 'ausente'}.")
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise UpdateSecurityError("Chave pública do updater inválida.") from exc


def verify_manifest(envelope: dict, trusted_keys: dict[str, str] | None = None) -> dict:
    if not isinstance(envelope, dict) or envelope.get("schema") != MANIFEST_SCHEMA:
        raise UpdateSecurityError("Schema de manifesto inválido.")
    unexpected_fields = set(envelope) - {"schema", "key_id", "payload", "signature"}
    if unexpected_fields:
        raise UpdateSecurityError(f"Manifesto contém campos não assinados: {sorted(unexpected_fields)}")
    key_id = str(envelope.get("key_id") or "").strip()
    payload = envelope.get("payload")
    signature = str(envelope.get("signature") or "").strip()
    if not key_id or not isinstance(payload, dict) or not signature:
        raise UpdateSecurityError("Manifesto assinado incompleto.")
    try:
        raw_signature = base64.b64decode(signature, validate=True)
    except Exception as exc:
        raise UpdateSecurityError("Assinatura do manifesto inválida.") from exc
    message = canonical_json_bytes({"schema": MANIFEST_SCHEMA, "key_id": key_id, "payload": payload})
    try:
        _public_key(key_id, trusted_keys).verify(raw_signature, message)
    except UpdateSecurityError:
        raise
    except Exception as exc:
        raise UpdateSecurityError("Não foi possível verificar a assinatura do manifesto.") from exc
    if str(payload.get("product") or "") != PRODUCT:
        raise UpdateSecurityError("Manifesto de outro produto.")
    if str(payload.get("channel") or DEFAULT_CHANNEL) != DEFAULT_CHANNEL:
        raise UpdateSecurityError("Canal de atualização não permitido.")
    for field in ("version", "sequence", "issued_at", "assets"):
        if field not in payload:
            raise UpdateSecurityError(f"Manifesto sem campo obrigatório: {field}.")
    try:
        if int(payload.get("sequence")) <= 0:
            raise ValueError
        datetime.fromisoformat(str(payload.get("issued_at")).replace("Z", "+00:00"))
    except Exception as exc:
        raise UpdateSecurityError("Sequence ou issued_at do manifesto inválidos.") from exc
    last_sequence = int(read_update_state().get("sequence") or 0)
    if int(payload.get("sequence")) < last_sequence:
        raise UpdateSecurityError("Manifesto mais antigo que a sequência já observada.")
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise UpdateSecurityError("Manifesto sem assets.")
    for asset in assets:
        if not isinstance(asset, dict) or not all(k in asset for k in ("url", "size", "sha256", "asset_type")):
            raise UpdateSecurityError("Asset do manifesto incompleto.")
        try:
            parsed_url = urlparse(str(asset.get("url") or "").strip())
            url_ok = (
                parsed_url.scheme.lower() == "https"
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
            )
        except ValueError:
            url_ok = False
        if not url_ok:
            raise UpdateSecurityError("URL de asset deve usar HTTPS válida e sem credenciais.")
        sha256 = str(asset.get("sha256") or "").strip()
        try:
            valid_sha256 = len(sha256) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha256)
            if int(asset.get("size")) <= 0 or not valid_sha256:
                raise ValueError
        except Exception as exc:
            raise UpdateSecurityError("Tamanho ou SHA-256 do asset inválido.") from exc
    return dict(payload)


def _decode_github_payload(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("content") is not None and payload.get("encoding"):
        try:
            raw = base64.b64decode(str(payload.get("content"))).decode("utf-8")
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, dict) else None
        except Exception:
            return None
    return payload


def fetch_manifest_payload(repo: str, manifest_name: str = MANIFEST_NAMES[0]) -> dict | None:
    """Busca JSON bruto para diagnóstico; a atualização exige verify_manifest()."""
    for url in _manifest_urls(repo, manifest_name):
        try:
            req = request.Request(url, headers=_request_headers("application/vnd.github+json, application/json"), method="GET")
            with request.urlopen(req, timeout=10) as response:
                raw = json.loads(response.read().decode("utf-8"))
            decoded = _decode_github_payload(raw)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            continue
    return None


def _asset_from_manifest(payload: dict, asset_type: str = "portable_zip") -> dict:
    assets = payload.get("assets") or []
    for asset in assets:
        if str(asset.get("asset_type") or "portable_zip") == asset_type:
            return dict(asset)
    return dict(assets[0]) if assets else {}


def fetch_update_manifest(repo: str, asset_type: str = "portable_zip") -> ReleaseInfo | None:
    envelope = fetch_manifest_payload(repo)
    if envelope is None:
        return None
    payload = verify_manifest(envelope)
    asset = _asset_from_manifest(payload, asset_type)
    return ReleaseInfo(
        version=normalize_version(payload.get("version")),
        asset_name=str(asset.get("name") or _asset_name_from_url(asset.get("url"))),
        asset_url=str(asset.get("url")),
        auto_update=bool(payload.get("auto_update", True)),
        product=PRODUCT,
        channel=DEFAULT_CHANNEL,
        sequence=int(payload.get("sequence") or 0),
        issued_at=str(payload.get("issued_at") or ""),
        size=int(asset.get("size") or 0),
        sha256=str(asset.get("sha256") or "").lower(),
        key_id=str(envelope.get("key_id") or ""),
        asset_type=str(asset.get("asset_type") or asset_type),
        manifest={"schema": envelope.get("schema"), "key_id": envelope.get("key_id"), "payload": payload, "signature": envelope.get("signature")},
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_asset(release: ReleaseInfo, destination: Path, progress=None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        partial.unlink(missing_ok=True)
    except Exception:
        pass
    req = request.Request(release.asset_url, headers=_request_headers("application/octet-stream, */*"), method="GET")
    digest = hashlib.sha256()
    total = 0
    try:
        with request.urlopen(req, timeout=90) as response, partial.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                if progress:
                    progress(total, release.size or total)
        if total != int(release.size or total):
            raise UpdateSecurityError("Tamanho do asset diferente do manifesto.")
        if digest.hexdigest().lower() != str(release.sha256).lower():
            raise UpdateSecurityError("SHA-256 do asset não confere.")
        os.replace(partial, destination)
        return destination
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _operational_gate(parent) -> tuple[bool, str]:
    """Impede atualização com qualquer operação que possa perder estado."""
    if parent is None:
        return True, ""
    pdv = getattr(parent, "_janela_pdv", None)
    if pdv is not None:
        try:
            if getattr(pdv, "caixa_id", None):
                return False, "Há um caixa aberto."
            if getattr(pdv, "itens_carrinho", None):
                return False, "Há itens no carrinho."
            if getattr(pdv, "valor_pago_acumulado", 0.0):
                return False, "Há um pagamento em andamento."
            if getattr(pdv, "pagamentos_parciais", None):
                return False, "Há um pagamento múltiplo em andamento."
            if getattr(pdv, "modal_abertura", None) is not None:
                return False, "Há uma tela de abertura/fechamento ativa."
            for janela in pdv.winfo_children():
                if isinstance(janela, type(pdv)) or str(janela.winfo_class()).lower().endswith("toplevel"):
                    if janela.winfo_exists() and bool(janela.winfo_ismapped()):
                        return False, "Há uma janela modal do PDV ativa."
        except Exception:
            return False, "Não foi possível confirmar o estado do PDV."
    if getattr(parent, "_backup_em_execucao", False):
        return False, "Há um backup em andamento."
    if getattr(parent, "_fiscal_alerta_em_exibicao", False):
        return False, "Há uma operação fiscal em andamento."
    monitor = getattr(parent, "_system_monitor", None)
    if bool(getattr(monitor, "_instalacao_acbr_em_andamento", False)):
        return False, "Há uma instalação/integração fiscal em andamento."
    return True, ""



class Updater:
    """Fachada de UI sobre o núcleo seguro; nenhuma instalação ocorre no PID atual."""

    def __init__(self, parent=None, gate_provider=None):
        self.parent = parent
        self.gate_provider = gate_provider
        self._janela = None
        self._barra = None
        self._lbl_status = None
        self._check_running = False
        self._release_pendente = None
        self.last_error = ""

    def _gate(self) -> tuple[bool, str]:
        if callable(self.gate_provider):
            try:
                resultado = self.gate_provider()
                if isinstance(resultado, tuple) and len(resultado) == 2:
                    return bool(resultado[0]), str(resultado[1] or "")
                return bool(resultado), ""
            except Exception as exc:
                return False, f"Não foi possível confirmar o estado operacional: {exc}"
        return _operational_gate(self.parent)

    def _run_on_ui(self, callback):
        if self.parent is not None and hasattr(self.parent, "after"):
            try:
                self.parent.after(0, callback)
                return
            except Exception:
                pass
        callback()

    def _should_defer(self, version: str) -> bool:
        data = read_update_state()
        return str(data.get("version") or "") == str(version) and int(data.get("deferred_until_ts") or 0) > current_ts()

    def _save_defer(self, release: ReleaseInfo, remind_hours: int = 24) -> None:
        _set_state(
            UpdateState.DEFERRED,
            version=release.version,
            asset_url=release.asset_url,
            asset_name=release.asset_name,
            asset_type=release.asset_type,
            size=release.size,
            sha256=release.sha256,
            key_id=release.key_id,
            sequence=release.sequence,
            issued_at=release.issued_at,
            manifest=release.manifest,
            deferred_until_ts=current_ts() + max(1, int(remind_hours)) * 3600,
            error="",
        )

    def checar_atualizacao(self, repo: str, considerar_adiamento=False) -> ReleaseInfo | None:
        _set_state(UpdateState.CHECKING, error="")
        try:
            release = fetch_update_manifest(str(repo or "").strip())
        except Exception as exc:
            self.last_error = str(exc)
            _set_state(UpdateState.FAILED, error=str(exc))
            return None
        if release is None or not release.auto_update:
            return None
        if compare_versions(release.version, get_local_version()) <= 0:
            return None
        if considerar_adiamento and self._should_defer(release.version):
            return None
        self._release_pendente = release
        _set_state(
            UpdateState.AVAILABLE,
            version=release.version,
            asset_url=release.asset_url,
            asset_name=release.asset_name,
            asset_type=release.asset_type,
            size=release.size,
            sha256=release.sha256,
            key_id=release.key_id,
            sequence=release.sequence,
            issued_at=release.issued_at,
            manifest=release.manifest,
            error="",
        )
        return release

    def _release_pendente_atual(self) -> ReleaseInfo | None:
        if self._release_pendente is not None:
            return self._release_pendente
        data = read_update_state()
        required = ("version", "asset_url", "asset_name", "size", "sha256", "key_id")
        if str(data.get("state") or "") not in {
            UpdateState.AVAILABLE.value,
            UpdateState.DEFERRED.value,
            UpdateState.STAGED.value,
            UpdateState.WAITING_FOR_EXIT.value,
        } or not all(data.get(k) not in (None, "") for k in required):
            return None
        return ReleaseInfo(
            version=normalize_version(data.get("version")),
            asset_name=str(data.get("asset_name")),
            asset_url=str(data.get("asset_url")),
            auto_update=bool(data.get("auto_update", True)),
            product=PRODUCT,
            channel=DEFAULT_CHANNEL,
            sequence=int(data.get("sequence") or 0),
            issued_at=str(data.get("issued_at") or ""),
            size=int(data.get("size") or 0),
            sha256=str(data.get("sha256") or ""),
            key_id=str(data.get("key_id") or ""),
            asset_type=str(data.get("asset_type") or "portable_zip"),
            manifest=data.get("manifest") if isinstance(data.get("manifest"), dict) else None,
        )

    def has_pending_update(self) -> bool:
        return self._release_pendente_atual() is not None

    def _validate_asset_content(self, path: Path, release: ReleaseInfo) -> None:
        if not path.is_file() or path.stat().st_size != int(release.size):
            raise UpdateSecurityError("Arquivo baixado não corresponde ao tamanho assinado.")
        if _sha256_file(path).lower() != str(release.sha256).lower():
            raise UpdateSecurityError("Arquivo baixado não corresponde ao SHA-256 assinado.")
        if release.asset_type == "portable_zip":
            if not zipfile.is_zipfile(path):
                raise UpdateSecurityError("Pacote Portable não é um ZIP válido.")
            with zipfile.ZipFile(path, "r") as package:
                if package.testzip() is not None:
                    raise UpdateSecurityError("Pacote Portable contém entrada corrompida.")
                names = [name.replace("\\", "/") for name in package.namelist()]
                if not any(Path(name).name.lower() == "frs_mercado.exe" for name in names):
                    raise UpdateSecurityError("Pacote Portable não contém FRS_Mercado.exe.")
        elif release.asset_type in {"installer_exe", "traditional_exe"}:
            with path.open("rb") as handle:
                if handle.read(2) != b"MZ":
                    raise UpdateSecurityError("Pacote traditional não é um executável PE válido.")
        else:
            raise UpdateSecurityError(f"Tipo de asset não suportado: {release.asset_type}")

    def _stage_release(self, release: ReleaseInfo) -> bool:
        permitido, motivo = self._gate()
        if not permitido:
            self._save_defer(release, 1)
            messagebox.showwarning(
                "Atualização adiada",
                f"A atualização foi adiada porque a operação está ativa: {motivo}",
                parent=self.parent,
            )
            return False
        destino = UPDATE_ROOT / release.version / release.asset_name
        destino.parent.mkdir(parents=True, exist_ok=True)
        _set_state(
            UpdateState.DOWNLOADING,
            version=release.version,
            asset_url=release.asset_url,
            asset_name=release.asset_name,
            asset_type=release.asset_type,
            size=release.size,
            sha256=release.sha256,
            key_id=release.key_id,
            sequence=release.sequence,
            manifest=release.manifest,
            error="",
        )
        try:
            _download_asset(release, destino)
            self._validate_asset_content(destino, release)
            job = destino.parent / "update-job.json"
            _atomic_json_write(
                job,
                {
                    "schema": "FRS-MERCADO-UPDATE-JOB-V1",
                    "parent_pid": os.getpid(),
                    "created_at": datetime_now_iso(),
                    "release": release.as_dict(),
                    "asset_path": str(destino),
                    "job_path": str(job),
                },
            )
            _set_state(
                UpdateState.STAGED,
                version=release.version,
                asset_url=release.asset_url,
                asset_name=release.asset_name,
                asset_type=release.asset_type,
                size=release.size,
                sha256=release.sha256,
                key_id=release.key_id,
                sequence=release.sequence,
                manifest=release.manifest,
                staged_path=str(destino),
                job_path=str(job),
                error="",
            )
            self._release_pendente = release
            return True
        except Exception as exc:
            destino.unlink(missing_ok=True)
            destino.with_suffix(destino.suffix + ".part").unlink(missing_ok=True)
            _set_state(UpdateState.FAILED, error=str(exc))
            messagebox.showerror("Atualização", f"Falha ao preparar a atualização: {exc}", parent=self.parent)
            return False

    def _show_update_dialog(self, release: ReleaseInfo, remind_hours: int = 24) -> None:
        if self.parent is not None and hasattr(self.parent, "winfo_exists"):
            try:
                if not self.parent.winfo_exists():
                    return
            except Exception:
                return
        try:
            confirmar = messagebox.askyesno(
                "Atualização disponível",
                "Nova atualização disponível. Deseja preparar a atualização?\n\n"
                f"Versão atual: {get_local_version()}\nVersão disponível: {release.version}\n\n"
                "A instalação ocorrerá somente após o fechamento seguro do sistema.",
                parent=self.parent,
            )
        except Exception:
            confirmar = False
        if confirmar:
            def _worker():
                if self._stage_release(release):
                    self._run_on_ui(lambda: messagebox.showinfo(
                        "Atualização preparada",
                        "O pacote foi validado. A instalação será reconfirmada no encerramento seguro.",
                        parent=self.parent,
                    ))
            threading.Thread(target=_worker, daemon=True).start()
        else:
            self._save_defer(release, remind_hours)

    def start_silent_check(self, repo: str, enabled=True, remind_hours=24) -> None:
        if not enabled or not repo or self._check_running:
            return
        self._check_running = True

        def worker():
            try:
                release = self.checar_atualizacao(repo, considerar_adiamento=True)
                if release is not None:
                    self._run_on_ui(lambda: self._show_update_dialog(release, remind_hours))
            finally:
                self._check_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _find_helper(self) -> list[str] | None:
        """Localiza o helper externo em builds compilados.

        Num build compilado procura SEMPRE o executável
        ``FRS_Mercado_UpdateHelper.exe`` — primeiro na pasta do executável real
        e depois em ``<dados>/updates/bin``. Nunca procura o ``updater_helper.py``
        de código-fonte, pois esse arquivo não é embarcado em builds.
        """
        if e_compilado():
            candidatos = []
            executavel = executavel_atual()
            if executavel is not None:
                candidatos.append(executavel.parent / "FRS_Mercado_UpdateHelper.exe")
            candidatos.append(
                Path(obter_caminho_dados("updates", "bin")) / "FRS_Mercado_UpdateHelper.exe"
            )
            for helper in candidatos:
                if helper.is_file():
                    return [str(helper)]
            return None
        helper = Path(__file__).resolve().with_name("updater_helper.py")
        return [sys.executable, str(helper)] if helper.is_file() else None

    def _launch_helper(self, job_path: Path) -> bool:
        command = self._find_helper()
        if not command:
            self.last_error = "Helper de atualização não está disponível neste build."
            _set_state(UpdateState.FAILED, error=self.last_error)
            messagebox.showerror(
                "Atualização",
                "O componente seguro de atualização não está disponível. O sistema atual continuará funcionando.",
                parent=self.parent,
            )
            return False
        payload = json.loads(job_path.read_text(encoding="utf-8"))
        payload["parent_pid"] = os.getpid()
        _atomic_json_write(job_path, payload)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(
                [*command, "--job", str(job_path)],
                cwd=str(Path(command[-1]).resolve().parent),
                close_fds=True,
                creationflags=flags,
            )
            _set_state(UpdateState.WAITING_FOR_EXIT, job_path=str(job_path), error="")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            _set_state(UpdateState.FAILED, error=str(exc))
            return False

    def preparar_instalacao_no_encerramento(self) -> bool:
        release = self._release_pendente_atual()
        if release is None:
            return False
        confirmado = messagebox.askyesno(
            "Atualização pendente",
            f"A versão {release.version} está disponível. Deseja instalar agora ao encerrar?",
            parent=self.parent,
        )
        if not confirmado:
            self._save_defer(release, 24)
            return False
        permitido, motivo = self._gate()
        if not permitido:
            messagebox.showwarning(
                "Atualização adiada",
                f"Não é possível encerrar e atualizar agora: {motivo}",
                parent=self.parent,
            )
            return None
        estado = read_update_state()
        job_path = Path(str(estado.get("job_path") or ""))
        staged_path = Path(str(estado.get("staged_path") or ""))
        if (
            str(estado.get("state") or "") != UpdateState.STAGED.value
            or not job_path.is_file()
            or not staged_path.is_file()
        ):
            if not self._stage_release(release):
                return False
            estado = read_update_state()
            job_path = Path(str(estado.get("job_path") or ""))
        try:
            self._validate_asset_content(staged_path, release)
        except Exception as exc:
            self.last_error = str(exc)
            _set_state(UpdateState.FAILED, error=str(exc))
            messagebox.showerror("Atualização", str(exc), parent=self.parent)
            return False
        return self._launch_helper(job_path)

    def aplicar_atualizacao(self, repo: str = "") -> bool:
        """Compatibilidade: prepara a atualização; nunca substitui o EXE no PID atual."""
        release = self._release_pendente_atual()
        if release is None and repo:
            release = self.checar_atualizacao(repo)
        if release is None:
            return False
        return self._stage_release(release)

    def start_login_notice_check(self, repo: str, enabled=True, on_available=None) -> None:
        """Compatibilidade: o login nunca executa rede ou updater."""
        return None

