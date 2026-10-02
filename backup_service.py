"""BLOCO 4.5 / L5 — Orquestrador do backup (pacote + Drive + retencao).

Ordem OBRIGORIA (nunca invertida):
    gerar -> validar localmente -> enviar -> confirmar no Drive -> reter
Se qualquer etapa ate a confirmacao falhar, a retencao NAO roda e nenhum
backup antigo e removido.
"""

from __future__ import annotations

from pathlib import Path

import backup_contract as contrato
import backup_drive as drive

TIPO_FECHAMENTO = "fechamento_caixa"
TIPO_OCIOSIDADE = "ociosidade"


def _app_version() -> str:
    try:
        from release_info import APP_VERSION

        return str(APP_VERSION)
    except Exception:
        return "desconhecida"


def _mercado_id() -> str:
    from market_identity import ensure_local_market_id

    return str(ensure_local_market_id() or "").strip()


def _caminho_config():
    from app_paths import obter_caminho_dados

    return Path(obter_caminho_dados("config.json"))


def _pasta_staging():
    from app_paths import obter_caminho_dados

    return Path(obter_caminho_dados("backups"))


def executar_backup(*, tipo_backup, origem=None, service=None, ao_progredir=None):
    """Ciclo completo. Levanta em falha; nunca apaga backup antigo."""
    from database_manager import get_db_path

    db_path = Path(get_db_path())
    market_id = _mercado_id()
    if not market_id:
        raise ValueError("market_id ausente: backup nao pode ser enderecado.")

    pasta = _pasta_staging()
    pasta.mkdir(parents=True, exist_ok=True)

    if ao_progredir:
        ao_progredir("gerando pacote")
    pacote = contrato.montar_pacote(
        db_path=db_path,
        config_path=_caminho_config(),
        pasta_destino=pasta,
        market_id=market_id,
        app_version=_app_version(),
        tipo_backup=tipo_backup,
        origem=origem or {"modulo": "desktop"},
    )

    if ao_progredir:
        ao_progredir("validando pacote")
    ok, problemas, _info = contrato.validar_pacote(pacote["zip_path"], market_id)
    if not ok:
        raise RuntimeError("Pacote invalido: " + "; ".join(problemas))

    if ao_progredir:
        ao_progredir("enviando ao Drive")
    servico = service or drive.obter_service()
    pasta_backups = drive.garantir_pasta_backups(servico, market_id)
    arquivo_id = drive.enviar_pacote(
        servico, pacote["zip_path"], pasta_backups, pacote["backup_id"],
        market_id, pacote["manifest_sha256"],
    )

    if ao_progredir:
        ao_progredir("confirmando no Drive")
    drive.confirmar_pacote(
        servico, arquivo_id, pacote["zip_path"], pacote["backup_id"],
        market_id, pacote["manifest_sha256"], pasta_backups,
    )

    # So agora, com o backup novo confirmado, a retencao pode rodar.
    if ao_progredir:
        ao_progredir("aplicando retencao")
    retencao = drive.aplicar_retencao(
        servico, pasta_backups,
        backup_atual_id=pacote["backup_id"],
        ao_falhar=lambda m: drive.registrar(None, "Backup Drive", "Aviso", m),
    )
    drive.registrar(
        None, "Backup Drive", "Sucesso",
        f"Backup {pacote['backup_id']} ({tipo_backup}) confirmado no Drive.",
    )
    return {
        "backup_id": pacote["backup_id"],
        "market_id": market_id,
        "zip_path": pacote["zip_path"],
        "file_id": arquivo_id,
        "tipo_backup": tipo_backup,
        "retencao": retencao,
    }


def backup_de_fechamento(*, caixa_id=None, service=None, usar_lock=True):
    """Backup de fechamento: TENTATIVA diaria garantida, nunca bloqueante."""
    origem = {"modulo": "desktop", "evento": "fechamento_caixa", "caixa_id": caixa_id}
    if not usar_lock:
        return executar_backup(tipo_backup=TIPO_FECHAMENTO, origem=origem, service=service)
    with drive.TravaDeBackup(usar_arquivo=True):
        return executar_backup(tipo_backup=TIPO_FECHAMENTO, origem=origem, service=service)


def backup_por_ociosidade(*, service=None, janela_min=drive.JANELA_SKIP_OCIOSIDADE_MIN):
    """Complementar: pula se ja houve backup deste mercado na janela."""
    try:
        servico = service or drive.obter_service()
        market_id = _mercado_id()
        if not market_id:
            return None
        pasta_backups = drive.garantir_pasta_backups(servico, market_id)
        existentes = drive.listar_backups(servico, pasta_backups)
        if drive.backup_recem_existe(existentes, janela_min=janela_min):
            drive.registrar(
                None, "Backup Drive", "Aviso",
                "Backup por ociosidade ignorado: ja existe backup recente.",
            )
            return None
    except Exception as exc:
        drive.registrar(None, "Backup Drive", "Aviso", f"Pre-check ocioso falhou: {exc}")
        return None

    with drive.TravaDeBackup(usar_arquivo=True):
        return executar_backup(
            tipo_backup=TIPO_OCIOSIDADE,
            origem={"modulo": "desktop", "evento": "ociosidade"},
            service=service,
        )


def agendar_backup_de_fechamento(caixa_id=None):
    """Dispara o backup de fechamento em background.

    O fechamento local ja foi gravado no banco; uma falha aqui NUNCA pode
    impedir o fechamento nem bloquear a operacao do operador.
    """
    import threading

    def _rodar():
        try:
            backup_de_fechamento(caixa_id=caixa_id)
        except Exception:
            pass  # ja registrado por executar_backup

    thread = threading.Thread(target=_rodar, name="backup-fechamento", daemon=True)
    thread.start()
    return thread


def caixa_esta_aberto(conn) -> bool:
    """Gate: nao ha backup de fechamento com caixa aberto."""
    try:
        linha = conn.execute(
            "SELECT 1 FROM caixa_operacao WHERE status = 'ABERTO' LIMIT 1"
        ).fetchone()
        return bool(linha)
    except Exception:
        return False
