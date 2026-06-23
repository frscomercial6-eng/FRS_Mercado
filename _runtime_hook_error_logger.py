import datetime
import pathlib
import sys
import traceback

from error_notifier import notify_error
from app_paths import obter_caminho_log


def _log_runtime_error(exc_type, exc_value, exc_tb):
    try:
        log_file = pathlib.Path(obter_caminho_log("FRS_Mercado_runtime_error.log"))
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stack = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))

        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] Falha nao tratada no executavel\n")
            f.write(f"Tipo: {getattr(exc_type, '__name__', str(exc_type))}\n")
            f.write(f"Mensagem: {exc_value}\n")
            if isinstance(exc_value, ModuleNotFoundError):
                f.write(f"Modulo ausente: {getattr(exc_value, 'name', 'desconhecido')}\n")
            f.write("Traceback:\n")
            f.write(stack)
            f.write("\n" + ("-" * 80) + "\n")

        notify_error("runtime_hook", exc_value, stack)
    except Exception:
        pass


sys.excepthook = _log_runtime_error
