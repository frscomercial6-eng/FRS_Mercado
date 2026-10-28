"""BLOCO 4.6 / L6 — Conexao e status real do Google Drive.

Regras:
- o Gmail digitado e APENAS a intencao do usuario;
- a conta exibida e SEMPRE a conta efetivamente autorizada pelo Google;
- o status so fica verde quando o token e realmente utilizavel;
- nenhuma senha ou credencial Google e persistida (apenas o e-mail da conta).
"""

from __future__ import annotations

import pickle
import re
from pathlib import Path

CHAVE_CONTA = "google_drive_conta"

_GMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

SITUACAO_SEM_CREDENCIAIS = "sem_credenciais"
SITUACAO_SEM_TOKEN = "sem_token"
SITUACAO_TOKEN_INVALIDO = "token_invalido"
SITUACAO_RENOVAVEL = "token_renovavel"
SITUACAO_TOKEN_REVOGADO = "token_revogado"
SITUACAO_CONECTADO = "conectado"


def validar_gmail(email) -> tuple[bool, str]:
    """Valida formato basico de e-mail. Nao verifica existencia na conta Google."""
    texto = str(email or "").strip()
    if not texto:
        return False, "Informe o Gmail para fazer backups."
    if len(texto) > 254 or not _GMAIL.match(texto):
        return False, f"'{texto}' nao parece um Gmail valido."
    return True, ""


def _caminhos():
    from app_paths import obter_caminho_dados

    return (
        Path(obter_caminho_dados("token.pickle")),
        obter_caminho_dados("config.json"),
    )


def conta_persistida() -> str:
    """Conta Google efetivamente autorizada, registrada na conexão anterior."""
    try:
        from modulo_config import carregar_configuracoes

        return str(carregar_configuracoes().get(CHAVE_CONTA) or "").strip()
    except Exception:
        return ""


def registrar_conta(email):
    """Persiste somente o e-mail da conta autorizada (nunca senha/credencial)."""
    from modulo_config import carregar_configuracoes, salvar_configuracoes

    dados = dict(carregar_configuracoes() or {})
    dados[CHAVE_CONTA] = str(email or "").strip()
    ok, _msg = salvar_configuracoes(dados, exibir_alerta=False)
    return ok


def limpar_conta():
    from modulo_config import carregar_configuracoes, salvar_configuracoes

    dados = dict(carregar_configuracoes() or {})
    dados[CHAVE_CONTA] = ""
    salvar_configuracoes(dados, exibir_alerta=False)


def _carregar_token(token_path):
    if not Path(token_path).is_file():
        return None, SITUACAO_SEM_TOKEN
    try:
        with open(token_path, "rb") as handle:
            return pickle.load(handle), None
    except Exception:
        return None, SITUACAO_TOKEN_INVALIDO


def status_conexao() -> dict:
    """Status HONESTO, calculado localmente (sem rede).

    Nunca retorna 'conectado' quando o token nao permite acessar o Drive.
    """
    conta = conta_persistida()
    base = {"conta": conta, "situacao": SITUACAO_SEM_TOKEN, "conectado": False}

    try:
        from database_manager import GOOGLE_CREDS

        credenciais_app = str((GOOGLE_CREDS or {}).get("credentials") or "")
    except Exception:
        credenciais_app = ""
    if not credenciais_app:
        return {
            **base, "situacao": SITUACAO_SEM_CREDENCIAIS,
            "mensagem": "🔴 Google Drive nao conectado (credenciais ausentes).",
            "cor": "#ff6666",
        }

    token_path, _config = _caminhos()
    token, falha = _carregar_token(token_path)
    if token is None:
        rotulo = {
            SITUACAO_SEM_TOKEN: "🔴 Google Drive nao conectado.",
            SITUACAO_TOKEN_INVALIDO: "🔴 Google Drive nao conectado (token invalido).",
        }[falha]
        return {**base, "situacao": falha, "mensagem": rotulo, "cor": "#ff6666"}

    valido = bool(getattr(token, "valid", False))
    expirado = bool(getattr(token, "expired", False))
    renovavel = bool(getattr(token, "refresh_token", None))

    if valido:
        return {
            "situacao": SITUACAO_CONECTADO, "conectado": True, "conta": conta,
            "mensagem": (
                f"🟢 Google Drive conectado\nConta: {conta}" if conta
                else "🟢 Google Drive conectado"
            ),
            "cor": "#2ecc71",
        }
    if expirado and renovavel:
        # Renovavel NAO e o mesmo que conectado: ainda exige acionar o Drive.
        return {
            **base, "situacao": SITUACAO_RENOVAVEL,
            "mensagem": "🟡 Autorizacao expirada — conecte novamente para renovar.",
            "cor": "#f1c40f",
        }
    return {
        **base, "situacao": SITUACAO_TOKEN_REVOGADO,
        "mensagem": "🔴 Autorizacao invalida ou revogada — conecte novamente.",
        "cor": "#ff6666",
    }


class ErroConexaoDrive(RuntimeError):
    """Falha durante a autorização do Google Drive."""


def conta_autorizada(service) -> str:
    """Conta REALMENTE autorizada, lida do proprio Google."""
    try:
        about = service.about().get(fields="user(emailAddress)").execute()
    except Exception as exc:
        raise ErroConexaoDrive(f"nao foi possivel confirmar a conta Google: {exc}") from exc
    email = str((about or {}).get("user", {}).get("emailAddress", "") or "").strip()
    if not email:
        raise ErroConexaoDrive("Google nao retornou a conta autorizada.")
    return email


def conectar(email_informado, service=None, credenciais=None):
    """Autoriza no Google, persiste a conta REAL e garante a arvore do mercado.

    O e-mail informado e validado, mas quem manda e o Google: se as contas
    divergirem isso e reportado explicitamente, nunca mascarado.
    """
    ok, motivo = validar_gmail(email_informado)
    if not ok:
        raise ErroConexaoDrive(motivo)

    import backup_drive as drive

    servico = service or drive.obter_service(credenciais=credenciais)
    conta_real = conta_autorizada(servico)
    informada = str(email_informado or "").strip()

    from market_identity import ensure_local_market_id

    market_id = ensure_local_market_id()
    arvore = drive._modulo_relatorio().ensure_market_drive_root(market_id, service=servico)

    registrar_conta(conta_real)
    drive.registrar(
        None, "Backup Drive", "Sucesso",
        f"Google Drive conectado ({conta_real}); arvore do mercado pronta.",
    )

    divergente = bool(informada) and informada.lower() != conta_real.lower()
    return {
        "conta": conta_real,
        "email_informado": informada,
        "divergente": divergente,
        "market_id": market_id,
        "backups_folder_id": arvore["backups_folder_id"],
    }
