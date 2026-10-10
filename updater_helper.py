"""Helper externo, seguro e independente da UI para atualizar o FRS Mercado.

A aplicação principal apenas cria um job após validar o gate. Este processo
aguarda o encerramento, revalida manifesto e asset, preserva ``data/``,
instala com rollback e reabre a aplicação somente após sucesso.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app_paths import (
    definir_raiz_dados_explícita,
    e_compilado,
    executavel_atual,
    obter_caminho_dados,
    raiz_dados_explícita,
)
from updater_secure import (
    PUBLIC_KEYS_FILE,
    UpdateSecurityError,
    UpdateState,
    _atomic_json_write,
    _set_state,
    normalize_version,
    read_update_state,
    read_version_marker,
    redefinir_caminhos_apos_raiz,
    verify_manifest,
)

JOB_SCHEMA = "FRS-MERCADO-UPDATE-JOB-V1"
POINTER_SCHEMA = "FRS-MERCADO-RUNTIME-POINTER-V1"
HEALTH_ARGUMENT = "--update-health-check"
PROTECTED_TOP_LEVEL = {"data", "runtime", "updates"}
FORBIDDEN_ASSETS = {
    "credentials.json",
    "google-services.json",
    "token.pickle",
    "client_credentials.sec.json",
    "firebase-admin-key.json",
    "firebase-adminsdk.json",
}
PROTECTED_DATA_NAMES = (
    "mercado.db",
    "mercado.db-wal",
    "mercado.db-shm",
    "mercado.db-journal",
    "config.json",
    "update_state.json",
    # Item 13 / R1: estado do cliente que existia na máquina do Mário mas não
    # estava listado. `integracoes_cofre.json` guarda o cofre de credenciais e
    # `integracoes_config.json` as configurações de iFood/99Entrega; `conversion_backup`
    # preserva conversões de pedido. Entram na proteção — apenas o NOME é
    # listado; conteúdo, formato e credenciais não são tocados.
    "integracoes_cofre.json",
    "integracoes_config.json",
    "conversion_backup",
    "licensing",
    "token.pickle",
    "credentials.json",
    "google-services.json",
    "client_credentials.sec.json",
    "firebase-admin-key.json",
    "firebase-adminsdk.json",
    "log_debug.txt",
    "config_mentora.json",
    "log_integracao.txt",
    "error_log.txt",
    "FRS_Mercado_runtime_error.log",
    "acbrmonitor/ACBrMonitor.ini",
    "assets/produtos",
    "backups",
    "exportacao_fiscal",
    "fiscal_in",
    "fiscal_out",
    "logs",
    "relatorios",
    "templates",
)
PROTECTED_HASH_FILES = (
    "mercado.db",
    "mercado.db-wal",
    "mercado.db-shm",
    "mercado.db-journal",
    "config.json",
    # Item 13 / R1: espelho de PROTECTED_DATA_NAMES na verificação por hash.
    "integracoes_cofre.json",
    "integracoes_config.json",
    "client_credentials.sec.json",
    "token.pickle",
    "credentials.json",
    "google-services.json",
    "firebase-admin-key.json",
    "firebase-adminsdk.json",
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
    "assets/produtos",
    "backups",
    "exportacao_fiscal",
    "fiscal_in",
    "fiscal_out",
    "logs",
    "relatorios",
    "templates",
)


class HelperError(RuntimeError):
    """Falha operacional que deve resultar em rollback."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HelperError(f"Job inválido: {path.name}") from exc
    if not isinstance(value, dict):
        raise HelperError(f"Job inválido: {path.name}")
    return value


def _log_diag_helper(etapa: str, versao, mensagem: str,
                     health_check: str = "", rollback: str = "") -> None:
    """Diagnóstico local do helper. Sanitizado: SEM dados sensíveis.

    Reaproveita o formato de ``updater_secure._log_diagnostico_atualizacao``
    sem importá-lo (evita acoplamento): grava apenas metadados do processo de
    atualização em ``update_diagnostics.log`` dentro da raiz de dados REAL.
    Nunca inclui banco, vendas, cadastro, senhas, tokens ou segredos.
    """
    stamp = _utc_now()
    linha = (
        f"[{stamp}] etapa={etapa} versao={versao} origem=helper\n"
        f"  mensagem: {mensagem}\n"
    )
    if health_check:
        linha += f"  health_check: {health_check}\n"
    if rollback:
        linha += f"  rollback: {rollback}\n"
    try:
        caminho = Path(obter_caminho_dados("update_diagnostics.log"))
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("a", encoding="utf-8") as fh:
            fh.write(linha + ("-" * 60) + "\n")
    except Exception:
        # Diagnóstico jamais pode interromper o fluxo de atualização.
        pass


