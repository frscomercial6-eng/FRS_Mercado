import traceback
from pathlib import Path
from datetime import datetime
import os
import sys

import customtkinter as ctk

from client_credentials_store import load_client_credentials
from modulo_login import ModuloLogin
from database_manager import get_db_connection, obter_caminho_dados
from error_notifier import notify_error, ensure_error_telemetry_started
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
        # Falha de log nao pode interromper inicializacao.
        pass


def _aplicar_configuracao_segura_ui() -> None:
    try:
        ctk.set_appearance_mode("Dark")
    except Exception:
        pass
    try:
        ctk.set_default_color_theme("blue")
    except Exception:
        pass


def _global_exception_handler(exc_type, exc_value, exc_tb) -> None:
    try:
        _log_debug("Excecao global nao tratada", exc_value)
        notify_error("excecao_global", exc_value)
    except Exception:
        pass


sys.excepthook = _global_exception_handler


def _garantir_banco_inicial() -> None:
    """Força criação do banco local se inexistente, sem dados pré-semeados."""
    with get_db_connection() as conn:
        conn.execute("SELECT 1")


def _carregar_credenciais_cliente() -> None:
    """Carrega credenciais protegidas para uso unificado no desktop/mobile."""
    try:
        dados = load_client_credentials()
    except Exception as e:
        _log_debug("Falha ao carregar arquivo protegido de credenciais do cliente", e)
        return

    if not dados:
        return

    mapa = {
        "license_key": "FRS_CLIENT_LICENSE_KEY",
        "client_key": "FRS_CLIENT_KEY",
        "firebase_admin_key_path": "FIREBASE_ADMIN_KEY_PATH",
        "google_oauth_credentials_path": "FRS_GOOGLE_CREDENTIALS_PATH",
        "google_services_path": "FRS_GOOGLE_SERVICES_PATH",
    }

    for source_key, env_key in mapa.items():
        value = str(dados.get(source_key, "") or "").strip()
        if not value:
            continue
        os.environ[env_key] = value


def main() -> None:
    """Fluxo único de inicialização: Login/Licença -> Interface principal."""
    ensure_error_telemetry_started()
    _aplicar_configuracao_segura_ui()

    for tentativa in range(2):
        usuario_logado = None
        app = None
        try:
            _carregar_credenciais_cliente()
            _garantir_banco_inicial()

            app = ctk.CTk()
            app.withdraw()

            def _ao_logar_com_sucesso(user_info):
                nonlocal usuario_logado
                usuario_logado = user_info

                # Encerra o loop de login para seguir ao modulo principal.
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

            if usuario_logado:
                try:
                    from modulo_main import iniciar_sistema

                    iniciar_sistema(usuario_logado)
                except Exception as e:
                    _log_debug("Falha ao carregar/inicializar modulo_main", e)
                    notify_error("modulo_main", e)
            return

        except Exception as e:
            _log_debug("Erro critico na inicializacao geral", e)
            notify_error("inicializacao_geral", e)
            _aplicar_configuracao_segura_ui()
            if tentativa == 0:
                continue

        finally:
            try:
                if app is not None and app.winfo_exists():
                    app.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    main()
