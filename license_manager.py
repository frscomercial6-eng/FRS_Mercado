from datetime import datetime, timedelta

from database_manager import get_db_connection
from modulo_config import carregar_configuracoes, salvar_configuracoes

RENOVACAO_URL = "https://invoice.infinitepay.io/plans/frsoficinadepesca/avka57U38g"


class LicenseManager:
    TRIAL_DIAS = 30

    def _status_from_expiration(self, expira_em, is_trial=False):
        dias = (expira_em.date() - datetime.now().date()).days
        if dias < 0:
            msg = (
                f"Licença Trial expirada há {abs(dias)} dia(s)"
                if is_trial
                else f"Licença expirada há {abs(dias)} dia(s)"
            )
            return {
                "code": "expired",
                "message": msg,
                "days_left": dias,
                "is_expired": True,
                "is_warning": False,
                "color": "#ff5555",
                "renewal_url": RENOVACAO_URL,
            }

        if is_trial and dias < 30:
            return {
                "code": "warning",
                "message": f"Licença Trial - {dias} dias restantes",
                "days_left": dias,
                "is_expired": False,
                "is_warning": True,
                "color": "#f1c40f",
                "renewal_url": RENOVACAO_URL,
            }

        if not is_trial and dias <= 7:
            return {
                "code": "warning",
                "message": f"Licença vence em {dias} dia(s)",
                "days_left": dias,
                "is_expired": False,
                "is_warning": True,
                "color": "#f1c40f",
                "renewal_url": RENOVACAO_URL,
            }

        if is_trial:
            return {
                "code": "active",
                "message": f"Licença Trial ativa ({dias} dia(s))",
                "days_left": dias,
                "is_expired": False,
                "is_warning": False,
                "color": "#2ecc71",
                "renewal_url": RENOVACAO_URL,
            }

        return {
            "code": "active",
            "message": f"Licença ativa ({dias} dia(s))",
            "days_left": dias,
            "is_expired": False,
            "is_warning": False,
            "color": "#2ecc71",
            "renewal_url": RENOVACAO_URL,
        }

    def get_status(self):
        cfg = carregar_configuracoes()
        modo_licenca = str(cfg.get("license_mode") or "").strip().lower()

        try:
            with get_db_connection() as conn:
                row = conn.execute("SELECT data_expiracao FROM licenca ORDER BY id DESC LIMIT 1").fetchone()
        except Exception as e:
            return {
                "code": "error",
                "message": f"Licença: erro de leitura ({e})",
                "days_left": None,
                "is_expired": False,
                "is_warning": True,
                "color": "#f1c40f",
                "renewal_url": RENOVACAO_URL,
            }

        if not row or not row[0]:
            trial_inicio = str(cfg.get("trial_start_date") or "").strip()
            if not trial_inicio:
                trial_inicio = datetime.now().date().isoformat()
                cfg["trial_start_date"] = trial_inicio
                cfg["license_mode"] = "trial"
                salvar_configuracoes(cfg, exibir_alerta=False)

            try:
                inicio = datetime.strptime(trial_inicio, "%Y-%m-%d")
            except Exception:
                inicio = datetime.now()

            exp_trial = inicio + timedelta(days=self.TRIAL_DIAS)
            return self._status_from_expiration(exp_trial, is_trial=True)

        data_exp = str(row[0]).strip()
        try:
            expira_em = datetime.strptime(data_exp, "%Y-%m-%d")
        except Exception:
            return {
                "code": "error",
                "message": "Licença: data inválida",
                "days_left": None,
                "is_expired": False,
                "is_warning": True,
                "color": "#f1c40f",
                "renewal_url": RENOVACAO_URL,
            }

        return self._status_from_expiration(expira_em, is_trial=(modo_licenca == "trial"))
