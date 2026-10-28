# -*- coding: utf-8 -*-
"""MESTRE BUILD OFICIAL - FRS MERCADO
Ponto de entrada unico para auditoria, testes, compilacao, empacotamento portatil,
instalador oficial Inno Setup e validacao de artefatos da versao 1.0.25.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = ROOT_DIR.parent.parent

APP_EXE_NAME = "FRS_Mercado.exe"
HELPER_EXE_NAME = "FRS_Mercado_UpdateHelper.exe"
SETUP_EXE_NAME = "FRS_Mercado_Setup.exe"


def _validate_security_files():
    required_files = {
        "login local": ROOT_DIR / "modulo_login.py",
        "licenca assinada": ROOT_DIR / "license_manager.py",
        "verificador": ROOT_DIR / "licensing" / "license_verifier.py",
        "identidade de maquina": ROOT_DIR / "licensing" / "machine_identity.py",
        "atualizador seguro": ROOT_DIR / "updater_secure.py",
        "helper externo": ROOT_DIR / "updater_helper.py",
        "chaves publicas de licenca": ROOT_DIR / "licensing" / "trusted_keys.json",
        "chaves publicas do updater": ROOT_DIR / "updater_public_keys.json",
    }
    missing_files = [str(path) for path in required_files.values() if not path.is_file()]
    if missing_files:
        raise RuntimeError("Infraestrutura de seguranca ausente: " + str(missing_files))

    login_path = required_files["login local"]
    with open(login_path, "r", encoding="utf-8") as f:
        login_src = f.read()

    required_login_markers = (
        "estritamente local",
        "O login nunca consulta o",
        "SELECT id, nome, permissao FROM usuarios",
    )
    missing_login = [marker for marker in required_login_markers if marker not in login_src]
    if missing_login:
        raise RuntimeError("Login local nao passou na validacao de seguranca: " + str(missing_login))

    for fpath in required_files.values():
        if fpath == login_path:
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            src = f.read()

    try:
        license_keys = json.loads(required_files["chaves publicas de licenca"].read_text(encoding="utf-8")).get("keys", {})
        updater_keys = json.loads(required_files["chaves publicas do updater"].read_text(encoding="utf-8")).get("keys", {})
    except Exception as exc:
        raise RuntimeError("Arquivos de chaves publicas estao ausentes ou invalidos.") from exc

    if not isinstance(license_keys, dict) or not license_keys:
        raise RuntimeError("Configure ao menos uma chave publica oficial de licenca antes do build.")
    if not isinstance(updater_keys, dict) or not updater_keys:
        raise RuntimeError("Configure ao menos uma chave publica oficial do updater antes do build.")

    private_markers = ("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "BEGIN EC PRIVATE KEY")
    client_files = list(required_files.values()) + [ROOT_DIR / "gerador_licenca.py"]
    for path in client_files:
        if path.suffix not in {".py", ".json"}:
            continue
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        if any(marker in source for marker in private_markers):
            raise RuntimeError("Material de chave privada nao pode estar no cliente: " + path.name)


def _check_dependencies():
    import importlib
    missing = []
    for mod_name in ["customtkinter", "PyInstaller", "psutil", "cryptography"]:
        try:
            importlib.import_module(mod_name)
        except ImportError:
            missing.append(mod_name)
    if missing:
        raise RuntimeError("Dependencias faltando: " + ", ".join(missing))


def _check_pyinstaller_environment():
    import PyInstaller.utils.cliutils.archive_viewer as av


def _find_iscc() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise RuntimeError("Inno Setup 6 (ISCC.exe) nao encontrado no sistema.")


def _clean_previous_builds():
    """Limpa artefatos de builds anteriores para garantir integridade nova."""
    print("Limpando artefatos de builds anteriores...")
    for rel_dir in ["dist", "_build_temp", "portable_build", "installer"]:
        target = ROOT_DIR / rel_dir
        if target.exists() and target.is_dir():
            try:
                shutil.rmtree(target)
            except Exception as exc:
                print(f"  Aviso: Nao foi possivel remover {target}: {exc}")
    (ROOT_DIR / "installer").mkdir(parents=True, exist_ok=True)


def _ensure_windows_version_file(app_version):
    parts = app_version.split(".")
    while len(parts) < 4:
        parts.append("0")
    version_tuple = ", ".join(parts[:4])

    lines = [
        "VSVersionInfo(",
        "    ffi=FixedFileInfo(",
        f"        filevers=({version_tuple}),",
        f"        prodvers=({version_tuple}),",
        "        mask=0x3f,",
        "        flags=0x0,",
        "        OS=0x40004,",
        "        fileType=0x1,",
        "        subtype=0x0,",
        "        date=(0, 0)",
        "    ),",
        "    kids=[",
        "        StringFileInfo(",
        "            [StringTable(",
        "                '040904B0',",
        "                [",
        "                    StringStruct('CompanyName', 'FRS Solutions'),",
        "                    StringStruct('FileDescription', 'FRS Mercado'),",
        f"                    StringStruct('FileVersion', '{app_version}.0'),",
        "                    StringStruct('InternalName', 'FRS_Mercado'),",
        "                    StringStruct('OriginalFilename', 'FRS_Mercado.exe'),",
        "                    StringStruct('ProductName', 'FRS Mercado'),",
        f"                    StringStruct('ProductVersion', '{app_version}.0')",
        "                ]",
        "            )]",
        "        ),",
        "        VarFileInfo([VarStruct('Translation', [1033, 1200])])",
        "    ]",
        ")",
    ]
    content = "\n".join(lines)
    version_file = ROOT_DIR / "version_info"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(content, encoding="utf-8")
    return version_file


def _resolve_customtkinter_assets_dir():
    import importlib.util
    spec = importlib.util.find_spec("customtkinter")
    if spec is None:
        return None
    origin = getattr(spec, "origin", None)
    if not origin:
        return None
    package_dir = Path(origin).resolve().parent
    assets_dir = package_dir / "assets"
    if assets_dir.exists() and assets_dir.is_dir():
        return assets_dir
    return None


def _build_main_exe(version: str) -> Path:
    """Compila FRS_Mercado.exe via PyInstaller em modo onedir em dist/FRS_Mercado."""
    print("\n--- Compilando executavel principal (FRS_Mercado.exe) ---")
    version_file = _ensure_windows_version_file(version)
    main_py = ROOT_DIR / "main.py"
    if not main_py.exists():
        raise FileNotFoundError("main.py nao encontrado.")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name=FRS_Mercado",
        f"--version-file={version_file}",
        f"--distpath={ROOT_DIR / 'dist'}",
        f"--workpath={ROOT_DIR / '_build_temp'}",
        f"--specpath={ROOT_DIR}",
    ]

    ctk_assets = _resolve_customtkinter_assets_dir()
    if ctk_assets:
        cmd.append(f"--add-data={ctk_assets};customtkinter/assets")

    version_txt = ROOT_DIR / "version.txt"
    if version_txt.is_file():
        cmd.append(f"--add-data={version_txt};.")

    assets_dir = ROOT_DIR / "assets"
    if assets_dir.is_dir():
        cmd.append(f"--add-data={assets_dir};assets")

    logo_ico = ROOT_DIR / "assets" / "logo.ico"
    if logo_ico.is_file():
        cmd.append(f"--icon={logo_ico}")

    hidden_imports = [
        "modulo_pdv", "modulo_login", "modulo_main", "modulo_relatorio",
        "modulo_config", "modulo_estoque", "database_manager", "app_paths",
        "license_manager", "updater_secure", "error_notifier",
        "customtkinter", "PIL", "reportlab", "cryptography",
    ]
    for h in hidden_imports:
        cmd.extend(["--hidden-import", h])

    cmd.append(str(main_py))

    print(f"Executando PyInstaller principal: {' '.join(cmd[:10])}...")
    log_file = ROOT_DIR / "_build_temp" / "pyinstaller_main.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8", errors="replace") as f_out:
        proc = subprocess.run(cmd, cwd=str(ROOT_DIR), stdout=f_out, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        print(f"PyInstaller principal falhou (codigo {proc.returncode}). Ultimas linhas do log:")
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f_in:
                lines = f_in.readlines()
                print("".join(lines[-30:]))
        except Exception:
            pass

    exe_path = ROOT_DIR / "dist" / "FRS_Mercado" / APP_EXE_NAME
    if not exe_path.exists():
        raise RuntimeError(f"Executavel principal nao foi gerado em: {exe_path}")
    print(f"Executavel principal gerado com sucesso: {exe_path} ({exe_path.stat().st_size} bytes)")
    return exe_path


def _build_update_helper(version: str) -> Path:
    """Compila o helper externo de atualizacao (FRS_Mercado_UpdateHelper.exe)."""
    print("\n--- Compilando FRS_Mercado_UpdateHelper.exe ---")
    helper_entry = ROOT_DIR / "updater_helper.py"
    if not helper_entry.is_file():
        raise FileNotFoundError(f"Helper ausente: {helper_entry}")

    helper_dist = ROOT_DIR / "installer" / "UpdateHelper"
    helper_work = ROOT_DIR / "_build_temp" / "UpdateHelper"
    helper_spec = ROOT_DIR / "_build_temp" / "UpdateHelperSpec"
    version_file = _ensure_windows_version_file(version)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name=FRS_Mercado_UpdateHelper",
        f"--distpath={helper_dist}",
        f"--workpath={helper_work}",
        f"--specpath={helper_spec}",
        f"--paths={ROOT_DIR}",
        f"--add-data={ROOT_DIR / 'updater_public_keys.json'};.",
        f"--version-file={version_file}",
        "--collect-submodules=licensing",
        "--collect-all=cryptography",
        str(helper_entry),
    ]

    log_file = ROOT_DIR / "_build_temp" / "pyinstaller_helper.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8", errors="replace") as f_out:
        proc = subprocess.run(cmd, cwd=str(ROOT_DIR), stdout=f_out, stderr=subprocess.STDOUT)
    produced = helper_dist / HELPER_EXE_NAME
    if not produced.is_file():
        print(f"Erro helper (codigo {proc.returncode}). Ultimas linhas do log:")
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f_in:
                lines = f_in.readlines()
                print("".join(lines[-30:]))
        except Exception:
            pass
        raise FileNotFoundError(f"Helper de atualizacao nao foi gerado em: {produced}")

    dest = ROOT_DIR / "dist" / "FRS_Mercado" / HELPER_EXE_NAME
    shutil.copy2(produced, dest)
    print(f"Helper seguro gerado e copiado para: {dest} ({dest.stat().st_size} bytes)")
    return dest


def _create_portable_package(version: str) -> Path:
    """Monta a versao portatil e gera o ZIP oficial em installer/."""
    print("\n--- Montando pacote portatil oficial ---")
    portable_dir = ROOT_DIR / "portable_build"
    installer_dir = ROOT_DIR / "installer"
    if portable_dir.exists():
        shutil.rmtree(portable_dir)
    portable_dir.mkdir(parents=True, exist_ok=True)

    app_dist = ROOT_DIR / "dist" / "FRS_Mercado"
    if not app_dist.exists():
        raise FileNotFoundError(f"Diretorio compilado dist/FRS_Mercado nao encontrado: {app_dist}")

    for item in app_dist.rglob("*"):
        rel = item.relative_to(app_dist)
        dest = portable_dir / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)

    # Copia assets
    for asset_name in ("logo.ico", "frsMercado.ico", "frsMercado.jpeg"):
        src = ROOT_DIR / "assets" / asset_name
        if src.is_file():
            (portable_dir / "assets").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, portable_dir / "assets" / asset_name)

    # Copia metadados e EULA
    shutil.copy2(ROOT_DIR / "version.txt", portable_dir / "version.txt")
    shutil.copy2(ROOT_DIR / "EULA.txt", portable_dir / "EULA.txt")

    # Copia chaves publicas
    (portable_dir / "licensing").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT_DIR / "licensing" / "trusted_keys.json", portable_dir / "licensing" / "trusted_keys.json")
    shutil.copy2(ROOT_DIR / "updater_public_keys.json", portable_dir / "updater_public_keys.json")

    # Seed banco limpo no Portable (sem dados de clientes)
    data_dir = portable_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_clean_path = data_dir / "mercado.db"
    if db_clean_path.exists():
        db_clean_path.unlink()
    
    staging_db = ROOT_DIR / "_build_staging" / "mercado_1.0.19_distribuicao_limpa.db"
    if staging_db.is_file():
        shutil.copy2(staging_db, db_clean_path)
    else:
        conn = sqlite3.connect(str(db_clean_path))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT);")
        conn.commit()
        conn.close()

    # Copiar instalador ACBr no portatil se presente
    acbr_src = ROOT_DIR / "_build_support" / "acbr" / "ACBrMonitor_Installer.exe"
    if acbr_src.is_file():
        (portable_dir / "instala").mkdir(parents=True, exist_ok=True)
        shutil.copy2(acbr_src, portable_dir / "instala" / "ACBrMonitor_Installer.exe")

    # Compactar ZIP oficial
    zip_path = installer_dir / f"FRS_Mercado_Portable_{version}.zip"
    if zip_path.exists():
        zip_path.unlink()

    print(f"Compactando pacote portatil para: {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(portable_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(portable_dir))

    print(f"Pacote portatil gerado com sucesso: {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def _build_inno_setup_installer(version: str) -> Path:
    """Executa o Inno Setup oficial compilando setup_frs.iss."""
    print("\n--- Executando Inno Setup (setup_frs.iss) ---")
    iscc = _find_iscc()
    iss_file = ROOT_DIR / "setup_frs.iss"
    if not iss_file.is_file():
        raise FileNotFoundError(f"Arquivo de configuracao Inno Setup nao encontrado: {iss_file}")

    # Garante que _build_support/data exista para o setup_frs.iss
    support_data = ROOT_DIR / "_build_support" / "data"
    support_data.mkdir(parents=True, exist_ok=True)

    cmd = [str(iscc), str(iss_file)]
    print(f"Executando comando: {' '.join(cmd)}")
    log_file = ROOT_DIR / "_build_temp" / "inno_setup.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8", errors="replace") as f_out:
        proc = subprocess.run(cmd, cwd=str(ROOT_DIR), stdout=f_out, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        print(f"Inno Setup falhou com codigo {proc.returncode}. Ultimas linhas do log:")
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f_in:
                lines = f_in.readlines()
                print("".join(lines[-30:]))
        except Exception:
            pass
        raise RuntimeError(f"Inno Setup falhou com codigo {proc.returncode}")

    installer_exe = ROOT_DIR / "installer" / SETUP_EXE_NAME
    if not installer_exe.is_file():
        raise FileNotFoundError(f"Instalador Inno Setup nao foi gerado em: {installer_exe}")

    print(f"Instalador gerado com sucesso: {installer_exe} ({installer_exe.stat().st_size} bytes)")
    return installer_exe


def _calculate_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _github_headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": "FRS-Mercado-MestreBuild/1.0",
    }


def _gh_api(token: str, method: str, path: str, data: bytes | None = None,
            content_type: str | None = None) -> dict:
    url = f"https://api.github.com{path}"
    headers = _github_headers(token)
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            if not body:
                return {}
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} falhou (HTTP {exc.code}): {body}") from exc


def _generate_signed_manifest(version: str, sequence: int, artefatos: list) -> Path:
    private_key = os.environ.get("FRS_UPDATER_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError(
            "FRS_UPDATER_PRIVATE_KEY nao esta definida. "
            "O manifesto assinado nao pode ser gerado sem a chave privada oficial v2."
        )

    manifest_out = ROOT_DIR / f"update-manifest-{version}.json"
    assets_args = []
    for label, path in artefatos:
        if label == "Pacote Portatil":
            asset_type = "portable_zip"
        elif label == "Instalador Oficial":
            asset_type = "installer_exe"
        elif label == "Helper de Atualizacao":
            asset_type = "traditional_exe"
        else:
            continue
        url = (
            f"https://github.com/frscomercial6-eng/FRS_Mercado/"
            f"releases/download/v{version}/{path.name}"
        )
        assets_args.extend(["--asset", asset_type, url, str(path)])

    cmd = [
        sys.executable, str(ROOT_DIR / "release_manifest.py"),
        "--version", version,
        "--sequence", str(sequence),
        "--key-id", "frs-mercado-updater-official-v2",
        "--out", str(manifest_out),
    ] + assets_args

    print(f"\n--- Gerando manifesto assinado (release_manifest.py) ---")
    proc = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError("Falha ao gerar o manifesto assinado do atualizador.")
    print(proc.stdout.strip())

    check_cmd = [
        sys.executable, str(ROOT_DIR / "release_manifest.py"),
        "--check-only", "--in", str(manifest_out),
    ]
    proc = subprocess.run(check_cmd, cwd=str(ROOT_DIR), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise RuntimeError("O manifesto assinado nao passou na verificacao interna.")
    print(proc.stdout.strip())
    return manifest_out


def _publish_release_and_assets(token: str, version: str, commit_sha: str,
                                 artefatos: list, manifest_path: Path | None = None) -> dict:
    repo = "frscomercial6-eng/FRS_Mercado"
    tag_name = f"v{version}"
    release_name = f"FRS Mercado v{version}"
    body = (
        f"FRS Mercado v{version} - Release oficial\r\n\r\n"
        f"Build oficial da versao {version} gerado pelo Mestre Build (commit {commit_sha}).\r\n\r\n"
        f"Artefatos desta release:\r\n"
    )
    for label, path in artefatos:
        body += f"- {path.name} - {label}\r\n"
    if manifest_path:
        body += f"- {manifest_path.name} - manifesto assinado do atualizador\r\n"

    release_data = _gh_api(token, "POST", f"/repos/{repo}/releases",
                           data=json.dumps({
                               "tag_name": tag_name,
                               "target_commitish": "main",
                               "name": release_name,
                               "body": body,
                               "draft": False,
                               "prerelease": False,
                               "generate_release_notes": False,
                           }).encode("utf-8"),
                           content_type="application/json")
    release_id = release_data.get("id")
    upload_url = release_data.get("upload_url", "")
    if upload_url:
        upload_url = upload_url.replace("{?name,label}", "")

    existing_assets = _gh_api(token, "GET", f"/repos/{repo}/releases/{release_id}/assets")
    existing_names = {a.get("name") for a in existing_assets}

    all_files = list(artefatos)
    if manifest_path:
        all_files.append(("Manifesto Assinado", manifest_path))

    for label, path in all_files:
        if path.name in existing_names:
            print(f"  Asset ja presente na release, pulando: {path.name}")
            continue
        asset_upload_url = f"{upload_url}?name={path.name}"
        headers = _github_headers(token, content_type="application/octet-stream")
        headers["Content-Length"] = str(path.stat().st_size)
        with open(path, "rb") as f:
            file_data = f.read()
        req = urllib.request.Request(asset_upload_url, data=file_data,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            print(f"  Upload OK: {result.get('name')} ({result.get('size')} bytes, state={result.get('state')})")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Upload do asset {path.name} falhou (HTTP {exc.code}): {body}") from exc

    return release_data


def main():
    print("=" * 70)
    print("MESTRE BUILD OFICIAL - FRS MERCADO v1.0.25")
    print("=" * 70)
    print(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Diretorio raiz: {ROOT_DIR}")
    print(f"Python em uso : {sys.executable} ({sys.version.split()[0]})")
    print()

    try:
        # 1. Validacao de seguranca e dependencias
        _validate_security_files()
        _check_dependencies()
        _check_pyinstaller_environment()

        # 2. Sincronizacao oficial de release artifacts
        from release_manager import prepare_release_artifacts, read_version
        version = read_version()
        if version != "1.0.25":
            raise ValueError(f"Versao em version.txt e '{version}', esperada '1.0.25'.")
        prepare_release_artifacts()
        print(f"Artefatos sincronizados para versao: {version}")

        # 3. Limpeza de artefatos anteriores
        _clean_previous_builds()

        # 4. Compilacao do executavel principal
        main_exe = _build_main_exe(version)

        # 5. Compilacao do helper de atualizacao
        helper_exe = _build_update_helper(version)

        # 6. Geracao do pacote portatil (.zip)
        portable_zip = _create_portable_package(version)

        # 7. Compilacao do instalador oficial Inno Setup
        setup_exe = _build_inno_setup_installer(version)

        # 8. Auditoria final dos artefatos gerados
        artefatos = [
            ("Executavel Principal", main_exe),
            ("Helper de Atualizacao", helper_exe),
            ("Instalador Oficial", setup_exe),
            ("Pacote Portatil", portable_zip),
        ]

        print("\n" + "=" * 70)
        print("RELATORIO FINAL DE ARTEFATOS - MESTRE BUILD v1.0.25")
        print("=" * 70)
        resumo = {}
        for label, path in artefatos:
            size_bytes = path.stat().st_size
            sha256 = _calculate_sha256(path)
            resumo[label] = {
                "caminho": str(path),
                "tamanho_bytes": size_bytes,
                "tamanho_mb": round(size_bytes / (1024 * 1024), 2),
                "sha256": sha256,
            }
            print(f"[{label}]")
            print(f"  Arquivo: {path.name}")
            print(f"  Caminho: {path}")
            print(f"  Tamanho: {size_bytes:,} bytes ({round(size_bytes / (1024 * 1024), 2)} MB)")
            print(f"  SHA-256: {sha256}\n")

        # 9. Geracao do manifesto assinado do atualizador (fail-closed sem chave)
        manifest_path = _generate_signed_manifest(version, sequence=7, artefatos=artefatos)

        # 10. Publicacao da release no GitHub (requer GITHUB_TOKEN)
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not github_token:
            print("\nAVISO: GITHUB_TOKEN nao definido. Pulando publicacao no GitHub.")
            print("  Para publicar, defina GITHUB_TOKEN e reexecute o mestre build.")
            print("=" * 70)
            print("BUILD OFICIAL v1.0.25 CONCLUIDO (publicacao pendente de GITHUB_TOKEN)!")
            print("=" * 70)
            return 0

        # Obtem o commit SHA atual do HEAD para associar à release
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT_DIR),
                              capture_output=True, text=True)
        commit_sha = proc.stdout.strip() if proc.returncode == 0 else "desconhecido"

        release_data = _publish_release_and_assets(
            token=github_token, version=version, commit_sha=commit_sha,
            artefatos=artefatos, manifest_path=manifest_path,
        )
        print(f"\nRelease publicada: {release_data.get('html_url')}")

        print("=" * 70)
        print("BUILD OFICIAL v1.0.25 CONCLUIDO COM SUCESSO (incluindo publicacao)!")
        print("=" * 70)
        return 0

    except Exception as exc:
        print(f"\nERRO NO MESTRE BUILD: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
