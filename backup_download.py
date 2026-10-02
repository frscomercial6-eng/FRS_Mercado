"""BLOCO 4.6 / L6 — Download de backup do Google Drive.

DOWNLOAD != RESTORE.
Este modulo apenas BAIXA o ZIP para uma pasta escolhida pelo usuario.
NAO escreve mercado.db, NAO escreve config.json, NAO substitui sidecars.
Restore permanece exclusivamente no L7.

Seguranca:
- a listagem e sempre feita dentro de FRS Mercado/<market_id>/backups;
- nunca se busca por nome global;
- so e aceito arquivo com nome <backup_id>.zip cujo backup_id pertence ao
  market_id atual e com appProperties coerentes;
- o conteudo e validado pelo contrato L5 antes de ser entregues ao usuario.
"""

from __future__ import annotations

from pathlib import Path

import backup_contract as contrato


class ErroDownload(RuntimeError):
    """Falha ao baixar um backup do Drive."""


def _mercado_id() -> str:
    from market_identity import ensure_local_market_id

    market_id = str(ensure_local_market_id() or "").strip()
    if not market_id:
        raise ErroDownload("market_id ausente: nao e possivel enderecar o backup.")
    return market_id


def _obter_service():
    import backup_drive as drive

    return drive.obter_service()


def _pasta_backups(service, market_id):
    import backup_drive as drive

    return drive.garantir_pasta_backups(service, market_id)


def _listar_com_metadados(service, pasta_backups_id):
    """Lista os backups do proprio mercado lendo os appProperties do L5.

    backup_drive.listar_backups (L5) nao expoe appProperties, entao a checagem
    de formato/manifest_sha256 seria cega. O L5 nao e alterado.
    """
    resposta = service.files().list(
        q=f"'{pasta_backups_id}' in parents and trashed = false",
        spaces="drive",
        fields="files(id,name,size,appProperties,parents)",
    ).execute()
    itens = []
    for arquivo in resposta.get("files", []):
        nome = str(arquivo.get("name") or "")
        if not nome.endswith(".zip"):
            continue
        backup_id = nome[:-4]
        props = arquivo.get("appProperties") or {}
        itens.append({
            "id": arquivo.get("id"),
            "name": nome,
            "backup_id": backup_id,
            "timestamp": contrato.backup_id_timestamp(backup_id),
            "formato": str(props.get("backup_format_version") or ""),
            "manifest_sha256": str(props.get("manifest_sha256") or ""),
            "tipo": str(props.get("tipo_backup") or ""),
            "parents": arquivo.get("parents") or [],
        })
    itens.sort(key=lambda x: (x["timestamp"], x["name"]), reverse=True)
    return itens


def listar_backups_mercado(service=None):
    """Backups do MESMO mercado, mais recentes primeiro.

    Cada item traz data/hora e o tipo (fechamento_caixa / ociosidade) lido dos
    metadados do Drive.
    """
    servico = service or _obter_service()
    market_id = _mercado_id()
    pasta = _pasta_backups(servico, market_id)

    saida = []
    for item in _listar_com_metadados(servico, pasta):
        if contrato.backup_id_mercado(item["backup_id"]) != market_id:
            continue  # defesa: nunca expor backup de outro mercado
        saida.append({
            "id": item["id"],
            "backup_id": item["backup_id"],
            "nome": item["name"],
            "timestamp": item["timestamp"],
            "data_hora": _data_legivel(item["timestamp"]),
            "tipo": item["tipo"] or "desconhecido",
            "manifest_sha256": item["manifest_sha256"],
            "formato": item["formato"],
        })
    return saida


def _data_legivel(ts: str) -> str:
    from datetime import datetime, timezone

    try:
        momento = datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
    except (ValueError, TypeError):
        return str(ts or "")
    return momento.strftime("%d/%m/%Y %H:%M:%S")


def _item_valido(item, market_id) -> tuple[bool, str]:
    backup_id = str(item.get("backup_id") or "")
    if not backup_id:
        return False, "item sem backup_id"
    if contrato.backup_id_mercado(backup_id) != market_id:
        return False, f"backup de outro mercado ({backup_id})"
    formato = str(item.get("formato") or "")
    if formato and formato != str(contrato.BACKUP_FORMAT_VERSION):
        return False, f"formato incompativel ({formato})"
    return True, ""


def _baixar_bytes(service, item, destino: Path) -> Path:
    import io

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    pedido = service.files().get_media(fileId=item["id"])
    dados = pedido.execute() if hasattr(pedido, "execute") else pedido
    destino.write_bytes(dados if isinstance(dados, (bytes, bytearray))
                        else io.BytesIO(dados).read())
    return destino


def baixar_backup(backup_id: str, pasta_destino, service=None):
    """Baixa UM backup do mercado atual e valida o pacote. Nunca restaura."""
    import backup_drive as drive

    servico = service or drive.obter_service()
    market_id = _mercado_id()

    candidatos = [b for b in listar_backups_mercado(servico)
                  if b["backup_id"] == str(backup_id)]
    if not candidatos:
        raise ErroDownload(f"Backup {backup_id} nao encontrado para este mercado.")
    item = candidatos[0]

    ok, motivo = _item_valido(item, market_id)
    if not ok:
        raise ErroDownload(f"Backup recusado: {motivo}")

    destino = Path(pasta_destino)
    if destino.is_dir():
        destino = destino / item["nome"]
    _baixar_bytes(servico, item, destino)

    valido, problemas, _info = contrato.validar_pacote(destino, market_id)
    if not valido:
        destino.unlink(missing_ok=True)
        raise ErroDownload("Pacote invalido: " + "; ".join(problemas))

    return {"caminho": destino, "backup_id": backup_id, "market_id": market_id}


def baixar_ultimo_backup(pasta_destino, service=None):
    """Baixa o backup VALIDADO mais recente deste mercado. Nunca restaura."""
    disponiveis = listar_backups_mercado(service)
    if not disponiveis:
        raise ErroDownload("Nenhum backup encontrado para este mercado.")
    return baixar_backup(disponiveis[0]["backup_id"], pasta_destino, service=service)
