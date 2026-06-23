import os
import sys
import tempfile


def _base_executavel():
    """Retorna diretório base do executável quando app está empacotado."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _base_appdata():
    base_dir = os.environ.get("APPDATA")
    if not base_dir:
        base_dir = os.path.expanduser("~")
    return os.path.join(base_dir, "FRS_Mercado", "data")


def _normalizar(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _esta_em_program_files(path: str) -> bool:
    """Retorna True quando o caminho está sob Program Files/Program Files (x86)."""
    alvo = _normalizar(path)
    bases = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        valor = os.environ.get(var)
        if valor:
            bases.append(_normalizar(valor))

    for base in bases:
        if alvo == base or alvo.startswith(base + os.sep):
            return True
    return False


def _garantir_diretorio(path_base):
    os.makedirs(path_base, exist_ok=True)
    return path_base


def _garantir_escrita(path_base: str) -> None:
    """Valida permissão de escrita real no diretório (não apenas existência)."""
    _garantir_diretorio(path_base)
    fd, tmp_path = tempfile.mkstemp(prefix="frs_write_test_", dir=path_base)
    os.close(fd)
    os.remove(tmp_path)


def obter_caminho_dados(*partes):
    """
    Retorna caminho de dados priorizando pasta relativa ao executável quando permitido.

    Regras:
    1) Em app empacotado fora de Program Files, tenta <pasta_do_exe>/data/...
    2) Se não houver permissão de escrita, cai para %APPDATA%/FRS_Mercado/...
    3) Em execução de código-fonte, usa %APPDATA%/FRS_Mercado/...
    """
    preferencias = []
    if getattr(sys, "frozen", False):
        base_exe = _base_executavel()
        if not _esta_em_program_files(base_exe):
            preferencias.append(os.path.join(base_exe, "data"))
    preferencias.append(_base_appdata())

    ultimo_erro = None
    for base in preferencias:
        try:
            app_dir = _garantir_diretorio(base)
            _garantir_escrita(app_dir)

            if not partes:
                return app_dir

            destino = os.path.join(app_dir, *partes)
            os.makedirs(os.path.dirname(destino) or app_dir, exist_ok=True)
            return destino
        except Exception as e:
            ultimo_erro = e
            continue

    raise RuntimeError(f"Falha ao resolver caminho de dados: {ultimo_erro}")


def obter_caminho_log(nome_arquivo: str) -> str:
    """Retorna caminho de log com fallback silencioso para APPDATA em caso de permissão negada."""
    if getattr(sys, "frozen", False):
        base_exe = _base_executavel()
        if not _esta_em_program_files(base_exe):
            local_log = os.path.join(base_exe, "data", nome_arquivo)
            try:
                os.makedirs(os.path.dirname(local_log), exist_ok=True)
                with open(local_log, "a", encoding="utf-8"):
                    pass
                return local_log
            except Exception:
                pass

    return obter_caminho_dados(nome_arquivo)
