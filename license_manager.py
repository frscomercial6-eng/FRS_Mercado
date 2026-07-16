from datetime import datetime

from database_manager import get_db_connection

RENOVACAO_URL = "https://invoice.infinitepay.io/plans/frsoficinadepesca/avka57U38g"


class LicenseManager:
    def get_status(self):
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
            return {
                "code": "expired",
                "message": "Licença: não localizada",
                "days_left": -1,
                "is_expired": True,
                "is_warning": False,
                "color": "#ff5555",
                "renewal_url": RENOVACAO_URL,
            }

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

        dias = (expira_em.date() - datetime.now().date()).days
        if dias < 0:
            return {
                "code": "expired",
                "message": f"Licença expirada há {abs(dias)} dia(s)",
                "days_left": dias,
                "is_expired": True,
                "is_warning": False,
                "color": "#ff5555",
                "renewal_url": RENOVACAO_URL,
            }

        if dias <= 7:
            return {
                "code": "warning",
                "message": f"Licença vence em {dias} dia(s)",
                "days_left": dias,
                "is_expired": False,
                "is_warning": True,
                "color": "#f1c40f",
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
