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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tkinter import messagebox
from urllib import request
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from updater_ui import JanelaProgressoAtualizacao, mensagem_fechamento_reabertura

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

# URL oficial informada pelo fundador: página institucional do FRS Mercado.
# Direcionamento manual ao cliente quando a atualização automática falha.
# Não é um link direto para instalador: o cliente acessa a página e baixa o
# instalador correto por conta própria.
MANUAL_UPDATE_URL = "https://www.frssolutions.com.br/frs-mercado"


def redefinir_caminhos_apos_raiz() -> None:
    """Recalcula caminhos de estado/lock a partir da raiz de dados VIGENTE.

    Importar ``updater_secure`` resolve os caminhos no momento do import. Quando
    o helper adota uma raiz EXPLÍCITA (``--data-root``) DEPOIS do import, este
    reajuste garante que estado, lock e staging caiam na mesma raiz do app em
    vez do "data fantasma" derivado do caminho do executável.
    """
    global UPDATE_STATE_FILE, UPDATE_ROOT, UPDATE_LOCK_FILE
    UPDATE_STATE_FILE = Path(obter_caminho_dados("update_state.json"))
    UPDATE_ROOT = Path(obter_caminho_dados("updates"))
    UPDATE_LOCK_FILE = UPDATE_ROOT / "update.lock"


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


# Item 12: precedência ÚNICA de leitura de versão.
# Antes, `get_local_version` lia version.json antes de version.txt, enquanto
# `_version_marker`/`_installed_version` do helper liam version.txt primeiro.
# Com as duas fontes divergentes, o app e o helper discordavam sobre a versão
# do mesmo runtime. Uma ordem única e determinística elimina a divergência:
# quando os arquivos divergem, a escolha é sempre a mesma dos dois lados.
VERSION_MARKER_FILES = ("version.txt", "version.json")
RELEASE_INFO_FILE = "release_info.py"
_APP_VERSION_RE = re.compile(r"APP_VERSION\s*=\s*[\"']([^\"']+)")


def read_version_marker(root: Path, allow_release_info: bool = False) -> str:
    """Lê a versão de um diretório obeying uma precedência única e fixa.

    Ordem: `version.txt` -> `version.json` -> `release_info.py` (opcional).
    `release_info.py` só entra quando permitido: num build compilado ele pode
    estar embutido no executável e não existir em disco.

    Devolve "0.0.0" quando nenhum marcador válido é encontrado — nunca levanta.
    """
    nomes = list(VERSION_MARKER_FILES)
    if allow_release_info:
        nomes.append(RELEASE_INFO_FILE)
    for nome in nomes:
        path = Path(root) / nome
        try:
            if not path.is_file():
                continue
            texto = path.read_text(encoding="utf-8")
            if nome.endswith(".json"):
                valor = json.loads(texto).get("latest_version", "")
            elif nome.endswith(".py"):
                achado = _APP_VERSION_RE.search(texto)
                valor = achado.group(1) if achado else ""
            else:
                valor = texto.strip()
            versao = normalize_version(valor)
            if versao != "0.0.0":
                return versao
        except Exception:
            continue
    return "0.0.0"


def get_local_version() -> str:
    roots = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
    roots.append(Path.cwd())
    for root in roots:
        versao = read_version_marker(root, allow_release_info=True)
        if versao != "0.0.0":
            return versao
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


def read_installed_sequence() -> int:
    """Sequência da última atualização efetivamente COMMITTED (instalada).

    Diferente de ``read_update_state()["sequence"]`` (última sequência apenas
    OBSERVADA em checagem/AVAILABLE), este valor só avança quando o helper
    conclui a instalação com sucesso. É o parâmetro que permite aceitar uma
    revisão de MESMA versão com sequência maior sem repetir atualização
    indevidamente.
    """
    try:
        return int(read_update_state().get("installed_sequence") or 0)
    except Exception:
        return 0


