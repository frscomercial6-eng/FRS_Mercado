import json
import os
import sys
from urllib import request

from updater import compare_versions, fetch_manifest_payload, normalize_version


def _simular_download(download_url: str) -> int:
    token = os.getenv("GITHUB_TOKEN", "").strip() or os.getenv("GH_TOKEN", "").strip()
    headers = {
        "User-Agent": "FRS-Mercado-UpdateFlowTest",
        "Accept": "application/octet-stream",
        "Range": "bytes=0-4095",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(
        download_url,
        headers=headers,
        method="GET",
    )
    with request.urlopen(req, timeout=20) as resp:
        blob = resp.read(4096)
    if not blob:
        raise RuntimeError("Download de teste retornou vazio.")
    return len(blob)


def main() -> int:
    repo = "frscomercial6-eng/FRS_Mercado"
    local_forcada = "1.0.3"

    payload = fetch_manifest_payload(repo)
    if not payload:
        print("[FALHA] Não foi possível ler version.json no GitHub.")
        return 1

    remoto = normalize_version(payload.get("latest_version"))
    download_url = str(payload.get("download_url") or "").strip()

    if remoto != "1.0.4":
        print(f"[FALHA] latest_version inesperada no manifesto: {remoto}")
        return 1

    if compare_versions(remoto, local_forcada) <= 0:
        print(f"[FALHA] O sistema não enxergou versão nova: local={local_forcada}, remota={remoto}")
        return 1

    try:
        bytes_recebidos = _simular_download(download_url)
    except Exception as exc:
        print(f"[FALHA] Simulação de download não concluiu: {exc}")
        return 1

    resultado = {
        "local_forcada": local_forcada,
        "remota_manifesto": remoto,
        "nova_versao_detectada": True,
        "download_url": download_url,
        "bytes_lidos_teste": bytes_recebidos,
    }
    print("[OK] Fluxo de update validado com sucesso")
    print(json.dumps(resultado, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