def _tentar_registrar_falha_raiz(mensagem: str) -> None:
    """Grava falha de raiz de forma segura quando possível.

    Se a raiz explícita foi fixada (caso raro: validação tardia), grava nela.
    Nunca deriva raiz do caminho do executável para registrar erro de raiz —
    é exatamente essa derivação que causa o bug de "data fantasma".
    """
    raiz = raiz_dados_explícita()
    if raiz is None:
        return
    try:
        _log_diag_helper("falha_raiz", None, mensagem)
        estado = Path(obter_caminho_dados("update_state.json"))
        estado.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(estado, {"state": UpdateState.FAILED.value,
                                    "error": mensagem, "updated_at": _utc_now()})
    except Exception:
        pass


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if process:
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_parent_exit(pid: int, timeout: int = 180) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + max(10, int(timeout))
    while _process_alive(pid):
        if time.monotonic() >= deadline:
            raise HelperError("Aplicação principal não encerrou dentro do tempo seguro.")
        time.sleep(0.5)


def _validate_job(job: dict, job_path: Path) -> tuple[dict, Path, Path]:
    if job.get("schema") != JOB_SCHEMA:
        raise HelperError("Schema de job de atualização inválido.")
    release = job.get("release")
    asset_path = Path(str(job.get("asset_path") or "")).resolve()
    if not isinstance(release, dict) or not asset_path.is_file():
        raise HelperError("Job não contém release e asset válidos.")
    envelope = release.get("manifest")
    if not isinstance(envelope, dict):
        raise HelperError("Manifesto assinado ausente no job.")
    payload = verify_manifest(envelope, trusted_keys=None)
    expected_version = normalize_version(release.get("version"))
    if expected_version != normalize_version(payload.get("version")):
        raise HelperError("Versão do job não confere com o manifesto assinado.")
    asset = next(
        (
            item for item in payload.get("assets", [])
            if str(item.get("url")) == str(release.get("asset_url"))
            and str(item.get("asset_type")) == str(release.get("asset_type"))
        ),
        None,
    )
    if not isinstance(asset, dict):
        raise HelperError("Asset não pertence ao manifesto assinado.")
    expected_size = int(asset.get("size") or 0)
    expected_hash = str(asset.get("sha256") or "").lower()
    if asset_path.stat().st_size != expected_size or _sha256(asset_path) != expected_hash:
        raise HelperError("Asset não confere com tamanho/SHA-256 assinado.")
    if job_path.name != "update-job.json" or job_path.parent.name != normalize_version(release.get("version")):
        raise HelperError("Caminho do job fora da pasta versionada de updates.")
    if asset_path.parent != job_path.parent:
        raise HelperError("Asset não está ao lado do job versionado.")
    updates_root = Path(obter_caminho_dados("updates")).resolve()
    if job_path.parent.parent.resolve() != updates_root:
        raise HelperError("Job não está na raiz real de updates.")
    if str(release.get("asset_name") or Path(asset_path).name) != Path(asset_path).name:
        raise HelperError("Nome do asset não confere com o job.")
    declared_job_path = str(job.get("job_path") or "").strip()
    if not declared_job_path or job_path.resolve() != Path(declared_job_path).resolve():
        raise HelperError("Caminho do job não confere.")
    return release, asset_path, job_path


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with zipfile.ZipFile(archive, "r") as package:
        if package.testzip() is not None:
            raise HelperError("ZIP corrompido.")
        for member in package.infolist():
            name = member.filename.replace("\\", "/")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise HelperError("ZIP contém caminho inseguro.")
            top = relative.parts[0].lower() if relative.parts else ""
            if top in PROTECTED_TOP_LEVEL or name.lower() in FORBIDDEN_ASSETS:
                continue
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise HelperError("ZIP contém destino fora da pasta de runtime.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    if not any(path.name.lower() == "frs_mercado.exe" for path in root.rglob("*") if path.is_file()):
        raise HelperError("Runtime extraído não contém FRS_Mercado.exe.")


def _version_marker(root: Path) -> str:
    """Lê a versão do runtime extraído sem depender do nome do pacote.

    Item 12: usa o MESMO leitor e a MESMA precedência de
    `updater_secure.get_local_version`. Antes este método e aquele discordavam
    quando `version.txt` e `version.json` divergiam — o app via uma versão e o
    helper validava outra, o que podia reprovar um runtime correto.
    """
    return read_version_marker(root, allow_release_info=False)


def _write_runtime_pointer(pointer: Path, version: str, runtime: Path) -> None:
    _atomic_json_write(
        pointer,
        {
            "schema": POINTER_SCHEMA,
            "version": version,
            "runtime": str(runtime),
            "updated_at": _utc_now(),
        },
    )


def _installed_version(app_root: Path) -> str:
    """Item 12: mesma precedência de `_version_marker` e `get_local_version`."""
    return read_version_marker(app_root, allow_release_info=False)


def _health_check(executable: Path, expected_version: str | None = None, timeout: int = 45) -> None:
    if not executable.is_file():
        raise HelperError("Executável da nova versão não encontrado.")
    if expected_version:
        root = executable.parent
        if _version_marker(root) != normalize_version(expected_version):
            raise HelperError("Marcador de versão do runtime não confere com o manifesto.")
    completed = subprocess.run(
        [str(executable), HEALTH_ARGUMENT],
        cwd=str(executable.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        raise HelperError(f"Health check da nova versão falhou ({completed.returncode}).")


def _portable_root() -> Path:
    """Resolve a raiz do Portable mesmo quando o helper vive em data/updates/bin.

    1.0.22: a raiz EXPLÍCITA transmitida pelo aplicativo (--data-root) tem
    prioridade absoluta. A heurística por ``runtime/``+``data/`` só roda em
    modo legado (helper chamado sem raiz transmitida).
    """
    raiz = raiz_dados_explícita()
    if raiz is not None:
        # raiz de dados == <app_root>/data (portátil) ou AppData (instalado).
        # A raiz do app é o PAI quando o diretório se chama "data".
        if raiz.name.lower() == "data":
            return raiz.parent
        return raiz
    if e_compilado():
        executavel = executavel_atual()
        current = executavel.parent if executavel is not None else Path(sys.executable).resolve().parent
        for parent in (current, *current.parents):
            if (parent / "runtime").is_dir() and (parent / "data").is_dir():
                return parent
        return current
    return Path(__file__).resolve().parent


def _install_portable(release: dict, asset: Path, app_root: Path, data_dir: Path) -> Path:
    version = normalize_version(release.get("version"))
    runtime_root = app_root / "runtime"
    versions_root = runtime_root / "versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    target = versions_root / version
    if target.exists():
        raise HelperError(f"Runtime da versão {version} já existe; integridade não pode ser garantida.")
    staging = versions_root / f".{version}.installing"
    backup_pointer = runtime_root / "current.previous.json"
    pointer = runtime_root / "current.json"
    if pointer.exists():
        shutil.copy2(pointer, backup_pointer)
    try:
        _set_state(UpdateState.INSTALLING, version=version, error="")
        _safe_extract_zip(asset, staging)
        if _version_marker(staging) != version:
            raise HelperError("Versão extraída não confere com o manifesto assinado.")
        os.replace(staging, target)
        _set_state(UpdateState.VERIFYING, version=version, error="")
        _health_check(target / "FRS_Mercado.exe", expected_version=version)
        _write_runtime_pointer(pointer, version, target)
        return target / "FRS_Mercado.exe"
    except Exception:
        _set_state(UpdateState.ROLLING_BACK, version=version, error="")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup_pointer.exists():
            os.replace(backup_pointer, pointer)
        raise


def _copy_tree_without_data(source: Path, destination: Path, data_dir: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for item in source.iterdir():
        if item.resolve() == data_dir.resolve() or item.name.lower() in {"data", "runtime", "updates"}:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=False)
        else:
            shutil.copy2(item, target)


def _install_traditional(release: dict, asset: Path, app_root: Path) -> tuple[Path, Path]:
    version = normalize_version(release.get("version"))
    backup_root = app_root.parent / f".frs_update_backup_{version}_{int(time.time())}"
    data_dir = Path(obter_caminho_dados())
    _set_state(UpdateState.BACKING_UP, version=version, error="")
    _copy_tree_without_data(app_root, backup_root, data_dir)
    try:
        _set_state(UpdateState.INSTALLING, version=version, error="")
        completed = subprocess.run(
            [str(asset), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            cwd=str(asset.parent),
            timeout=900,
            check=False,
        )
        if completed.returncode != 0:
            raise HelperError(f"Instalador retornou código {completed.returncode}.")
        _set_state(UpdateState.VERIFYING, version=version, error="")
        if _installed_version(app_root) != version:
            raise HelperError("Versão instalada não confere com o manifesto.")
        _health_check(app_root / "FRS_Mercado.exe", expected_version=version)
        return app_root / "FRS_Mercado.exe", backup_root
    except Exception:
        _set_state(UpdateState.ROLLING_BACK, version=version, error="")
        if backup_root.is_dir():
            for item in app_root.iterdir():
                if item.name.lower() not in {"data", "runtime", "updates"}:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            for item in backup_root.iterdir():
                target = app_root / item.name
                if item.is_dir():
                    shutil.copytree(item, target, symlinks=False)
                else:
                    shutil.copy2(item, target)
        raise


def _cleanup_job(path: Path, app_root: Path, data_dir: Path) -> None:
    path.unlink(missing_ok=True)
    for partial in (Path(obter_caminho_dados("updates"))).rglob("*.part"):
        partial.unlink(missing_ok=True)
    for temp in (app_root / "runtime" / "versions").glob(".*.installing"):
        shutil.rmtree(temp, ignore_errors=True)
    for backup in app_root.parent.glob(".frs_update_backup_*"):
        if backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)


def run_job(job_path: Path, reopen: bool = True) -> int:
    data_dir = Path(obter_caminho_dados())
    lock_path = Path(obter_caminho_dados("updates")) / "update.lock"
    app_root = _portable_root()
    with _exclusive_lock(lock_path):
        job = _read_json(job_path)
        release, asset, validated_job = _validate_job(job, job_path)
        _wait_parent_exit(int(job.get("parent_pid") or 0))
        protected_before = _protected_hashes(data_dir)
        runtime_root = app_root / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        data_backup = Path(tempfile.mkdtemp(prefix=f".data-backup-{release.get('version')}-", dir=runtime_root))
        pointer = runtime_root / "current.json"
        previous_pointer = pointer.read_text(encoding="utf-8") if pointer.exists() else None
        installed_runtime = None
        traditional_backup = None
        data_backup_complete = False
        _set_state(UpdateState.BACKING_UP, version=release.get("version"), error="")
        try:
            _backup_protected_data(data_dir, data_backup)
            data_backup_complete = True
            if str(release.get("asset_type")) == "portable_zip":
                executable = _install_portable(release, asset, app_root, data_dir)
                installed_runtime = executable.parent
            else:
                executable, traditional_backup = _install_traditional(release, asset, app_root)
            _assert_protected_data_unchanged(protected_before, data_dir)
            _set_state(
                UpdateState.COMMITTED,
                version=release.get("version"),
                installed_sequence=int(release.get("sequence") or 0),
                error="",
            )
            _cleanup_job(validated_job, app_root, data_dir)
            shutil.rmtree(data_backup, ignore_errors=True)
            if traditional_backup is not None:
                shutil.rmtree(traditional_backup, ignore_errors=True)
            if reopen:
                subprocess.Popen([str(executable)], cwd=str(executable.parent))
            return 0
        except Exception as exc:
            _set_state(UpdateState.ROLLING_BACK, version=release.get("version"), error=str(exc))
            if previous_pointer is not None:
                _atomic_json_write(pointer, json.loads(previous_pointer))
            elif pointer.exists():
                pointer.unlink(missing_ok=True)
            if installed_runtime is not None and installed_runtime.exists():
                shutil.rmtree(installed_runtime, ignore_errors=True)
            if traditional_backup is not None and traditional_backup.is_dir():
                for item in app_root.iterdir():
                    if item.name.lower() not in {"data", "runtime", "updates"}:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                for item in traditional_backup.iterdir():
                    target = app_root / item.name
                    if item.is_dir():
                        shutil.copytree(item, target, symlinks=False)
                    else:
                        shutil.copy2(item, target)
            if data_backup_complete:
                _restore_protected_data(data_dir, data_backup)
            shutil.rmtree(data_backup, ignore_errors=True)
            _set_state(UpdateState.ROLLED_BACK, version=release.get("version"), error=str(exc))
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helper seguro de atualização FRS Mercado")
    parser.add_argument("--job", required=True)
    parser.add_argument("--no-reopen", action="store_true")
    parser.add_argument(
        "--data-root",
        default="",
        help="Raiz de dados canônica do aplicativo. Tem prioridade sobre qualquer "
        "heurística baseada no caminho deste executável.",
    )
    args = parser.parse_args(argv)

    # ------------------------------------------------------------------
    # Raiz de dados explícita (correção 1.0.22).
    # Deve ocorrer ANTES de qualquer chamada a obter_caminho_dados() para que
    # estado, lock, staging, rollback e diagnóstico caiam na MESMA raiz do app.
    # ------------------------------------------------------------------
    origem = "argumento"
    candidato = str(args.data_root or "").strip()
    if not candidato:
        candidato = os.environ.get("FRS_MERCADO_DATA_ROOT", "").strip()
        origem = "ambiente"
    raiz_transmitida = candidato
    try:
        if candidato:
            raiz = definir_raiz_dados_explícita(candidato, origem=origem)
            # Reajusta os caminhos de estado/lock que foram congelados no import
            # de updater_secure ANTES da raiz explícita ser conhecida.
            redefinir_caminhos_apos_raiz()
        else:
            # Compatibilidade: app antigo que não transmite a raiz. Adota a
            # heurística, mas REGISTRA o risco para diagnóstico.
            raiz = None
    except ValueError as exc:
        # Raiz inconsistente: NÃO prosseguir. Grava falha de forma segura na
        # raiz real do aplicativo quando ela puder ser determinada sem heurística
        # derivada do executável; caso contrário apenas reporta no stderr.
        mensagem = f"Raiz de dados transmitida inválida: {exc}"
        _tentar_registrar_falha_raiz(mensagem)
        sys.stderr.write(f"ERRO: {mensagem}\n")
        return 1

    try:
        if raiz is not None:
            _log_diag_helper(
                "raiz_validada",
                None,
                f"origem={origem}; raiz={raiz}; transmitida={raiz_transmitida}",
            )
        else:
            _log_diag_helper(
                "raiz_nao_transmitida",
                None,
                "helper chamado sem --data-root/FRS_MERCADO_DATA_ROOT; "
                "usando heurística do executável (modo legado).",
            )
        return run_job(Path(args.job).resolve(), reopen=not args.no_reopen)
    except Exception as exc:
        _set_state(UpdateState.FAILED, error=str(exc))
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise HelperError("Outro processo de atualização já está em execução.") from exc
    try:
        handle.write(json.dumps({"pid": os.getpid(), "created_at": _utc_now()}))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            handle.close()
        finally:
            path.unlink(missing_ok=True)


def _protected_hashes(data_dir: Path) -> dict[str, str]:
    hashes = {}
    for name in PROTECTED_HASH_FILES:
        path = data_dir / name
        if path.is_symlink():
            hashes[str(path.relative_to(data_dir))] = "SYMLINK"
        elif path.is_file():
            hashes[str(path.relative_to(data_dir))] = _sha256(path)
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_symlink():
                    hashes[str(child.relative_to(data_dir))] = "SYMLINK"
                elif child.is_file():
                    hashes[str(child.relative_to(data_dir))] = _sha256(child)
    return hashes


def _backup_protected_data(data_dir: Path, backup_dir: Path) -> None:
    """Cria snapshot dos dados que jamais podem ser substituídos pelo update."""
    if not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=False)
    elif any(backup_dir.iterdir()):
        raise HelperError("Diretório de backup de dados não está vazio.")
    for name in PROTECTED_DATA_NAMES:
        source = data_dir / name
        if not source.exists():
            continue
        if source.is_symlink():
            raise HelperError(f"Arquivo protegido é symlink e não pode ser snapshotado: {name}.")
        target = backup_dir / name
        if source.is_dir():
            if any(child.is_symlink() for child in source.rglob("*")):
                raise HelperError(f"Diretório protegido contém symlink: {name}.")
            shutil.copytree(source, target, symlinks=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _restore_protected_data(data_dir: Path, backup_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in PROTECTED_DATA_NAMES:
        current = data_dir / name
        saved = backup_dir / name
        if current.is_dir() and not current.is_symlink():
            shutil.rmtree(current, ignore_errors=True)
        elif current.exists():
            current.unlink(missing_ok=True)
        if saved.is_dir():
            shutil.copytree(saved, current, symlinks=False)
        elif saved.is_file():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)


def _assert_protected_data_unchanged(before: dict[str, str], data_dir: Path) -> None:
    after = _protected_hashes(data_dir)
    if before != after:
        raise HelperError("A instalação alterou dados protegidos do usuário; rollback será executado.")


if __name__ == "__main__":
    raise SystemExit(main())
