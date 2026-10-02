import os
import sys
import tempfile
import json
import hashlib
import shutil
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
    "fornecedor_produtos",
    "entradas",
    "financeiro",
    "usuarios",
    "licenca",
    "vales",
    "vale_itens",
    "orcamentos",
    "orcamento_itens",
    "sangrias",
    "caixa_conferencia",
    "logs_auditoria",
    "logs_mentoria",
    "produto_lotes",
)

# Trava de processo: a adoção é avaliada uma única vez por execução.
_ADOCAO_AVALIADA = False

_CAMPOS_CONFIG_RECONHECIDOS = {
    "nome_fantasia", "razao_social", "nome_estabelecimento", "cnpj", "cep",
    "logradouro", "numero", "complemento", "bairro", "cidade", "uf",
    "endereco", "fiscal_ativo", "email_cliente", "drive_credentials_path",
    "drive_backup_folder_id", "emissor_fiscal_path", "market_id",
    "pasta_entrada_fiscal", "pasta_retorno_fiscal", "pasta_exportacao_fiscal",
    "auto_update_enabled", "auto_update_repo", "auto_update_remind_hours",
    "app_executable_path", "update_executable_path", "limite_sangria_preventiva",
    "mensagem_rodape_cupom", "largura_cupom_mm",
}
_CAMPOS_CONFIG_IDENTIDADE = (
    "nome_fantasia", "razao_social", "nome_estabelecimento", "cnpj", "cep",
    "logradouro", "numero", "complemento", "bairro", "cidade", "uf", "endereco",
    "email_cliente", "drive_credentials_path", "drive_backup_folder_id",
    "emissor_fiscal_path",
)
_ARQUIVOS_PERSISTENTES = (
    "client_credentials.sec.json",
    "credentials.json",
    "google-services.json",
    "token.pickle",
    "firebase-admin-key.json",
    "firebase-adminsdk.json",
    "update_state.json",
    "log_debug.txt",
    "config_mentora.json",
    "log_integracao.txt",
    "error_log.txt",
    "FRS_Mercado_runtime_error.log",
    "logs/fiscal_config_error.log",
    "acbrmonitor/ACBrMonitor.ini",
    "licensing/license.json",
    "licensing/installation_id",
    "licensing/challenge.json",
    "licensing/clock.json",
)
_ARQUIVOS_LOG = {
    "log_debug.txt",
    "log_integracao.txt",
    "error_log.txt",
    "FRS_Mercado_runtime_error.log",
    "logs/fiscal_config_error.log",
}
_DIRETORIOS_PERSISTENTES = (
    ("assets/produtos", {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}),
    ("backups", {".db"}),
    ("exportacao_fiscal", {".xml"}),
    ("fiscal_in", {".json", ".txt"}),
    ("fiscal_out", {".json", ".txt"}),
    ("logs", {".log", ".txt"}),
    ("relatorios", {".pdf", ".csv"}),
    ("templates", {".db"}),
)


def _ler_json_objeto(caminho):
    try:
        caminho = Path(caminho)
        if not caminho.is_file() or caminho.is_symlink():
            return None
        valor = json.loads(caminho.read_text(encoding="utf-8"))
        return valor if isinstance(valor, dict) else None
    except Exception:
        return None


def _config_json_valida(caminho):
    dados = _ler_json_objeto(caminho)
    if dados is None or not (_CAMPOS_CONFIG_RECONHECIDOS & set(dados)):
        return None
    return dados


