import os
import sys
import tempfile
from pathlib import Path



def e_compilado() -> bool:
    """Indica se o app está em execução como executável compilado.

    PyInstaller define ``sys.frozen``; o Nuitka NÃO define ``sys.frozen``, mas
    injeta ``__compiled__`` nos módulos compilados. Depender só de
    ``sys.frozen`` deixaria esses fluxos inertes num build Nuitka.
    """
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def executavel_atual():
    """Caminho do executável real do app, ou None em execução por código-fonte.

    No Nuitka, ``sys.executable`` aponta para o ``python.exe`` interno da pasta
    distribuída, e não para o executável do produto. Por isso usamos
    ``__compiled__.original_argv0`` / ``sys.argv[0]`` e nunca o nome de
    ``sys.executable``.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()

    compilado = globals().get("__compiled__")
    if compilado is not None:
        candidatos = [getattr(compilado, "original_argv0", None)]
        argv = getattr(sys, "argv", None) or []
        if argv:
            candidatos.append(argv[0])
        for candidato in candidatos:
            if not candidato:
                continue
            try:
                caminho = Path(str(candidato)).resolve()
            except Exception:
                continue
            if caminho.is_file():
                return caminho
    return None


def _base_executavel():
    """Retorna diretório base do executável quando app está empacotado."""
    executavel = executavel_atual()
    if executavel is not None:
        return str(executavel.parent)
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


# Tabelas consideradas operacionais: registro em qualquer uma delas significa que o
# banco NÃO é a semente vazia distribuída com o Portable.
_TABELAS_OPERACIONAIS = (
    "produtos",
    "vendas",
    "itens_venda",
    "caixa_operacao",
    "clientes",
    "fornecedores",
)

# Trava de processo: a adoção é avaliada uma única vez por execução.
_ADOCAO_AVALIADA = False


def _sqlite_valido(caminho) -> bool:
    """True se o arquivo é um SQLite legível com header válido."""
    try:
        caminho = Path(caminho)
        if not caminho.is_file() or caminho.stat().st_size <= 0:
            return False
        with caminho.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                return False
    except Exception:
        return False

    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    return True


def _total_registros_operacionais(caminho) -> int:
    """Soma registros das tabelas operacionais que existirem no banco."""
    total = 0
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
        try:
            existentes = {
                linha[0]
                for linha in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for tabela in _TABELAS_OPERACIONAIS:
                if tabela not in existentes:
                    continue
                try:
                    total += int(conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] or 0)
                except Exception:
                    continue
        finally:
            conn.close()
    except Exception:
        return -1
    return total


def _adotar_banco_portavel(base_exe) -> str:
    """Adoção one-shot do banco legado de instalações Portable anteriores.

    Só ocorre quando o app está compilado, fora de Program Files, e a resolução
    de dados está elegendo ``<exe>/data``. O Portable 1.0.20/1.0.21 distribuía
    um banco-semente vazio em ``<exe>/data`` enquanto gravava os dados reais em
    ``%APPDATA%``; sem esta adoção, o Portable corrigido abriria o banco vazio.

    Regras conservadoras: nunca move, nunca apaga e nunca sobrescreve o legado;
    nunca sobrescreve um destino que já tenha dados operacionais; qualquer
    dúvida resulta em "não copiar". É idempotente.
    """
    global _ADOCAO_AVALIADA
    if _ADOCAO_AVALIADA:
        return "já avaliada nesta execução"
    _ADOCAO_AVALIADA = True

    try:
        destino = Path(base_exe) / "data" / "mercado.db"
        legado = Path(_base_appdata()) / "mercado.db"

        # 1) Legado precisa existir, ser SQLite válido e ter dados operacionais.
        if not legado.is_file():
            return "legado ausente: nada a fazer"
        if not _sqlite_valido(legado):
            return "legado não é um SQLite válido"
        total_legado = _total_registros_operacionais(legado)
        if total_legado <= 0:
            return "legado sem dados operacionais: não copiar"

        # 2) Destino: só seguimos se estiver ausente ou for semente vazia.
        if destino.exists():
            if not _sqlite_valido(destino):
                return "destino não é um SQLite válido: não tocar"
            total_destino = _total_registros_operacionais(destino)
            if total_destino > 0:
                return "destino já possui dados operacionais: não sobrescrever"
            if total_destino < 0:
                return "destino ilegível: não tocar"

        # 3) Sem sidecars órfãos: o backup da API do SQLite gera um banco
        # completo e autocontido (sem -wal/-shm), portanto nada precisa ser
        # renomeado para o nome final. Se o destino já tiver sidecars, não
        # adotamos — associar WAL/SHM antigo ao novo banco seria inconsistente.
        for sufixo in ("-wal", "-shm", "-journal"):
            if Path(str(destino) + sufixo).exists():
                return f"destino possui sidecar {sufixo}: risco de inconsistência, não adotar"

        destino.parent.mkdir(parents=True, exist_ok=True)
        temporario = destino.with_name(destino.name + ".adocao.tmp")
        if temporario.exists():
            temporario.unlink()

        import sqlite3

        origem = sqlite3.connect(f"file:{legado}?mode=ro", uri=True)
        try:
            alvo = sqlite3.connect(temporario)
            try:
                origem.backup(alvo)
            finally:
                alvo.close()
        finally:
            origem.close()

        # Valida o temporário antes de promover: se não estiver íntegro, aborta.
        if not _sqlite_valido(temporario):
            try:
                temporario.unlink()
            except Exception:
                pass
            return "backup gerado inválido: adoção cancelada"
        for sufixo in ("-wal", "-shm", "-journal"):
            try:
                Path(str(temporario) + sufixo).unlink()
            except FileNotFoundError:
                pass

        os.replace(temporario, destino)
        return (
            f"banco legado adotado ({total_legado} registros); "
            "origem preservada em %APPDATA%"
        )
    except Exception as exc:
        return f"adoção ignorada com segurança: {type(exc).__name__}"



def _garantir_escrita(path_base: str) -> None:
    """Valida permissão de escrita real no diretório (não apenas existência)."""
    _garantir_diretorio(path_base)
    fd, tmp_path = tempfile.mkstemp(prefix="frs_write_test_", dir=path_base)
    os.close(fd)
    os.remove(tmp_path)


def _versioned_portable_data_base():
    """Resolve a pasta de dados compartilhada de um runtime versionado."""
    if not e_compilado():
        return None
    exe_dir = _base_executavel()
    try:
        # <app_root>/runtime/versions/<versao>/FRS_Mercado.exe
        version_dir = Path(exe_dir).resolve().parent
        runtime_dir = version_dir.parent
        app_root = runtime_dir.parent
        if version_dir.name == "versions" and runtime_dir.name == "runtime":
            return str(app_root / "data")
    except Exception:
        pass
    return None


def obter_caminho_dados(*partes):
    """
    Retorna caminho de dados priorizando pasta relativa ao executável quando permitido.

    Regras:
    1) Em app empacotado fora de Program Files, tenta <pasta_do_exe>/data/...
    2) Se não houver permissão de escrita, cai para %APPDATA%/FRS_Mercado/...
    3) Em execução de código-fonte, usa %APPDATA%/FRS_Mercado/...
    """
    preferencias = []
    if e_compilado():
        base_exe = _base_executavel()
        if not _esta_em_program_files(base_exe):
            base_runtime = _versioned_portable_data_base()
            if base_runtime:
                preferencias.append(base_runtime)
            # Portable legado: adota o banco real de %APPDATA% ANTES que o app
            # abra o banco-semente vazio de <exe>/data. Nunca sobrescreve um
            # destino com dados e nunca altera a origem.
            _adotar_banco_portavel(base_exe)
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
    """Usa sempre a mesma raiz de dados resolvida por obter_caminho_dados()."""
    return obter_caminho_dados(nome_arquivo)
