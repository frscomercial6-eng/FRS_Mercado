"""BLOCO 4.5 / L5 — Operacoes de backup no Google Drive do cliente.

Regras inviolaveis:
- destino SEMPRE FRS Mercado/<market_id>/backups (nunca a raiz do Drive);
- upload com 'parents' explicito;
- files().update() nunca e usado;
- files().delete() so na retencao, e nunca no backup recem-criado;
- confirmacao por files().get antes de considerar o backup valido.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import backup_contract as contrato

LOCK_BACKUP = "backup.lock"
RETENCAO_PADRAO = 15
JANELA_SKIP_OCIOSIDADE_MIN = 30

_lock_local = threading.Lock()


def _modulo_relatorio():
    from modulo_relatorio import ModuloRelatorio

    return ModuloRelatorio


def caminho_lock():
    from app_paths import obter_caminho_dados

    return Path(obter_caminho_dados("updates")) / LOCK_BACKUP


class TravaDeBackup:
    """Trava compartilhada entre backup de fechamento e backup por ociosidade."""

    def __init__(self, usar_arquivo=True):
        self.usar_arquivo = usar_arquivo
        self._arquivo = None
        self._local = False

    def __enter__(self):
        from updater_helper import _exclusive_lock

        if self._local or self._arquivo is not None:
            raise RuntimeError("Outro backup ja esta em execucao.")
        if not _lock_local.acquire(blocking=False):
            raise RuntimeError("Outro backup ja esta em execucao.")
        self._local = True
        if self.usar_arquivo:
            try:
                self._arquivo = _exclusive_lock(caminho_lock())
                self._arquivo.__enter__()
            except Exception:
                _lock_local.release()
                self._local = False
                self._arquivo = None
                raise
        return self

    def __exit__(self, *_exc):
        if self._arquivo is not None:
            try:
                self._arquivo.__exit__(None, None, None)
            finally:
                self._arquivo = None
        if self._local:
            _lock_local.release()
            self._local = False
        return False


def garantir_pasta_backups(service, market_id: str) -> str:
    arvore = _modulo_relatorio().ensure_market_drive_root(market_id, service=service)
    return arvore["backups_folder_id"]


def obter_service():
    return _modulo_relatorio()._obter_drive_service()


def registrar(usuario_id, acao, status, detalhes):
    from database_manager import registrar_log

    registrar_log(usuario_id, acao, status, detalhes)


def enviar_pacote(service, zip_path, pasta_backups_id, backup_id, market_id,
                  manifest_sha256, formato=contrato.BACKUP_FORMAT_VERSION):
    """Upload resumable em <backup_id>.zip com appProperties. Devolve o file_id."""
    if not pasta_backups_id:
        raise ValueError("Pasta de backups do Drive nao informada.")
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"Pacote de backup inexistente: {zip_path}")

    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(zip_path), mimetype=contrato.MIME[".zip"], resumable=True)
    corpo = {
        "name": f"{backup_id}.zip",
        "parents": [pasta_backups_id],
        "appProperties": {
            "backup_id": str(backup_id),
            "market_id": str(market_id),
            "manifest_sha256": str(manifest_sha256),
            "backup_format_version": str(formato),
        },
    }
    requisicao = service.files().create(
        body=corpo, media_body=media, fields="id", resumable=True
    )
    arquivo_id = None
    while requisicao is not None:
        status, resposta = requisicao.next_chunk(num_retries=3)
        if resposta:
            arquivo_id = resposta.get("id")
        if status is not None and getattr(status, "finished", False):
            requisicao = None
    if not arquivo_id:
        raise RuntimeError("Upload concluido sem devolver o id do arquivo.")
    return arquivo_id


def confirmar_pacote(service, arquivo_id, zip_path, backup_id, market_id,
                    manifest_sha256, pasta_backups_id):
    """Confirma tamanho, parents e appProperties. Levanta em caso de divergencia."""
    info = service.files().get(
        fileId=arquivo_id, fields="id,name,size,parents,appProperties"
    ).execute()
    tamanho_local = Path(zip_path).stat().st_size
    tamanho_remoto = int(info.get("size") or -1)
    if tamanho_local != tamanho_remoto:
        raise RuntimeError(
            f"Tamanho divergente no Drive: local {tamanho_local} vs remoto {tamanho_remoto}."
        )
    if pasta_backups_id and pasta_backups_id not in (info.get("parents") or []):
        raise RuntimeError("Arquivo enviado fora da pasta de backups do mercado.")
    props = info.get("appProperties") or {}
    if str(props.get("backup_id")) != str(backup_id):
        raise RuntimeError("backup_id do Drive diverge do pacote.")
    if str(props.get("market_id")) != str(market_id):
        raise RuntimeError("market_id do Drive diverge do pacote.")
    if str(props.get("manifest_sha256")) != str(manifest_sha256):
        raise RuntimeError("manifest_sha256 do Drive diverge do pacote.")
    return info


def listar_backups(service, pasta_backups_id):
    """Backups deste mercado, mais novos primeiro."""
    resposta = service.files().list(
        q=f"'{pasta_backups_id}' in parents and trashed = false",
        spaces="drive",
        fields="files(id,name,size,appProperties)",
    ).execute()
    itens = []
    for arquivo in resposta.get("files", []):
        nome = str(arquivo.get("name") or "")
        if not nome.endswith(".zip"):
            continue
        backup_id = nome[:-4]
        itens.append({
            "id": arquivo.get("id"),
            "name": nome,
            "backup_id": backup_id,
            "timestamp": contrato.backup_id_timestamp(backup_id),
        })
    itens.sort(key=lambda x: (x["timestamp"], x["name"]), reverse=True)
    return itens


def aplicar_retencao(service, pasta_backups_id, manter=RETENCAO_PADRAO,
                     backup_atual_id=None, ao_falhar=None):
    """Mantem os `manter` mais recentes. Nunca remove o backup recem-criado.

    Falha ao excluir e registrada e NAO desfaz o backup novo.
    """
    try:
        itens = listar_backups(service, pasta_backups_id)
    except Exception as exc:
        if ao_falhar:
            ao_falhar(f"listagem para retencao falhou: {exc}")
        return {"mantidos": [], "removidos": [], "falhas": [str(exc)]}

    removidos, falhas, mantidos = [], [], []
    for item in itens[manter:]:
        if backup_atual_id and item["backup_id"] == backup_atual_id:
            mantidos.append(item)  # defesa explicita
            continue
        try:
            service.files().delete(fileId=item["id"]).execute()
            removidos.append(item["backup_id"])
        except Exception as exc:
            falhas.append(f"{item['backup_id']}: {exc}")
            if ao_falhar:
                ao_falhar(f"exclusao de {item['backup_id']} falhou: {exc}")
    mantidos.extend(itens[:manter])
    return {"mantidos": mantidos, "removidos": removidos, "falhas": falhas}


def backup_recem_existe(itens, janela_min=JANELA_SKIP_OCIOSIDADE_MIN):
    """Backup deste mercado dentro da janela? (o idle pode ser pulado)"""
    if not itens:
        return None
    limite = datetime.now(timezone.utc) - timedelta(minutes=janela_min)
    for item in itens:
        ts = item.get("timestamp") or ""
        try:
            momento = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if momento >= limite:
            return item
    return None