def _config_tem_dados_usuario(dados, raiz_config=None):
    if any(str(dados.get(chave) or "").strip() for chave in _CAMPOS_CONFIG_IDENTIDADE):
        return True
    market_id = str(dados.get("market_id") or "").strip()
    if market_id and not market_id.lower().startswith("market-local-"):
        return True
    if str(dados.get("fiscal_ativo") or "").strip().lower() in {"true", "1", "sim"}:
        return True
    if str(dados.get("auto_update_enabled", True)).strip().lower() in {"false", "0", "nao", "não"}:
        return True
    repo = str(dados.get("auto_update_repo") or "").strip()
    if repo and repo != "frscomercial6-eng/FRS_Mercado":
        return True
    try:
        if int(dados.get("auto_update_remind_hours", 24)) != 24:
            return True
    except (TypeError, ValueError):
        return True
    if any(str(dados.get(chave) or "").strip() for chave in ("app_executable_path", "update_executable_path")):
        return True
    raiz_config = Path(raiz_config).resolve() if raiz_config is not None else None
    for chave, pasta_padrao in (
        ("pasta_entrada_fiscal", "fiscal_in"),
        ("pasta_retorno_fiscal", "fiscal_out"),
        ("pasta_exportacao_fiscal", "exportacao_fiscal"),
    ):
        valor = str(dados.get(chave) or "").strip()
        if valor:
            if raiz_config is None:
                if Path(valor).name.lower() != pasta_padrao:
                    return True
            elif os.path.normcase(os.path.abspath(valor)) != os.path.normcase(
                os.path.abspath(raiz_config / pasta_padrao)
            ):
                return True
    if str(dados.get("mensagem_rodape_cupom") or "").strip():
        return True
    # Largura da bobina: 58 mm é escolha do cliente (80 mm/absente = padrão).
    # Ausente NÃO conta como estado, para não reclassificar config antigo.
    largura = dados.get("largura_cupom_mm")
    if largura is not None and str(largura).strip() not in ("", "80"):
        return True
    try:
        if float(dados.get("limite_sangria_preventiva", 500.0)) != 500.0:
            return True
    except (TypeError, ValueError):
        return True
    return False


