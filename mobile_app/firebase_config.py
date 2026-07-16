import os
import sys
import json
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

from client_credentials_store import load_client_credentials


_FIREBASE_APP = None


def _base_exec_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_service_account_json(caminho: Path) -> bool:
    try:
        if not caminho.exists() or not caminho.is_file() or caminho.suffix.lower() != ".json":
            return False
        payload = json.loads(caminho.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        return (
            str(payload.get("type") or "").strip().lower() == "service_account"
            and bool(str(payload.get("client_email") or "").strip())
            and bool(str(payload.get("private_key") or "").strip())
        )
    except Exception:
        return False


def _resolver_arquivo_credenciais() -> Path:
    candidatos = []
    credenciais_seguras = load_client_credentials()

    caminho_seguro = str(
        credenciais_seguras.get("firebase_admin_key_path")
        or credenciais_seguras.get("google_oauth_credentials_path")
        or ""
    ).strip()
    if caminho_seguro:
        candidatos.append(Path(caminho_seguro).expanduser())

    env_path = str(os.getenv("FIREBASE_ADMIN_KEY_PATH", "") or "").strip()
    if env_path:
        candidatos.append(Path(env_path).expanduser())

    base = _base_exec_dir()
    modulo_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()

    candidatos.append(base / "firebase-admin-key.json")
    candidatos.append(base / "assets" / "firebase-admin-key.json")
    candidatos.append(modulo_dir / "firebase-admin-key.json")
    candidatos.append(modulo_dir / "assets" / "firebase-admin-key.json")
    candidatos.append(cwd / "firebase-admin-key.json")
    candidatos.append(cwd / "assets" / "firebase-admin-key.json")

    candidatos.extend(sorted(base.glob("*firebase-adminsdk*.json")))
    candidatos.extend(sorted(modulo_dir.glob("*firebase-adminsdk*.json")))
    candidatos.extend(sorted(cwd.glob("*firebase-adminsdk*.json")))

    for json_file in sorted(base.glob("*.json")):
        if _is_service_account_json(json_file):
            candidatos.append(json_file)
    for json_file in sorted(modulo_dir.glob("*.json")):
        if _is_service_account_json(json_file):
            candidatos.append(json_file)
    for json_file in sorted(cwd.glob("*.json")):
        if _is_service_account_json(json_file):
            candidatos.append(json_file)

    vistos = set()
    for caminho in candidatos:
        try:
            key = str(caminho.resolve()).lower()
        except Exception:
            key = str(caminho).lower()
        if key in vistos:
            continue
        vistos.add(key)

        if caminho.exists() and caminho.is_file():
            return caminho.resolve()

    raise FileNotFoundError(
        "Credencial Firebase nao encontrada. Defina FIREBASE_ADMIN_KEY_PATH ou adicione firebase-admin-key.json na raiz."
    )


def inicializar_firebase():
    global _FIREBASE_APP

    if _FIREBASE_APP is not None:
        return _FIREBASE_APP

    cred_path = _resolver_arquivo_credenciais()

    if firebase_admin._apps:
        _FIREBASE_APP = firebase_admin.get_app()
        return _FIREBASE_APP

    cred = credentials.Certificate(str(cred_path))
    _FIREBASE_APP = firebase_admin.initialize_app(cred)
    return _FIREBASE_APP


def obter_firestore_client():
    inicializar_firebase()
    _ensure_market_namespace_mobile()
    return firestore.client()


def _market_id_from_local_config() -> str:
    cfg_path = Path(os.getenv("APPDATA") or Path.home()) / "FRS_Mercado" / "data" / "config.json"
    if not cfg_path.exists():
        return ""
    try:
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return str(payload.get("market_id") or "").strip()
    except Exception:
        return ""
    return ""


def get_market_id() -> str:
    env_market_id = str(os.getenv("FRS_MARKET_ID", "") or "").strip()
    if env_market_id:
        return env_market_id

    credenciais_seguras = load_client_credentials()
    secure_market_id = str(credenciais_seguras.get("market_id") or "").strip()
    if secure_market_id:
        return secure_market_id

    cfg_market_id = _market_id_from_local_config()
    if cfg_market_id:
        return cfg_market_id

    raise ValueError("market_id ausente. Configure a Identificação de Mercado antes do uso do Firebase.")


def _ensure_market_namespace_mobile() -> None:
    db = firestore.client()
    market_id = get_market_id()
    db.collection("mercados").document(market_id).set(
        {
            "market_id": market_id,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )


def buscar_produto_por_codigo(codigo: str):
    codigo = str(codigo or "").strip()
    if not codigo:
        return None

    db = obter_firestore_client()
    market_id = get_market_id()

    # Toda consulta no Firestore agora é segregada por market_id.
    consulta = (
        db.collection("produtos")
        .where("market_id", "==", market_id)
        .where("codigo_barras", "==", codigo)
        .limit(1)
        .stream()
    )
    docs = list(consulta)
    if not docs:
        consulta = (
            db.collection("produtos")
            .where("market_id", "==", market_id)
            .where("codigo", "==", codigo)
            .limit(1)
            .stream()
        )
        docs = list(consulta)

    if not docs:
        return None

    data = docs[0].to_dict() or {}
    return {
        "id": docs[0].id,
        "market_id": market_id,
        "nome": str(data.get("nome") or data.get("descricao") or "Produto sem nome"),
        "preco": float(data.get("preco") or data.get("preco_venda") or 0.0),
        "foto_url": str(data.get("foto_url") or data.get("imagem_url") or "").strip(),
    }
