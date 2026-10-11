"""Licença do FRS Mercado: Trial de 30 dias, ativação por chave e status.

Fluxo histórico comprovado (v1.0.18) restaurado para o ambiente de campo:

- o login local (usuário/senha) sempre abre, inclusive com licença vencida
  (modo restrito);
- ``COMPRAR LICENÇA`` abre https://www.frssolutions.com.br/planos;
- ``ATIVAR LICENÇA`` / ``ATIVAÇÃO DE SISTEMA`` abrem o campo de digitação da
  chave e validam o código conforme o fluxo histórico;
- o Trial de 30 dias é criado no setup inicial (tabela ``licenca``);
- licença vencida mantém login, exportação/recuperação dos dados, compra e
  ativação, bloqueando apenas as operações comerciais.

Formato da chave do fluxo histórico: ``FRS-AAAAMMDD-XXXXXXXXXXXX``, em que o
bloco final é ``sha256(IDENTIFICADOR|AAAAMMDD|FRS_ATIVACAO_2026)[:12]`` em
maiúsculas. O ``IDENTIFICADOR`` é o ``market_id``, o CNPJ ou a razão social
configurada; ``FRS_ATIVACAO_2026`` é constante pública do fluxo comprovado e
não é o ``SECRET_SALT`` legado, que permanece removido.

A arquitetura Stage 1 permanece preservada: a licença assinada Ed25519 em
``licensing/`` continua sendo verificada com o trust anchor público (chave
pública em ``licensing/trusted_keys.json``) e, quando presente, prevalece como
fonte de verdade. As chaves privadas permanecem exclusivamente em
``FRS_MERCADO_PRIVATE`` e jamais são distribuídas ou usadas no cliente.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from database_manager import get_db_connection
from licensing import LicenseService
from licensing import chave_curta, formats
from licensing.storage import LicenseStorage
from modulo_config import carregar_configuracoes, salvar_configuracoes

RENOVACAO_URL = "https://www.frssolutions.com.br/planos"
TRIAL_DIAS = 30
# Constante pública do fluxo histórico comprovado (não é segredo e não é o
# SECRET_SALT legado).
FRS_ATIVACAO_2026 = "FRS_ATIVACAO_2026"
PREFIXO_LEGADO = "LICENCA_FRS:"
PADRAO_CHAVE_ATIVACAO = re.compile(r"^FRS-(\d{8})-([A-F0-9]{12})$")

# Ruídos de area de transferência que precedem o texto colado na tela de
# ativação. O Notepad, o Excel e o Word gravam UTF-8/UTF-16 com BOM, e a
# Internet/formatadores podem envolver o texto em aspas tipográficas. Sem esta
# normalização o envelope assinado era recusado como "Formato inválido",
# embora a licença estivesse correta.
_PREFIXOS_NAO_LIDOS = ("\ufeff", "\ufffe")
_ASPAS_TIPOGRAFICAS = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
}


def _decodificar_chave_curta(texto: str) -> str:
    """Chave curta de ativação -> envelope JSON assinado (canônico).

    Recodifica no formato EXATO que o `LicenseVerifier` já validava. A
    assinatura NÃO é recalculada nem reescrita aqui: ela veio dentro da
    chave, assinada pela FRS. Só o transporte muda.
    """
    return formats.encode_envelope(chave_curta.ler_chave_ativacao(texto))


def normalizar_texto_ativacao(texto: str) -> str:
    """Remove BOM, aspas tipográficas e espaços em volta do texto colado.

    Não altera o conteúdo do envelope: apenas remove caracteres que não fazem
    parte da licença. A validação continua sendo a Ed25519 oficial; a chave
    legada ``FRS-AAAAMMDD-XXXXXXXXXXXX`` segue apenas como interface
    histórica, encaminhada ao mesmo validador.
    """
    resultado = str(texto or "")
    for _ in range(4):
        anterior = resultado
        for prefixo in _PREFIXOS_NAO_LIDOS:
            if resultado.startswith(prefixo):
                resultado = resultado[len(prefixo):]
        resultado = resultado.strip()
        if resultado == anterior:
            break
    # Aspas tipográficas abraçantes/cnjuntantes viram aspas simples antes de
    # qualquer decisão de formato; um par envolvendo o texto também é removido.
    resultado = resultado.replace("\u201d", '"').replace("\u201c", '"')
    resultado = resultado.replace("\u201f", '"').replace("\u201e", '"')
    resultado = resultado.replace("\u2019", "'").replace("\u2018", "'")
    resultado = resultado.replace("\u201b", "'").replace("\u201a", "'")
    if len(resultado) >= 2 and resultado[0] == '"' and resultado[-1] == '"':
        resultado = resultado[1:-1]
    elif resultado.startswith('"'):
        resultado = resultado[1:]
    return resultado.strip()

# Operações comerciais bloqueadas quando a licença está vencida. Exportação,
# relatórios, usuários, configurações e a própria ativação permanecem liberados
# para permitir recuperação dos dados, compra e ativação da licença.
OPERACOES_COMERCIAIS_BLOQUEADAS = frozenset(
    {
        "PDV",
        "VENDA",
        "VENDAS",
        "ESTOQUE",
        "ENTRADA",
        "ENTRADAS",
        "IMPORTAR PRODUTOS",
        "ENTRADA DE ESTOQUE",
        "ENTRADAS DE ESTOQUE",
        "FINANCEIRO",
        "TAXAS",
        "CAIXA",
        "SANGRIA",
        "SANGRIA PREVENTIVA",
        "ORCAMENTO",
        "ORCAMENTOS",
        "FISCAL",
        "NOTA FISCAL",
        "NFCE",
        "NFE",
        "FORNECEDOR",
        "FORNECEDORES",
        "CLIENTE",
        "CLIENTES",
        "VALE",
        "VALES",
    }
)


def _normalizar_nome_operacao(nome: str) -> str:
    texto = unicodedata.normalize("NFKD", str(nome or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.upper().split())


def operacao_comercial_bloqueada(nome: str) -> bool:
    """True quando a operação exige licença válida (bloqueada no modo restrito)."""
    return _normalizar_nome_operacao(nome) in OPERACOES_COMERCIAIS_BLOQUEADAS


def _status(
    code: str,
    message: str,
    days: int | None,
    is_expired: bool,
    is_warning: bool,
    color: str,
    source: str,
) -> dict:
    return {
        "code": code,
        "message": message,
        "days_left": days,
        "is_expired": is_expired,
        "is_warning": is_warning,
        "color": color,
        "renewal_url": RENOVACAO_URL,
        "source": source,
        "restricted": is_expired,
    }


def _status_from_expiration(expira_em: datetime, is_trial: bool = False) -> dict:
    """Status histórico a partir da data de expiração gravada na licença."""
    dias = (expira_em.date() - datetime.now().date()).days
    source = "trial" if is_trial else "licensed"

    if dias < 0:
        mensagem = (
            f"Licença Trial expirada há {abs(dias)} dia(s)"
            if is_trial
            else f"Licença expirada há {abs(dias)} dia(s)"
        )
        return _status("expired", mensagem, dias, True, False, "#ff5555", source)

    if is_trial and dias < 30:
        return _status(
            "warning",
            f"Licença Trial - {dias} dias restantes",
            dias,
            False,
            True,
            "#f1c40f",
            source,
        )

    if not is_trial and dias <= 7:
        return _status(
            "warning",
            f"Licença vence em {dias} dia(s)",
            dias,
            False,
            True,
            "#f1c40f",
            source,
        )

    if is_trial:
        return _status(
            "active",
            f"Licença Trial ativa ({dias} dia(s))",
            dias,
            False,
            False,
            "#2ecc71",
            source,
        )

    return _status(
        "active",
        f"Licença ativa ({dias} dia(s))",
        dias,
        False,
        False,
        "#2ecc71",
        source,
    )


class LicenseManager:
    """Status, Trial e ativação de licença do FRS Mercado (fluxo histórico)."""

    TRIAL_DIAS = TRIAL_DIAS

    def __init__(self, storage: LicenseStorage | None = None):
        self.storage = storage or LicenseStorage()
        self.service = LicenseService(self.storage)

    # ------------------------------------------------------------------
    # Arquitetura Stage 1 preservada (Ed25519 + trust anchor público)
    # ------------------------------------------------------------------
    def _status_licenca_assinada(self) -> dict | None:
        """Status da licença assinada; ``None`` quando não existe arquivo.

        Uma licença assinada presente porém inválida é fail-closed
        (``invalid_signed_license``) e nunca cai silenciosamente para Trial.
        """
        try:
            if not self.storage.license_path.is_file():
                return None
            status = self.service.get_status()
        except Exception as exc:
            return _status(
                "invalid_signed_license",
                f"Licença assinada inválida: {exc}",
                None,
                True,
                False,
                "#ff5555",
                "signed",
            )

        if status.valid:
            dias = None
            try:
                expira = datetime.fromisoformat(str(status.payload["expires_at"]))
                if expira.tzinfo is None:
                    expira = expira.replace(tzinfo=timezone.utc)
                dias = max(0, (expira - datetime.now(timezone.utc)).days)
            except Exception:
                dias = None
            alerta = dias is not None and dias <= 7
            return _status(
                "active",
                status.message,
                dias,
                False,
                alerta,
                "#f1c40f" if alerta else "#2ecc71",
                "signed",
            )

        return _status(
            "invalid_signed_license",
            f"Licença assinada inválida: {status.message}",
            None,
            True,
            False,
            "#ff5555",
            "signed",
        )

    def create_challenge(self) -> dict:
        """Challenge local (Stage 1). Não é exibido no fluxo do cliente final."""
        return self.service.create_challenge()

    def activate(self, license_text: str):
        """Ativa uma licença assinada Ed25519 (Stage 1 preservado)."""
        return self.service.activate(license_text)

    # ------------------------------------------------------------------
    # Status (Trial de 30 dias / licença ativada)
    # ------------------------------------------------------------------
    def _status_trial_sem_registro(self, cfg: dict) -> dict:
        trial_inicio = str(cfg.get("trial_start_date") or "").strip()
        if not trial_inicio:
            trial_inicio = datetime.now().date().isoformat()
            cfg["trial_start_date"] = trial_inicio
            cfg["license_mode"] = "trial"
            try:
                salvar_configuracoes(cfg, exibir_alerta=False)
            except Exception:
                pass

        try:
            inicio = datetime.strptime(trial_inicio, "%Y-%m-%d")
        except Exception:
            inicio = datetime.now()

        return _status_from_expiration(inicio + timedelta(days=self.TRIAL_DIAS), is_trial=True)

    def get_status(self) -> dict:
        assinada = self._status_licenca_assinada()
        if assinada is not None:
            return assinada

        try:
            cfg = carregar_configuracoes()
        except Exception as exc:
            return _status(
                "error",
                f"Licença: erro ao carregar configuração ({exc})",
                None,
                False,
                True,
                "#f1c40f",
                "trial",
            )

        modo_licenca = str(cfg.get("license_mode") or "").strip().lower()

        try:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT data_expiracao FROM licenca ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except Exception as exc:
            return _status(
                "error",
                f"Licença: erro de leitura ({exc})",
                None,
                False,
                True,
                "#f1c40f",
                "trial",
            )

        if not row or not row[0]:
            return self._status_trial_sem_registro(cfg)

        try:
            expira_em = datetime.strptime(str(row[0]).strip(), "%Y-%m-%d")
        except Exception:
            return _status(
                "error",
                "Licença: data inválida",
                None,
                False,
                True,
                "#f1c40f",
                "licensed",
            )

        return _status_from_expiration(expira_em, is_trial=(modo_licenca == "trial"))

    # ------------------------------------------------------------------
    # Chave curta (Item 10) — camada de transporte sobre a segurança atual
    # ------------------------------------------------------------------
    def chave_instalacao_curta(self) -> str:
        """Gera a CHAVE CURTA DE INSTALAÇÃO que o cliente envia à FRS.

        Ela embrulha o challenge oficial (`FRS-MERCADO-CHALLENGE`), que já
        contém `installation_id` e o `machine_binding`. O cliente não precisa
        saber disso: ele apenas copia e envia a chave.

        Nenhuma segredo é gerado aqui — a chave é um PEDIDO de licença, não
        uma concessão. Ela não habilita nada sozinha.
        """
        try:
            challenge = self.service.create_challenge()
        except Exception:
            # Sem identidade de máquina (fora do Windows, por exemplo), ainda
            # entregamos uma chave: a FRS confirma o que falta na emissão.
            challenge = {}
        try:
            return chave_curta.gerar_chave_instalacao(challenge)
        except chave_curta.ErroChaveCurta:
            return ""

    @staticmethod
    def _e_chave_curta_ativacao(texto: str) -> bool:
        return chave_curta.parece_chave_curta(texto)

    def _envelope_da_chave_curta(self, texto: str) -> str:
        """Chave curta de ativação -> envelope JSON assinado.

        Recodifica no formato canônico EXATO que o `LicenseVerifier` já
        valida hoje. A assinatura não é recalculada nem reescrita aqui: ela
        veio dentro da chave, assinada pela FRS.
        """
        envelope = chave_curta.ler_chave_ativacao(texto)
        return formats.encode_envelope(envelope)

    # ------------------------------------------------------------------
    # Ativação (fluxo histórico comprovado)
    # ------------------------------------------------------------------
    def identificador_ativacao(self) -> str:
        """Identificador usado na chave: market_id, CNPJ ou razão social."""
        try:
            cfg = carregar_configuracoes()
        except Exception:
            cfg = {}
        return (
            str(cfg.get("market_id") or "").strip()
            or str(cfg.get("cnpj") or "").strip()
            or str(cfg.get("razao_social") or "").strip()
            or "FRS_MERCADO"
        ).upper()

    def chave_esperada(self, data_chave: str, identificador: str | None = None) -> str:
        ident = str(identificador or self.identificador_ativacao()).strip().upper()
        base = f"{ident}|{data_chave}|{FRS_ATIVACAO_2026}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:12].upper()

    @staticmethod
    def _e_documento_assinado(texto: str) -> bool:
        return str(texto or "").strip().startswith("{")

    def validar_chave(self, chave: str):
        """Valida a chave digitada sem persistir.

        Retorna ``(ok, mensagem, data_expiracao)``. Aceita, na ordem:

        - a **chave curta de ativação** (`FRS-A-...`, Item 10);
        - o envelope assinado (Stage 1, já existente);
        - a chave histórica ``FRS-AAAAMMDD-XXXXXXXXXXXX``.
        """
        texto = normalizar_texto_ativacao(chave)
        if not texto:
            return False, "Informe a chave de ativação.", None

        # Item 10: chave curta vira envelope assinado antes de qualquer
        # verificação. A segurança é a MESMA — só muda o transporte.
        if chave_curta.parece_chave_curta(texto):
            try:
                texto = _decodificar_chave_curta(texto)
            except chave_curta.ErroChaveCurta as exc:
                return False, exc.mensagem, None

        if self._e_documento_assinado(texto):
            try:
                validacao = self.service.verifier.verify(texto)
                expira = datetime.fromisoformat(str(validacao.payload["expires_at"]))
                return True, "Licença assinada validada com sucesso.", expira.date()
            except Exception as exc:
                return False, f"Chave de ativação inválida: {exc}", None

        texto_normalizado = texto.upper().replace(" ", "")
        if texto_normalizado.startswith(PREFIXO_LEGADO):
            texto_normalizado = texto_normalizado.split(":", 1)[1].strip()

        match = PADRAO_CHAVE_ATIVACAO.match(texto_normalizado)
        if not match:
            return False, "Formato inválido. Use FRS-AAAAMMDD-XXXXXXXXXXXX.", None

        data_raw, assinatura = match.groups()
        try:
            data_exp = datetime.strptime(data_raw, "%Y%m%d").date()
        except ValueError:
            return False, "Data da chave inválida.", None

        if assinatura != self.chave_esperada(data_raw):
            return False, "Chave de ativação inválida para este cliente.", None

        return True, "Licença validada com sucesso.", data_exp

    def _gravar_expiracao(self, data_exp: date, modo: str) -> None:
        with get_db_connection() as conn:
            row = conn.execute("SELECT id FROM licenca ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                conn.execute(
                    "UPDATE licenca SET data_expiracao = ? WHERE id = ?",
                    (data_exp.isoformat(), row[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO licenca (data_expiracao) VALUES (?)",
                    (data_exp.isoformat(),),
                )
        try:
            cfg = carregar_configuracoes()
            cfg["license_mode"] = modo
            cfg["license_expiration"] = data_exp.isoformat()
            salvar_configuracoes(cfg, exibir_alerta=False)
        except Exception:
            pass

    def iniciar_trial(self, dias: int = TRIAL_DIAS) -> date:
        """Cria o Trial de 30 dias no setup inicial (fluxo histórico)."""
        data_exp = datetime.now().date() + timedelta(days=max(1, int(dias)))
        self._gravar_expiracao(data_exp, "trial")
        return data_exp

    def ativar(self, chave: str):
        """Ativa a chave digitada e persiste o resultado.

        Retorna ``(ok, mensagem, status)``.

        Aceita a chave curta de ativação (`FRS-A-...`), o envelope assinado e
        a chave histórica. Nos três casos a decisão de validade é a mesma: a
        chave curta é decodificada antes e entregue ao verificador já existente.
        """
        texto = normalizar_texto_ativacao(chave)

        # Item 10: chave curta -> envelope assinado, antes de qualquer coisa.
        if chave_curta.parece_chave_curta(texto):
            try:
                texto = _decodificar_chave_curta(texto)
            except chave_curta.ErroChaveCurta as exc:
                return False, exc.mensagem, None

        if self._e_documento_assinado(texto):
            try:
                status = self.service.activate(texto)
            except Exception as exc:
                return False, f"Chave de ativação inválida: {exc}", None
            return True, status.message, self.get_status()

        ok, mensagem, data_exp = self.validar_chave(texto)
        if not ok or data_exp is None:
            return False, mensagem, None

        try:
            cfg = carregar_configuracoes()
            cfg["license_key"] = texto
            salvar_configuracoes(cfg, exibir_alerta=False)
            self._gravar_expiracao(data_exp, "full")
        except Exception as exc:
            return False, f"Licença válida, porém não foi possível gravar: {exc}", None

        dias = (data_exp - datetime.now().date()).days
        return (
            True,
            f"Licença ativada! Nova validade: {data_exp.isoformat()} ({dias} dias).",
            self.get_status(),
        )



