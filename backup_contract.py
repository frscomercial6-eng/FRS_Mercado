"""BLOCO 4.5 / L5 — Contrato do pacote de backup (ZIP + manifesto canonico).

Modulo puro: nao conhece interface, nao conhece Google Drive e nao faz rede.
O backup real e formado por banco + configuracao + manifesto/hashes.
O relatorio .xlsx e apenas representacao para leitura humana.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BACKUP_FORMAT_VERSION = 1
PASTA_INTERNA = "frs-backup-v1"

ARQUIVO_DB = "mercado.db"
ARQUIVO_CONFIG = "config.json"
ARQUIVO_MANIFEST = "manifest.json"
ARQUIVO_RELATORIO = "Relatorio_Fechamento.xlsx"

# Allowlist explicita: nada fora daqui entra no ZIP.
ALLOWLIST = (ARQUIVO_DB, ARQUIVO_CONFIG, ARQUIVO_MANIFEST, ARQUIVO_RELATORIO)
OBRIGATORIOS = (ARQUIVO_DB, ARQUIVO_CONFIG, ARQUIVO_MANIFEST)

# Denylist como segunda barreira (nao substitui o allowlist).
DENYLIST_PARTES = (
    "token.pickle", "credentials.json", "client_credentials.sec.json",
    "google-services.json", "firebase-admin-key.json", "firebase-adminsdk",
    ".pem", ".key", "-wal", "-shm", "-journal", "adocao",
)

MIME = {
    ".zip": "application/zip",
    ".db": "application/vnd.sqlite3",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

TABELAS_ESSENCIAIS = (
    "produtos", "vendas", "itens_venda", "caixa_operacao",
    "clientes", "usuarios", "vales",
)


def canonical_bytes(obj) -> bytes:
    """Serializacao estavel: mesmo conteudo logico => mesmos bytes."""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def sha256_arquivo(caminho) -> str:
    digest = hashlib.sha256()
    with Path(caminho).open("rb") as handle:
        for bloco in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def mime_de(nome: str) -> str:
    return MIME.get(Path(nome).suffix.lower(), "application/octet-stream")


def nome_permitido(nome: str) -> bool:
    """So entra no ZIP o que estiver no allowlist e fora do denylist."""
    base = Path(nome).name
    if base not in ALLOWLIST:
        return False
    baixo = base.lower()
    return not any(parte in baixo for parte in DENYLIST_PARTES)


_PADRAO_BACKUP_ID = re.compile(
    r"^(?P<market_id>[A-Za-z0-9._-]+)_"
    r"(?P<ts>\d{8}T\d{6}Z)_"
    r"(?P<rand>[0-9a-f]{8})$"
)


def gerar_backup_id(market_id: str, quando: datetime | None = None,
                    aleatorio: str | None = None) -> str:
    momento = quando or datetime.now(timezone.utc)
    ts = momento.strftime("%Y%m%dT%H%M%SZ")
    sufixo = (aleatorio or secrets.token_hex(4)).lower()
    return f"{market_id}_{ts}_{sufixo}"


def backup_id_timestamp(backup_id: str) -> str:
    m = _PADRAO_BACKUP_ID.match(str(backup_id or ""))
    return m.group("ts") if m else ""


def backup_id_mercado(backup_id: str) -> str:
    m = _PADRAO_BACKUP_ID.match(str(backup_id or ""))
    return m.group("market_id") if m else ""


def construir_manifesto(*, backup_id, market_id, app_version, tipo_backup,
                        origem, arquivos):
    """`arquivos` = dicts com logical_name/size/sha256/obrigatorio/restauravel."""
    return {
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "backup_id": backup_id,
        "market_id": market_id,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": app_version,
        "tipo_backup": tipo_backup,
        "origem": dict(origem or {}),
        "arquivos": [
            {
                "logical_name": item["logical_name"],
                "size": int(item["size"]),
                "sha256": item["sha256"],
                "mime_type": item.get("mime_type") or mime_de(item["logical_name"]),
                "obrigatorio": bool(item.get("obrigatorio", True)),
                "restauravel": bool(item.get("restauravel", True)),
            }
            for item in sorted(arquivos, key=lambda x: x["logical_name"])
        ],
    }


def _snapshots(sqlite_path: Path) -> bytes:
    """Bytes do banco via sqlite3.Connection.backup() — sem sidecars."""
    destino = sqlite_path.with_name(sqlite_path.name + ".snap")
    destino.parent.mkdir(parents=True, exist_ok=True)
    origem = sqlite3.connect(str(sqlite_path))
    try:
        alvo = sqlite3.connect(str(destino))
        try:
            origem.backup(alvo)
        finally:
            alvo.close()
    finally:
        origem.close()
    dados = destino.read_bytes()
    destino.unlink(missing_ok=True)
    for sufixo in ("-wal", "-shm", "-journal"):
        Path(str(destino) + sufixo).unlink(missing_ok=True)
    return dados


def _entrada(nome: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(f"{PASTA_INTERNA}/{nome}", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def montar_pacote(*, db_path, config_path, pasta_destino, market_id,
                  app_version, tipo_backup, origem, relatorio_xlsx=None,
                  quando=None, aleatorio=None):
    """Monta <backup_id>.zip e devolve seus dados. Nao envia nada ao Drive."""
    db_path = Path(db_path)
    config_path = Path(config_path)
    pasta_destino = Path(pasta_destino)
    if not nome_permitido(ARQUIVO_DB) or not nome_permitido(ARQUIVO_CONFIG):
        raise ValueError("allowlist de backup invalida.")

    db_bytes = _snapshots(db_path)
    config_bytes = config_path.read_bytes()
    json.loads(config_bytes.decode("utf-8"))  # recusa config invalido cedo

    conteudo = {ARQUIVO_DB: db_bytes, ARQUIVO_CONFIG: config_bytes}
    if relatorio_xlsx and Path(relatorio_xlsx).exists():
        conteudo[ARQUIVO_RELATORIO] = Path(relatorio_xlsx).read_bytes()

    backup_id = gerar_backup_id(market_id, quando=quando, aleatorio=aleatorio)
    arquivos = [
        {"logical_name": nome, "size": len(dados),
         "sha256": sha256_bytes(dados), "mime_type": mime_de(nome),
         "obrigatorio": nome in OBRIGATORIOS,
         "restauravel": nome != ARQUIVO_RELATORIO}
        for nome, dados in sorted(conteudo.items())
    ]
    manifesto = construir_manifesto(
        backup_id=backup_id, market_id=market_id, app_version=app_version,
        tipo_backup=tipo_backup, origem=origem, arquivos=arquivos,
    )
    manifesto_bytes = canonical_bytes(manifesto)

    pasta_destino.mkdir(parents=True, exist_ok=True)
    zip_path = pasta_destino / f"{backup_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as pacote:
        for nome, dados in sorted(conteudo.items()):
            pacote.writestr(_entrada(nome), dados)
        pacote.writestr(_entrada(ARQUIVO_MANIFEST), manifesto_bytes)

    return {
        "backup_id": backup_id,
        "zip_path": zip_path,
        "manifest": manifesto,
        "manifest_bytes": manifesto_bytes,
        "manifest_sha256": sha256_bytes(manifesto_bytes),
        "zip_sha256": sha256_arquivo(zip_path),
        "zip_size": zip_path.stat().st_size,
        "market_id": market_id,
        "tipo_backup": tipo_backup,
    }


def validar_pacote(zip_path, market_id_esperado=None) -> tuple[bool, list[str], dict]:
    """Retorna (ok, problemas, informacoes). Nunca levanta excecao."""
    problemas = []
    info = {}
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return False, ["arquivo de pacote inexistente"], info

    try:
        with zipfile.ZipFile(zip_path) as pacote:
            corrompido = pacote.testzip()
            if corrompido is not None:
                return False, [f"ZIP corrompido em {corrompido}"], info
            chave_manifesto = f"{PASTA_INTERNA}/{ARQUIVO_MANIFEST}"
            if chave_manifesto not in pacote.namelist():
                return False, ["manifest.json ausente"], info
            try:
                manifesto = json.loads(
                    pacote.read(chave_manifesto).decode("utf-8")
                )
            except Exception as exc:
                return False, [f"manifesto corrompido: {exc}"], info
            nomes = set(pacote.namelist())
    except zipfile.BadZipFile:
        return False, ["ZIP invalido/corrompido"], info
    except Exception as exc:
        return False, [f"ZIP ilegivel: {exc}"], info

    if not isinstance(manifesto, dict):
        return False, ["manifesto nao e objeto JSON"], info

    info["manifest"] = manifesto
    info["backup_id"] = str(manifesto.get("backup_id") or "")
    info["market_id"] = str(manifesto.get("market_id") or "")

    formato = manifesto.get("backup_format_version")
    if formato != BACKUP_FORMAT_VERSION:
        problemas.append(
            f"formato {formato!r} incompativel com o esperado {BACKUP_FORMAT_VERSION}"
        )
    if market_id_esperado is not None and info["market_id"] != str(market_id_esperado):
        problemas.append(
            f"market_id divergente: {info['market_id']!r} != {market_id_esperado!r}"
        )

    declarados = manifesto.get("arquivos")
    if not isinstance(declarados, list) or not declarados:
        return False, problemas + ["manifesto sem lista de arquivos"], info

    with zipfile.ZipFile(zip_path) as pacote:
        vistos = set()
        for item in declarados:
            nome = str(item.get("logical_name") or "")
            if not nome_permitido(nome):
                problemas.append(f"arquivo nao permitido no pacote: {nome}")
                continue
            chave = f"{PASTA_INTERNA}/{nome}"
            vistos.add(chave)
            if chave not in nomes:
                problemas.append(f"arquivo declarado ausente: {nome}")
                continue
            dados = pacote.read(chave)
            if len(dados) != int(item.get("size") or -1):
                problemas.append(f"tamanho divergente em {nome}")
            if sha256_bytes(dados) != str(item.get("sha256") or ""):
                problemas.append(f"sha256 divergente em {nome}")

        for chave in sorted(nomes):
            if chave not in vistos and chave != f"{PASTA_INTERNA}/{ARQUIVO_MANIFEST}":
                problemas.append(f"arquivo inesperado no pacote: {chave}")

        for exigido in OBRIGATORIOS:
            if f"{PASTA_INTERNA}/{exigido}" not in nomes:
                problemas.append(f"obrigatorio ausente: {exigido}")

        chave_db = f"{PASTA_INTERNA}/{ARQUIVO_DB}"
        if chave_db in nomes:
            bruto = pacote.read(chave_db)
            if not bruto.startswith(b"SQLite format 3\x00"):
                problemas.append("mercado.db nao e um SQLite")
            else:
                problemas.extend(_validar_sqlite_em_memoria(bruto))

        chave_cfg = f"{PASTA_INTERNA}/{ARQUIVO_CONFIG}"
        if chave_cfg in nomes:
            try:
                json.loads(pacote.read(chave_cfg).decode("utf-8"))
            except Exception as exc:
                problemas.append(f"config.json invalido: {exc}")

    info["ok"] = not problemas
    return (not problemas), problemas, info


def _validar_sqlite_em_memoria(bruto: bytes) -> list[str]:
    import tempfile

    problemas = []
    with tempfile.TemporaryDirectory(prefix="frs-l5-check-") as tmp:
        destino = Path(tmp) / "check.db"
        destino.write_bytes(bruto)
        try:
            conn = sqlite3.connect(str(destino))
        except Exception as exc:
            return [f"mercado.db nao abre: {exc}"]
        try:
            integridade = conn.execute("PRAGMA quick_check").fetchone()
            if not integridade or str(integridade[0]).lower() != "ok":
                problemas.append(f"mercado.db quick_check falhou: {integridade}")
            existentes = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for tabela in TABELAS_ESSENCIAIS:
                if tabela not in existentes:
                    problemas.append(f"tabela essencial ausente: {tabela}")
        except Exception as exc:
            problemas.append(f"mercado.db ilegivel: {exc}")
        finally:
            conn.close()
    return problemas