def _config_assinatura_usuario(dados, raiz_config=None):
    normalizados = {}
    raiz_config = Path(raiz_config).resolve() if raiz_config is not None else None
    caminhos_padrao = {
        "pasta_entrada_fiscal": "fiscal_in",
        "pasta_retorno_fiscal": "fiscal_out",
        "pasta_exportacao_fiscal": "exportacao_fiscal",
    }
    for chave, valor in dados.items():
        if chave in caminhos_padrao:
            texto = str(valor or "").strip()
            pasta_padrao = caminhos_padrao[chave]
            normalizados[chave] = (
                f"<padrao:{pasta_padrao}>"
                if not texto or (
                    raiz_config is not None
                    and os.path.normcase(os.path.abspath(texto))
                    == os.path.normcase(os.path.abspath(raiz_config / pasta_padrao))
                )
                else os.path.normcase(os.path.abspath(texto))
            )
        else:
            normalizados[chave] = valor
    return json.dumps(normalizados, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _copiar_arquivo_atomico(origem, destino, *, substituir=False):
    origem = Path(origem)
    destino = Path(destino)
    if destino.exists() and not substituir:
        return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, nome_temporario = tempfile.mkstemp(prefix=f".{destino.name}.adocao-", dir=str(destino.parent))
    os.close(fd)
    temporario = Path(nome_temporario)
    try:
        shutil.copy2(origem, temporario)
        if destino.exists() and not substituir:
            return False
        os.replace(temporario, destino)
        return True
    finally:
        try:
            temporario.unlink(missing_ok=True)
        except OSError:
            pass


def _sqlite_valido(caminho) -> bool:
    """True se o arquivo é um SQLite legível com header válido."""
    try:
        caminho = Path(caminho)
        if not caminho.is_file() or caminho.is_symlink() or caminho.stat().st_size <= 0:
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
            integridade = conn.execute("PRAGMA quick_check").fetchone()
            if not integridade or str(integridade[0]).lower() != "ok":
                return False
        finally:
            conn.close()
    except Exception:
        return False
    return True


def _sha256_arquivo(caminho):
    digest = hashlib.sha256()
    with Path(caminho).open("rb") as handle:
        for bloco in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def _sha256_banco(caminho):
    digest = hashlib.sha256()
    caminho = Path(caminho)
    for suffix in ("", "-wal", "-journal"):
        arquivo = Path(str(caminho) + suffix)
        if not arquivo.is_file():
            continue
        digest.update(suffix.encode("ascii"))
        with arquivo.open("rb") as handle:
            for bloco in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(bloco)
    return digest.hexdigest()


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


def _banco_tem_dados_usuario(caminho):
    """Detecta estado não-semente, inclusive configurações fiscais e contas."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{Path(caminho)}?mode=ro", uri=True)
        try:
            tabelas = {
                linha[0]
                for linha in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for tabela in _TABELAS_OPERACIONAIS:
                if tabela in tabelas and int(conn.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0] or 0) > 0:
                    return True

            if "config_fiscal" in tabelas:
                rows = conn.execute(
                    "SELECT api_key, ambiente, webhook_token_hash FROM config_fiscal"
                ).fetchall()
                if len(rows) > 1 or any(
                    str(api_key or "").strip()
                    or str(token_hash or "").strip()
                    or str(ambiente or "").strip().upper() not in {"", "HOMOLOGACAO"}
                    for api_key, ambiente, token_hash in rows
                ):
                    return True

            if "config_webhook" in tabelas:
                rows = conn.execute("SELECT token_hash FROM config_webhook").fetchall()
                if any(str(token_hash or "").strip() for (token_hash,) in rows):
                    return True

            if "config_sistema" in tabelas:
                rows = conn.execute("SELECT chave, valor FROM config_sistema").fetchall()
                if any(
                    str(chave or "") != "limite_caixa"
                    or str(valor or "").strip() not in {"500", "500.0", "500.00"}
                    for chave, valor in rows
                ):
                    return True

            if "config_taxas" in tabelas:
                for forma, percentual in conn.execute("SELECT tipo, percentual FROM config_taxas"):
                    if str(forma or "").strip().upper() not in {"DEBITO", "CREDITO", "VOUCHER"}:
                        return True
                    if abs(float(percentual or 0.0)) > 0.000001:
                        return True

            if "config_aliquotas_ncm" in tabelas:
                rows = conn.execute(
                    "SELECT ncm_prefixo, aliquota_percentual, descricao, ativo FROM config_aliquotas_ncm"
                ).fetchall()
                if any(
                    str(prefixo or "") != "*"
                    or abs(float(aliquota or 0.0)) > 0.000001
                    or str(descricao or "") != "Aliquota padrao/fallback"
                    or int(ativo or 0) != 1
                    for prefixo, aliquota, descricao, ativo in rows
                ):
                    return True

            if "config_aliquotas_fiscais_ncm" in tabelas:
                rows = conn.execute(
                    "SELECT ncm_prefixo, aliquota_icms, aliquota_pis, aliquota_cofins, "
                    "aliquota_ibs, aliquota_cbs, descricao, ativo FROM config_aliquotas_fiscais_ncm"
                ).fetchall()
                if any(
                    str(row[0] or "") != "*"
                    or any(abs(float(value or 0.0)) > 0.000001 for value in row[1:6])
                    or str(row[6] or "") != "Aliquotas fiscais padrao/fallback"
                    or int(row[7] or 0) != 1
                    for row in rows
                ):
                    return True

            for tabela in tabelas - {"sqlite_sequence"} - set(_TABELAS_OPERACIONAIS) - {
                "config_fiscal", "config_sistema", "config_taxas",
                "config_aliquotas_ncm", "config_aliquotas_fiscais_ncm",
                # Avaliada acima pelo conteúdo: a linha-semente vazia de
                # config_webhook NÃO pode marcar o banco como estado do cliente.
                "config_webhook",
            }:
                if int(conn.execute(f'SELECT COUNT(*) FROM "{tabela}"').fetchone()[0] or 0) > 0:
                    return True
            return False
        finally:
            conn.close()
    except Exception:
        return None


def _adotar_banco_portavel(base_exe, raiz_destino=None, raizes_legadas=None) -> str:
    """Adota uma base operacional em destino ausente ou semente vazia.

    O snapshot usa a API de backup do SQLite e somente leitura na origem. A
    origem nunca é movida/apagada e um destino operacional, inválido ou com
    sidecars nunca é substituído.
    """
    destino_raiz = Path(raiz_destino) if raiz_destino is not None else Path(base_exe) / "data"
    destino = destino_raiz / "mercado.db"
    fontes = list(raizes_legadas or [Path(_base_appdata())])

    destino_existe = destino.exists()
    destino_valido = _sqlite_valido(destino) if destino_existe else False
    if destino_valido:
        estado_destino = _banco_tem_dados_usuario(destino)
        if estado_destino is True:
            return "destino possui estado: não sobrescrever"
        if estado_destino is None:
            return "destino não pôde ser classificado: não tocar"

    candidatas_validas = []
    destino_resolvido = destino.resolve()
    for raiz in fontes:
        candidato = Path(raiz) / "mercado.db"
        try:
            if candidato.resolve() == destino_resolvido:
                continue
        except OSError:
            continue
        if not _sqlite_valido(candidato):
            continue
        total = _total_registros_operacionais(candidato)
        tem_dados = _banco_tem_dados_usuario(candidato)
        if tem_dados is True and total >= 0:
            candidatas_validas.append((candidato, total, _sha256_banco(candidato)))

    if not candidatas_validas:
        if destino_existe and not destino_valido:
            return "destino não é SQLite válido: não tocar"
        return "nenhum banco legado com estado persistente válido"

    if destino_existe and not destino_valido:
        raise RuntimeError(
            "Banco canônico inválido e existe banco operacional legado; "
            "ambos foram preservados para recuperação explícita."
        )

    hashes = {hash_value for _path, _total, hash_value in candidatas_validas}
    if len(hashes) > 1:
        raise RuntimeError(
            "Há bancos legados operacionais divergentes em mais de uma raiz; "
            "nenhum será escolhido automaticamente."
        )
    origem, total_origem, _hash = candidatas_validas[0]

    for sufixo in ("-wal", "-shm", "-journal"):
        if Path(str(destino) + sufixo).exists():
            if candidatas_validas:
                raise RuntimeError(
                    f"Banco canônico possui sidecar {sufixo} e há origem válida; "
                    "nenhum banco foi substituído."
                )
            return f"destino possui sidecar {sufixo}: não adotar"

    if not candidatas_validas:
        return "nenhum banco legado com estado persistente válido"

    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, nome_temporario = tempfile.mkstemp(
        prefix=f"{destino.name}.adocao-",
        suffix=".tmp",
        dir=str(destino.parent),
    )
    os.close(fd)
    temporario = Path(nome_temporario)
    try:
        import sqlite3

        fonte_conn = sqlite3.connect(f"file:{origem}?mode=ro", uri=True)
        try:
            destino_conn = sqlite3.connect(str(temporario))
            try:
                fonte_conn.backup(destino_conn)
            finally:
                destino_conn.close()
        finally:
            fonte_conn.close()

        if not _sqlite_valido(temporario):
            raise RuntimeError("Snapshot SQLite inválido; banco legado original preservado.")
        if _total_registros_operacionais(temporario) != total_origem:
            raise RuntimeError("Contagem do snapshot diverge; banco legado original preservado.")

        # Revalida o destino imediatamente antes da promoção para evitar corrida.
        if destino.exists():
            if not _sqlite_valido(destino) or _banco_tem_dados_usuario(destino) is not False:
                raise RuntimeError("Destino mudou durante adoção; não foi sobrescrito.")
        for sufixo in ("-wal", "-shm", "-journal"):
            if Path(str(destino) + sufixo).exists():
                raise RuntimeError(f"Destino recebeu sidecar {sufixo}; adoção cancelada.")

        os.replace(temporario, destino)
        return f"banco operacional adotado ({total_origem} registros); origem preservada"
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"Falha ao adotar banco operacional; origem preservada ({type(exc).__name__})."
        ) from exc
    finally:
        try:
            temporario.unlink(missing_ok=True)
        except OSError:
            pass


def _adotar_configuracao(raiz_destino, raizes_legadas) -> str:
    destino = Path(raiz_destino) / "config.json"
    dados_destino = _config_json_valida(destino) if destino.exists() else None
    if dados_destino is not None and _config_tem_dados_usuario(dados_destino, destino.parent):
        return "configuração canônica válida: mantida"

    candidatas = []
    for raiz in raizes_legadas:
        origem = Path(raiz) / "config.json"
        try:
            if origem.resolve() == destino.resolve():
                continue
        except OSError:
            continue
        dados = _config_json_valida(origem)
        if dados is not None and _config_tem_dados_usuario(dados, origem.parent):
            candidatas.append((origem, dados))

    if not candidatas:
        return "nenhuma configuração legada válida com dados de usuário"

    assinaturas = {
        _config_assinatura_usuario(dados, origem.parent)
        for origem, dados in candidatas
    }
    if len(assinaturas) > 1:
        raise RuntimeError(
            "Há configurações legadas válidas e divergentes em mais de uma raiz; "
            "nenhuma será escolhida ou sobrescrita automaticamente."
        )

    origem, _dados = max(candidatas, key=lambda par: par[0].stat().st_mtime_ns)
    if destino.exists():
        sufixo_backup = ".pre-adocao.bak" if dados_destino is not None else ".invalid.bak"
        backup = destino.with_name(destino.name + sufixo_backup)
        indice = 1
        while backup.exists():
            backup = destino.with_name(f"{destino.name}{sufixo_backup[:-4]}.{indice}.bak")
            indice += 1
        try:
            shutil.copy2(destino, backup)
        except OSError as exc:
            raise RuntimeError(
                "Configuração canônica não pôde ser preservada em backup; origem mantida."
            ) from exc

    if _copiar_arquivo_atomico(origem, destino, substituir=destino.exists()):
        return f"configuração adotada de {origem.parent}; origem preservada"
    if destino.exists() and _config_tem_dados_usuario(_config_json_valida(destino) or {}, destino.parent):
        return "configuração canônica concorrente válida: mantida"
    raise RuntimeError("Configuração legada válida não pôde ser adotada; origem preservada.")


def _validar_arquivo_persistente(caminho, relativo):
    caminho = Path(caminho)
    try:
        if not caminho.is_file() or caminho.is_symlink() or caminho.stat().st_size <= 0:
            return False
        nome = caminho.name.lower()
        if nome == "client_credentials.sec.json":
            dados = _ler_json_objeto(caminho)
            return bool(
                dados
                and str(dados.get("scheme") or "").lower() in {"plain", "xor-sha256-v1"}
                and dados.get("payload")
            )
        if nome == "credentials.json":
            dados = _ler_json_objeto(caminho)
            if not dados:
                return False
            oauth = dados.get("installed") or dados.get("web")
            return isinstance(oauth, dict) and bool(oauth.get("client_id"))
        if nome == "google-services.json":
            dados = _ler_json_objeto(caminho)
            return bool(
                dados
                and isinstance(dados.get("project_info"), dict)
                and isinstance(dados.get("client"), list)
            )
        if nome in {"firebase-admin-key.json", "firebase-adminsdk.json"} or "firebase-adminsdk" in nome:
            dados = _ler_json_objeto(caminho)
            return bool(
                dados
                and str(dados.get("type") or "").lower() == "service_account"
                and dados.get("client_email")
                and dados.get("private_key")
            )
        if nome == "update_state.json" or caminho.suffix.lower() == ".json":
            return _ler_json_objeto(caminho) is not None
        if nome == "token.pickle":
            return caminho.stat().st_size <= 64 * 1024 * 1024
        if caminho.suffix.lower() == ".db":
            return _sqlite_valido(caminho)
        return True
    except OSError:
        return False


def _adotar_estado_updater(origem, destino, substituir=False):
    dados = _ler_json_objeto(origem)
    if dados is None:
        return False

    estado = str(dados.get("state") or "").upper()
    if estado in {"DOWNLOADING", "STAGED", "WAITING_FOR_EXIT", "BACKING_UP", "INSTALLING", "VERIFYING"}:
        for chave in ("staged_path", "job_path"):
            dados.pop(chave, None)
        dados["state"] = "AVAILABLE" if all(
            dados.get(chave) not in (None, "")
            for chave in ("version", "asset_url", "asset_name", "size", "sha256", "key_id")
        ) else "FAILED"
        dados["error"] = "Atualização precisa ser preparada novamente após a troca da raiz de dados."

    destino = Path(destino)
    if destino.exists() and not substituir:
        return False
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, nome_temporario = tempfile.mkstemp(
        prefix=f".{destino.name}.adocao-",
        dir=str(destino.parent),
        text=True,
    )
    temporario = Path(nome_temporario)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dados, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if destino.exists() and not substituir:
            return False
        os.replace(temporario, destino)
        return True
    finally:
        try:
            temporario.unlink(missing_ok=True)
        except OSError:
            pass


def _adotar_outros_arquivos(raiz_destino, raizes_legadas) -> None:
    destino_raiz = Path(raiz_destino)
    for relativo in _ARQUIVOS_PERSISTENTES:
        destino = destino_raiz / relativo
        destino_valido = _validar_arquivo_persistente(destino, relativo)
        if destino.exists() and destino_valido:
            continue

        candidatas = []
        for raiz in raizes_legadas:
            origem = Path(raiz) / relativo
            if _validar_arquivo_persistente(origem, relativo):
                candidatas.append(origem)

        if not candidatas:
            continue
        hashes = {_sha256_arquivo(origem) for origem in candidatas}
        if len(hashes) > 1 and relativo not in _ARQUIVOS_LOG:
            raise RuntimeError(
                f"Arquivo persistente divergente em várias raízes: {relativo}; "
                "nenhuma versão foi escolhida automaticamente."
            )

        origem = max(candidatas, key=lambda path: path.stat().st_mtime_ns)
        if destino.exists():
            sufixo = ".invalid.bak" if not destino_valido else ".pre-adocao.bak"
            backup = destino.with_name(destino.name + sufixo)
            indice = 1
            while backup.exists():
                backup = destino.with_name(f"{destino.name}{sufixo[:-4]}.{indice}.bak")
                indice += 1
            shutil.copy2(destino, backup)

        if relativo == "update_state.json":
            if not _adotar_estado_updater(origem, destino, substituir=destino.exists()):
                raise RuntimeError("Estado do updater válido não pôde ser adotado.")
        elif not _copiar_arquivo_atomico(origem, destino, substituir=destino.exists()):
            raise RuntimeError(f"Arquivo persistente {relativo} não pôde ser adotado.")

    firebase_candidatas = {}
    for raiz in raizes_legadas:
        for origem in Path(raiz).glob("*firebase-adminsdk*.json"):
            if _validar_arquivo_persistente(origem, origem.name):
                firebase_candidatas.setdefault(origem.name, []).append(origem)
    for nome, candidatas in firebase_candidatas.items():
        destino = destino_raiz / nome
        if _validar_arquivo_persistente(destino, nome):
            continue
        if len({_sha256_arquivo(origem) for origem in candidatas}) > 1:
            raise RuntimeError(f"Credencial Firebase divergente em várias raízes: {nome}.")
        origem = max(candidatas, key=lambda path: path.stat().st_mtime_ns)
        if destino.exists():
            backup = destino.with_name(destino.name + ".invalid.bak")
            indice = 1
            while backup.exists():
                backup = destino.with_name(f"{destino.name}.invalid.{indice}.bak")
                indice += 1
            shutil.copy2(destino, backup)
        if not _copiar_arquivo_atomico(origem, destino, substituir=destino.exists()):
            raise RuntimeError(f"Credencial Firebase {nome} não pôde ser adotada.")

    for raiz_relativa, extensoes in _DIRETORIOS_PERSISTENTES:
        destino_dir = destino_raiz / raiz_relativa
        candidatas_por_relativo = {}
        for indice_raiz, raiz in enumerate(raizes_legadas):
            origem_dir = Path(raiz) / raiz_relativa
            if not origem_dir.is_dir() or origem_dir.is_symlink():
                continue
            for origem in origem_dir.rglob("*"):
                if origem.is_symlink() or not origem.is_file():
                    continue
                if origem.suffix.lower() not in extensoes:
                    continue
                relativo = origem.relative_to(Path(raiz))
                destino = destino_raiz / relativo
                if _validar_arquivo_persistente(origem, relativo):
                    candidatas_por_relativo.setdefault(str(relativo), []).append(origem)

        for relativo, candidatas in candidatas_por_relativo.items():
            destino = destino_raiz / relativo
            if destino.exists():
                continue
            hashes = {_sha256_arquivo(origem) for origem in candidatas}
            if len(hashes) > 1 and raiz_relativa != "logs":
                raise RuntimeError(
                    f"Arquivo de dados divergente em várias raízes: {relativo}; "
                    "nenhuma versão foi escolhida automaticamente."
                )
            origem = max(candidatas, key=lambda path: path.stat().st_mtime_ns)
            if not _copiar_arquivo_atomico(origem, destino):
                raise RuntimeError(f"Arquivo persistente {relativo} não pôde ser adotado.")


def _adotar_dados_protegidos(raiz_destino, raizes_legadas, base_exe=None) -> None:
    global _ADOCAO_AVALIADA
    if _ADOCAO_AVALIADA:
        return

    _adotar_configuracao(raiz_destino, raizes_legadas)
    _adotar_banco_portavel(base_exe, raiz_destino, raizes_legadas)
    _adotar_outros_arquivos(raiz_destino, raizes_legadas)
    _ADOCAO_AVALIADA = True



def _garantir_escrita(path_base: str) -> None:
    """Valida permissão de escrita real no diretório (não apenas existência)."""
    _garantir_diretorio(path_base)
    fd, tmp_path = tempfile.mkstemp(prefix="frs_write_test_", dir=path_base)
    os.close(fd)
    os.remove(tmp_path)


def _versioned_portable_data_base():
    """Resolve data/ compartilhada, inclusive quando executado pelo helper."""
    if not e_compilado():
        return None
    try:
        exe_dir = Path(_base_executavel()).resolve()
        # Abrange tanto runtime/versions/<versao>/FRS_Mercado.exe quanto
        # data/updates/bin/FRS_Mercado_UpdateHelper.exe.
        for app_root in (exe_dir, *exe_dir.parents):
            if (app_root / "runtime").is_dir() and (app_root / "data").is_dir():
                return str(app_root / "data")
    except Exception:
        pass
    return None


def _raiz_dados_preferida():
    """Raiz canônica por modo: AppData para source/instalação, data/ para Portable."""
    if e_compilado():
        base_exe = Path(_base_executavel()).resolve()
        if not _esta_em_program_files(str(base_exe)):
            versioned = _versioned_portable_data_base()
            return Path(versioned) if versioned else base_exe / "data"
    return Path(_base_appdata()).resolve()


def _raizes_legadas(raiz_preferida, base_exe=None):
    """Retorna somente localizações históricas conhecidas para esta instalação."""
    preferida = Path(raiz_preferida).resolve()
    candidatas = [Path(_base_appdata())]

    if e_compilado() and base_exe:
        exe_dir = Path(base_exe).resolve()
        versioned = _versioned_portable_data_base()
        if versioned:
            app_root = Path(versioned).resolve().parent
            versions_dir = app_root / "runtime" / "versions"
            if versions_dir.is_dir():
                candidatas.extend(
                    pasta / "data"
                    for pasta in sorted(versions_dir.iterdir(), key=lambda item: item.name, reverse=True)
                    if pasta.is_dir()
                )
        else:
            candidatas.append(exe_dir / "data")

    unicas = []
    vistas = {os.path.normcase(str(preferida))}
    for candidata in candidatas:
        try:
            resolved = candidata.resolve()
        except OSError:
            resolved = candidata.absolute()
        clave = os.path.normcase(str(resolved))
        if clave not in vistas:
            vistas.add(clave)
            unicas.append(resolved)
    return unicas


def obter_caminho_dados(*partes):
    """
    Retorna caminho dentro da raiz canônica e migra dados conhecidos antes dos defaults.
    """
    raiz_preferida = _raiz_dados_preferida()
    base_exe = Path(_base_executavel()).resolve() if e_compilado() else None
    alternativas = [raiz_preferida]
    raiz_appdata = Path(_base_appdata()).resolve()
    if os.path.normcase(str(raiz_appdata)) != os.path.normcase(str(raiz_preferida)):
        alternativas.append(raiz_appdata)
    ultimo_erro = None
    for base in alternativas:
        # F-4: _garantir_escrita cria a pasta antes da adoção. Se a adoção
        # falhar fechada, um diretório vazio ficaria para trás na raiz tentada.
        # Guardamos o estado previo para poder remover apenas o que foi criado
        # aqui, sem nunca tocar em diretório que já existia com dados.
        try:
            ja_existia = os.path.isdir(base)
            _garantir_escrita(str(base))
        except Exception as e:
            ultimo_erro = e
            continue

        app_dir = str(base)
        try:
            _adotar_dados_protegidos(app_dir, _raizes_legadas(raiz_preferida, base_exe), base_exe)
        except Exception:
            if not ja_existia:
                try:
                    os.rmdir(base)
                except OSError:
                    pass
            raise
        if not partes:
            return app_dir

        destino = Path(app_dir).joinpath(*partes)
        destino.parent.mkdir(parents=True, exist_ok=True)
        return str(destino)

    raise RuntimeError(f"Falha ao resolver caminho de dados: {ultimo_erro}")


def obter_caminho_log(nome_arquivo: str) -> str:
    """Usa sempre a mesma raiz de dados resolvida por obter_caminho_dados()."""
    return obter_caminho_dados(nome_arquivo)
