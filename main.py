import traceback
from pathlib import Path
from datetime import datetime

import customtkinter as ctk
from tkinter import messagebox

from modulo_login import ModuloLogin
from database_manager import get_db_connection, obter_caminho_dados
from error_notifier import notify_error
from app_paths import obter_caminho_log


def _log_debug(contexto: str, erro: Exception | None = None) -> None:
    log_path = Path(obter_caminho_log("log_debug.txt"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] {contexto}\n")
            if erro is not None:
                f.write(f"Erro: {erro}\n")
                f.write(traceback.format_exc())
            f.write("\n" + ("-" * 80) + "\n")
    except Exception:
        # Falha de log não pode interromper inicialização.
        pass


def _garantir_banco_inicial() -> None:
    """Força criação do banco local se inexistente, sem dados pré-semeados."""
    with get_db_connection() as conn:
        conn.execute("SELECT 1")


def main() -> None:
    """Fluxo único de inicialização: Login/Licença -> Interface principal."""
    usuario_logado = None

    try:
        _garantir_banco_inicial()

        app = ctk.CTk()
        app.withdraw()

        def _ao_logar_com_sucesso(user_info):
            nonlocal usuario_logado
            usuario_logado = user_info

            # Encerra o loop de login para seguir ao módulo principal.
            try:
                if app.winfo_exists():
                    app.quit()
            except Exception:
                pass

        ModuloLogin(app, callback_sucesso=_ao_logar_com_sucesso)
        app.mainloop()

        try:
            if app.winfo_exists():
                app.destroy()
        except Exception:
            pass

        # Só inicia a interface principal após autenticação/licença válidas.
        if usuario_logado:
            try:
                from modulo_main import iniciar_sistema

                iniciar_sistema(usuario_logado)
            except Exception as e:
                _log_debug("Falha ao carregar/inicializar modulo_main", e)
                enviado, retorno_email = notify_error("modulo_main", e)
                _log_debug(f"Notificação de erro por e-mail: {retorno_email}")
                messagebox.showerror(
                    "Erro na Inicialização",
                    f"Falha ao abrir módulo principal.\nResumo: {e}\n\nConsulte log_debug.txt.",
                )

    except Exception as e:
        _log_debug("Erro crítico na inicialização geral", e)
        enviado, retorno_email = notify_error("inicializacao_geral", e)
        _log_debug(f"Notificação de erro por e-mail: {retorno_email}")
        print(f"Erro crítico na inicialização: {e}")
        traceback.print_exc()
        try:
            messagebox.showerror(
                "Erro Crítico",
                f"Falha ao iniciar o sistema.\nResumo: {e}\n\nConsulte log_debug.txt.",
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
