import os
import smtplib
import traceback
import threading
import queue
import time
import platform
import getpass
from pathlib import Path
from datetime import datetime
from email.message import EmailMessage
from urllib import request

from app_paths import obter_caminho_log

SUPPORT_EMAIL = "FRS.suporte.oficina@gmail.com"
APP_NAME = "FRS Mercado"


_telemetry_queue: "queue.Queue[dict]" = queue.Queue()
_telemetry_started = False
_telemetry_lock = threading.Lock()


def _error_log_path() -> Path:
    return Path(obter_caminho_log("error_log.txt"))


def _append_error_log(message: str) -> None:
    try:
        path = _error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(message)
            f.write("\n" + ("-" * 80) + "\n")
    except Exception:
        pass


def _internet_available(timeout: int = 5) -> bool:
    try:
        request.urlopen("https://www.google.com/generate_204", timeout=timeout)
        return True
    except Exception:
        return False


def _machine_user_label() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = os.getenv("USERNAME", "desconhecido")

    try:
        machine = platform.node() or os.getenv("COMPUTERNAME", "desconhecido")
    except Exception:
        machine = os.getenv("COMPUTERNAME", "desconhecido")

    return f"usuario={user} | maquina={machine}"


def ensure_error_telemetry_started() -> None:
    global _telemetry_started
    with _telemetry_lock:
        if _telemetry_started:
            return

        def _worker() -> None:
            while True:
                item = _telemetry_queue.get()
                if not isinstance(item, dict):
                    continue

                retries = int(item.get("retries", 0) or 0)
                while not _internet_available():
                    time.sleep(15)

                ok, retorno = send_error_email(
                    subject=str(item.get("subject", "[FRS Mercado] Erro")),
                    body=str(item.get("body", "")),
                    to_email=str(item.get("to", SUPPORT_EMAIL)),
                )

                if ok:
                    _append_error_log(f"[TELEMETRIA] Envio concluido: {retorno}")
                    continue

                retries += 1
                _append_error_log(f"[TELEMETRIA] Falha de envio (tentativa {retries}): {retorno}")
                if retries < 10:
                    item["retries"] = retries
                    _telemetry_queue.put(item)
                    time.sleep(10)

        threading.Thread(target=_worker, daemon=True, name="frs-error-telemetry").start()
        _telemetry_started = True


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
    ensure_error_telemetry_started()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stack_text = stack if stack else traceback.format_exc()
    host_info = _machine_user_label()
    body = (
        f"Sistema: {APP_NAME}\n"
        f"Data/Hora: {now}\n"
        f"Host: {host_info}\n"
        f"Contexto: {context}\n"
        f"Erro: {err}\n\n"
        f"Traceback:\n{stack_text}\n"
    )
    subject = f"[{APP_NAME}] Erro critico - {context}"

    _append_error_log(
        f"[{now}] {subject}\n{host_info}\nErro: {err}\nTraceback:\n{stack_text}"
    )

    _telemetry_queue.put(
        {
            "subject": subject,
            "body": body,
            "to": SUPPORT_EMAIL,
            "retries": 0,
        }
    )
    return True, "Erro registrado e enfileirado para telemetria em background."
