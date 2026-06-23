import os
import smtplib
import traceback
from datetime import datetime
from email.message import EmailMessage

SUPPORT_EMAIL = "FRS.suporte.oficina@gmail.com"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def send_error_email(
    subject: str,
    body: str,
    to_email: str = SUPPORT_EMAIL,
    smtp_client_factory=None,
) -> tuple[bool, str]:
    """Envia e-mail de erro para o suporte usando SMTP configurado por ambiente."""
    host = os.getenv("FRS_SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("FRS_SMTP_PORT", "587"))
    user = os.getenv("FRS_SMTP_USER", "")
    password = os.getenv("FRS_SMTP_PASS", "")
    from_email = os.getenv("FRS_SMTP_FROM", user)
    use_tls = _bool_env("FRS_SMTP_TLS", True)

    if not from_email or not user or not password:
        return False, "SMTP não configurado (FRS_SMTP_USER/FRS_SMTP_PASS)."

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    smtp_factory = smtp_client_factory or smtplib.SMTP
    server = None
    try:
        server = smtp_factory(host, port, timeout=20)
        if use_tls:
            server.starttls()
        server.login(user, password)
        server.send_message(msg)
        return True, f"E-mail enviado para {to_email}."
    except Exception as exc:
        return False, f"Falha no envio de e-mail: {exc}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def notify_error(
    context: str,
    err: Exception | None = None,
    stack: str | None = None,
    smtp_client_factory=None,
) -> tuple[bool, str]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stack_text = stack if stack else traceback.format_exc()
    body = (
        f"Sistema: FRS Mercado\n"
        f"Data/Hora: {now}\n"
        f"Contexto: {context}\n"
        f"Erro: {err}\n\n"
        f"Traceback:\n{stack_text}\n"
    )
    subject = f"[FRS Mercado] Erro crítico - {context}"
    return send_error_email(subject, body, smtp_client_factory=smtp_client_factory)
