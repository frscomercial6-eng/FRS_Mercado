import traceback
from pathlib import Path
from datetime import datetime
import subprocess
import sys

import customtkinter as ctk

from app_paths import e_compilado, executavel_atual, obter_caminho_log


# F-3: CONFIG_FILE/DB_PATH são resolvidos no import. Se a adoção de dados
# falhar fechada, o import de modulo_login/database_manager aborta antes de
# qualquer janela existir. Capturamos aqui para exibir uma mensagem clara em
# vez de um traceback cru, sem enfraquecer o fail-closed em nenhum momento.
_ERRO_RAIZ_DADOS: Exception | None = None
try:
    from modulo_login import ModuloLogin
    from database_manager import get_db_connection
except Exception as _exc:  # pragma: no cover - depende do ambiente do cliente
    _ERRO_RAIZ_DADOS = _exc
    ModuloLogin = None
    get_db_connection = None


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
    """Antes do login, registra erros somente no arquivo local.

    Nenhuma notificação remota é iniciada nesta fase da aplicação.
    """
    try:
        _log_debug("Excecao global nao tratada", exc_value)
    except Exception:
        pass


sys.excepthook = _global_exception_handler


def _garantir_banco_inicial() -> None:
    """Força criação do banco local se inexistente, sem dados pré-semeados."""
    with get_db_connection() as conn:
        conn.execute("SELECT 1")


def _reexecutar_runtime_versionado() -> bool:
    """Encaminha o Portable raiz para a versão validada em runtime/current.json."""
    if not e_compilado():
        return False
    # No Nuitka, sys.executable aponta para o python.exe interno da dist.
    # O executável real do produto vem de __compiled__.original_argv0 /
    # sys.argv[0], e é ele que dá o nome procurado em runtime/versions/.
    executavel = executavel_atual()
    if executavel is None:
        return False
    root = executavel.parent
    pointer = root / "runtime" / "current.json"
    if not pointer.is_file():
        return False
    try:
        import json
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        if payload.get("schema") != "FRS-MERCADO-RUNTIME-POINTER-V1":
            return False
        target = Path(str(payload.get("runtime") or "")).resolve()
        runtime_root = (root / "runtime" / "versions").resolve()
        if target == executavel.parent:
            return False
        if runtime_root != target and runtime_root not in target.parents:
            _log_debug("Ponteiro de runtime rejeitado: destino fora de runtime/versions.")
            return False
        target_executable = target / executavel.name
        if not target_executable.is_file():
            _log_debug(f"Runtime apontado não encontrado: {target_executable}")
            return False
        args = [str(target_executable), *sys.argv[1:]]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) if hasattr(subprocess, "DETACHED_PROCESS") else 0
        subprocess.Popen(args, cwd=str(target), close_fds=True, creationflags=flags)
        return True
    except Exception as exc:
        _log_debug("Falha ao despachar runtime versionado", exc)
        return False


def _informar_erro_raiz_dados(exc: Exception) -> None:
    """Falha fechada na raiz de dados: informa sem apagar nem recriar config.

    Não há recuperação automática: o arquivo do cliente foi preservado e a
    correção é manual, sobre o caminho informado.
    """
    try:
        caminho = str(getattr(exc, "caminho", "") or "")
    except Exception:
        caminho = ""

    mensagem = (
        "Não foi possível preparar a pasta de dados do FRS Mercado.\n\n"
        "Se o config.json da empresa estiver inválido, ele foi PRESERVADO: "
        "nada foi apagado, substituído ou recriado com valores padrão.\n\n"
    )
    if caminho:
        mensagem += f"Arquivo preservado:\n{caminho}\n\n"
    mensagem += (
        "Corrija o arquivo (ou restaure uma cópia válida) e abra o programa "
        "novamente.\n\n"
        f"Detalhe técnico: {exc}"
    )

    try:
        from tkinter import messagebox

        messagebox.showerror("FRS Mercado — dados preservados", mensagem)
    except Exception:
        print(mensagem)


def main() -> None:
    """Fluxo local e offline: banco -> autenticação -> interface principal."""
    if "--update-health-check" in sys.argv:
        # O helper externo usa este modo para validar o runtime novo sem
        # abrir login, caixa, PDV ou qualquer integração remota.
        print("FRS Mercado update health check: OK")
        return

    if _reexecutar_runtime_versionado():
        return

    if _ERRO_RAIZ_DADOS is not None:
        _informar_erro_raiz_dados(_ERRO_RAIZ_DADOS)
        return

    _aplicar_configuracao_segura_ui()

    for tentativa in range(2):
        usuario_logado = None
        app = None
        try:
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
            return

        except Exception as e:
            _log_debug("Erro critico na inicializacao geral", e)
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