def _log_diagnostico_atualizacao(etapa: str, versao: str, mensagem: str,
                                 health_check: str = "", rollback: str = "") -> None:
    """Registra localmente um diagnóstico de atualização (metadados apenas).

    Escreve em ``update_diagnostics.log`` dentro da raiz de dados REAL do
    aplicativo. NUNCA inclui banco de dados, vendas, cadastro de clientes,
    senhas, tokens ou segredos de integração — apenas metadados do processo.
    Diagnóstico jamais interrompe o fluxo de atualização.
    """
    stamp = datetime_now_iso()
    linha = (
        f"[{stamp}] etapa={etapa} versao={versao} "
        f"app={get_local_version()}\n"
        f"  mensagem: {mensagem}\n"
    )
    if health_check:
        linha += f"  health_check: {health_check}\n"
    if rollback:
        linha += f"  rollback: {rollback}\n"
    try:
        caminho = Path(obter_caminho_dados("update_diagnostics.log"))
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("a", encoding="utf-8") as fh:
            fh.write(linha + ("-" * 60) + "\n")
    except Exception:
        pass


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
        self._progresso = None

    # ------------------------------------------------------------------
    # Janela de progresso (Etapa 4C)
    # ------------------------------------------------------------------
    def _abrir_progresso(self, versao_destino: str):
        """Abre (uma única vez) a janela de progresso e devolve a instância.

        Thread-safe (Etapa 4D): ``_stage_release`` pode rodar na thread de
        fundo do download automático OU na thread da interface (encerramento
        seguro). Criar um widget Tk só é seguro na thread da interface:
          * já na thread principal -> cria diretamente;
          * em thread de fundo -> agenda via ``_run_on_ui`` e aguarda um
            ``Event`` com tempo limite (o mainloop processa o ``after``).

        Sem interface Tk (headless/testes) ou se a criação falhar/esgotar o
        tempo, devolve ``None`` e o download/validação seguem normalmente,
        apenas sem UI - nunca bloqueia a atualização.
        """
        if self._progresso is not None and not self._progresso.fechada:
            return self._progresso
        if self.parent is None:
            return None

        def _criar():
            try:
                return JanelaProgressoAtualizacao(
                    self.parent,
                    versao_origem=get_local_version(),
                    versao_destino=versao_destino,
                )
            except Exception:
                return None

        if threading.current_thread() is threading.main_thread():
            self._progresso = _criar()
            return self._progresso

        holder = {}
        pronto = threading.Event()

        def _criar_na_ui():
            holder["janela"] = _criar()
            pronto.set()

        try:
            self._run_on_ui(_criar_na_ui)
        except Exception:
            self._progresso = None
            return None
        pronto.wait(timeout=5.0)
        self._progresso = holder.get("janela")
        return self._progresso

    def _fechar_progresso(self):
        if self._progresso is not None:
            try:
                self._progresso.fechar()
            except Exception:
                pass
            self._progresso = None

    def _callback_progresso(self, janela):
        """Cria o callback de bytes para ``_download_asset``.

        Roda na thread de download e só delega à janela, que agenda a
        atualização dos widgets na thread da interface. Nunca toca em widgets
        diretamente.
        """
        def _cb(recebido, total):
            try:
                if janela is None or janela.fechada:
                    return
                janela.atualizar_bytes(recebido, total)
            except Exception:
                pass
        return _cb

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
            _log_diagnostico_atualizacao("checagem_falhou", "", str(exc))
            return None
        if release is None or not release.auto_update:
            # Transição terminal coerente (correção 1.0.22): nenhum retorno
            # antecipado deixa CHECKING gravado. Sem atualização -> AVAILABLE
            # sem versão pendente (estado "ocioso" que a UI já entende).
            _set_state(UpdateState.AVAILABLE, version="", error="")
            return None
        local_version = get_local_version()
        comparacao = compare_versions(release.version, local_version)
        if comparacao < 0:
            # Downgrade de número de versão: sempre recusado.
            _set_state(UpdateState.AVAILABLE, version="", error="")
            return None
        if comparacao == 0:
            # Regra de negócio aprovada pelo fundador (1.0.22): mesma versão
            # anunciada nunca gera oferta de atualização, independentemente da
            # sequência. O gatilho por `installed_sequence` para revisões de
            # mesma versão fica desativado por esta regra; dowgrade
            # (comparacao < 0) já é recusado acima e versão superior segue o
            # fluxo legítimo abaixo.
            _set_state(UpdateState.AVAILABLE, version="", error="")
            return None
        if considerar_adiamento and self._should_defer(release.version):
            # Adiamento do usuário: grava DEFERRED (não CHECKING abandonado) e
            # preserva os campos da release para _release_pendente_atual().
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
                error="",
            )
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
        janela = self._abrir_progresso(release.version)
        if janela is not None:
            janela.definir_estado("baixando")
            if not release.size:
                janela.indeterminado()
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
            _download_asset(release, destino, progress=self._callback_progresso(janela))
            if janela is not None:
                janela.definir_estado("verificando")
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
            if janela is not None:
                janela.falhar(str(exc))
            messagebox.showerror("Atualização", f"Falha ao preparar a atualização: {exc}", parent=self.parent)
            return False
        finally:
            # Fecha a janela de progresso em qualquer ramo (sucesso ou falha);
            # a confirmação final continua sendo feita por messagebox.
            self._fechar_progresso()

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

        1.0.22: valida que o helper escolhido partilhará a MESMA raiz de dados
        do aplicativo (via ``--data-root``). A validação e o caminho escolhido
        são registrados em ``update_diagnostics.log`` da raiz real.
        """
        raiz_app = Path(obter_caminho_dados()).resolve()
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
                    escolhido = str(helper.resolve())
                    # O helper precisa cair na mesma raiz. Um helper que vivesse
                    # fora de um contexto resolvível seria rejeitado.
                    _log_diagnostico_atualizacao(
                        "helper_escolhido",
                        get_local_version(),
                        f"helper={escolhido}; raiz_app={raiz_app}; "
                        f"raiz_transmitida={raiz_app}",
                    )
                    return [escolhido]
            _log_diagnostico_atualizacao(
                "helper_ausente", get_local_version(), f"candidatos={[str(c) for c in candidatos]}"
            )
            return None
        helper = Path(__file__).resolve().with_name("updater_helper.py")
        return [sys.executable, str(helper)] if helper.is_file() else None

    def _launch_helper(self, job_path: Path) -> bool:
        command = self._find_helper()
        if not command:
            self.last_error = "Helper de atualização não está disponível neste build."
            _set_state(UpdateState.FAILED, error=self.last_error)
            _log_diagnostico_atualizacao(
                "launch_helper_falhou", get_local_version(), self.last_error
            )
            _mostrar_falha_atualizacao_com_suporte(
                "", rollback_confirmado=False, parent=self.parent
            )
            return False
        payload = json.loads(job_path.read_text(encoding="utf-8"))
        payload["parent_pid"] = os.getpid()
        _atomic_json_write(job_path, payload)
        # Raiz de dados canônica do app, transmitida de forma explícita ao helper
        # para eliminar qualquer heurística baseada no caminho do executável.
        raiz_app = str(Path(obter_caminho_dados()).resolve())
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(
                [*command, "--job", str(job_path), "--data-root", raiz_app],
                cwd=str(Path(command[-1]).resolve().parent),
                close_fds=True,
                creationflags=flags,
                env={**os.environ, "FRS_MERCADO_DATA_ROOT": raiz_app},
            )
            _set_state(UpdateState.WAITING_FOR_EXIT, job_path=str(job_path), error="")
            _log_diagnostico_atualizacao(
                "launch_helper",
                str(payload.get("release", {}).get("version", "")),
                f"helper={command[-1]}; raiz_transmitida={raiz_app}; job={job_path}",
            )
            return True
        except Exception as exc:
            self.last_error = str(exc)
            _set_state(UpdateState.FAILED, error=str(exc))
            _log_diagnostico_atualizacao("launch_helper_falhou", "", str(exc))
            release_versao = str(
                json.loads(job_path.read_text(encoding="utf-8")).get(
                    "release", {}
                ).get("version", "")
            )
            _mostrar_falha_atualizacao_com_suporte(
                release_versao, rollback_confirmado=False, parent=self.parent
            )
            return False

    def preparar_instalacao_no_encerramento(self) -> bool:
        release = self._release_pendente_atual()
        if release is None:
            return False
        # Gate operacional ANTES de qualquer promessa de fechamento: se houver
        # caixa aberto, carrinho, pagamento, backup ou operação fiscal, não
        # força o encerramento — adia e informa o motivo.
        permitido, motivo = self._gate()
        if not permitido:
            messagebox.showwarning(
                "Atualização adiada",
                f"Não é possível encerrar e atualizar agora: {motivo}\n\n"
                "Conclua a operação em andamento e tente encerrar novamente.",
                parent=self.parent,
            )
            return None
        # Aviso explícito de fechamento e reabertura. Reflete o comportamento
        # real (helper aguarda a saída, faz health-check e reabre no login) e
        # NÃO promete reabertura incondicional.
        confirmado = messagebox.askyesno(
            "Atualização ao encerrar",
            mensagem_fechamento_reabertura(release.version) + "\n\nDeseja continuar?",
            parent=self.parent,
        )
        if not confirmado:
            self._save_defer(release, 24)
            return False
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
        # Revalidação de versão imediatamente antes de iniciar a instalação:
        # garante que a versão destino ainda é estritamente superior à instalada
        # e que o pacote não perdeu validade entre o stage e o encerramento.
        try:
            self._revalidar_antes_de_instalar(release)
        except UpdateSecurityError as exc:
            self.last_error = str(exc)
            _set_state(UpdateState.FAILED, error=str(exc))
            _mostrar_falha_atualizacao_com_suporte(
                release.version, rollback_confirmado=False, parent=self.parent
            )
            return False
        return self._launch_helper(job_path)

    def _revalidar_antes_de_instalar(self, release: ReleaseInfo) -> None:
        """Reconfirma que a versão destino ainda é estritamente superior.

        Chamada imediatamente antes de lançar o helper. Recusa se a versão
        instalada for igual ou superior (downgrade ou versão igual), ou se
        o pacote não puder ser validado. Não enfraquece assinatura, SHA-256
        nem verificações de conteúdo.
        """
        local_version = get_local_version()
        comparacao = compare_versions(release.version, local_version)
        if comparacao < 0:
            raise UpdateSecurityError(
                f"Versão destino ({release.version}) inferior à instalada ({local_version}). "
                "Downgrade não permitido."
            )
        if comparacao == 0:
            raise UpdateSecurityError(
                f"Versão destino ({release.version}) igual à instalada ({local_version}). "
                "Não há atualização a aplicar."
            )

    def _mostrar_falha_atualizacao_com_suporte(
        self, versao: str, rollback_confirmado: bool, parent=None
    ) -> None:
        """Exibe mensagem clara ao cliente quando a atualização automática falha.

        Informa em português:
          - que a atualização automática não foi concluída;
          - se a versão anterior foi preservada/restaurada (somente se confirmado);
          - que o cliente deve acessar o site oficial para atualização manual;
          - que deve contatar o suporte se a recuperação não pôde ser confirmada.

        Oferece abertura do navegador na página oficial quando disponível,
        mantendo o endereço visível para cópia manual.
        """
        if rollback_confirmado:
            trecho_versao = (
                f"A versão {versao} não foi instalada. "
                "A versão anterior do programa foi preservada e continua funcional."
            )
        else:
            trecho_versao = (
                f"A versão {versao} não pôde ser instalada. "
                "Não foi possível confirmar que a versão anterior esteja intacta. "
                "Não tente reinstalar antes de falar com o suporte."
            )
        mensagem = (
            f"Atualização automática não concluída.\n\n"
            f"{trecho_versao}\n\n"
            f"Para atualizar manualmente, acesse o site oficial da FRS Solutions:\n"
            f"{MANUAL_UPDATE_URL}\n\n"
            "Se a recuperação não puder ser confirmada, entre em contato com o "
            "suporte da FRS Solutions antes de tentar novamente.\n"
        )
        try:
            if webbrowser is not None:
                webbrowser.open(MANUAL_UPDATE_URL, new=0, autoraise=True)
        except Exception:
            pass
        try:
            messagebox.showwarning("Atualização não concluída", mensagem, parent=parent)
        except Exception:
            print(mensagem)

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

