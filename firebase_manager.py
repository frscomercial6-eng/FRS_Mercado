import os
import sys
from pathlib import Path


_FIREBASE_APP = None


def _resolve_market_id(provided: str | None = None) -> str:
    market_id = str(provided or "").strip()
    if market_id:
        return market_id

    from market_identity import ensure_local_market_id

    market_id = ensure_local_market_id()
    if not market_id:
        raise ValueError("market_id ausente. Configure a identificação de mercado antes de usar o Firebase.")
    return market_id


def _base_exec_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolver_arquivo_credenciais() -> Path:
    candidatos = []

    env_path = str(os.getenv("FIREBASE_ADMIN_KEY_PATH", "") or "").strip()
    if env_path:
        candidatos.append(Path(env_path).expanduser())

    base = _base_exec_dir()
    cwd = Path.cwd()

    candidatos.append(base / "firebase-admin-key.json")
    candidatos.append(cwd / "firebase-admin-key.json")

    # Compatibilidade: encontra automaticamente chaves oficiais do Firebase Admin SDK.
    candidatos.extend(sorted(base.glob("*firebase-adminsdk*.json")))
    candidatos.extend(sorted(cwd.glob("*firebase-adminsdk*.json")))

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
        "Arquivo de credenciais Firebase não encontrado. "
        "Use FIREBASE_ADMIN_KEY_PATH ou coloque firebase-admin-key.json na raiz da aplicação."
    )


def inicializar_firebase():
    global _FIREBASE_APP

    if _FIREBASE_APP is not None:
        return _FIREBASE_APP

    import firebase_admin
    from firebase_admin import credentials

    cred_path = _resolver_arquivo_credenciais()

    # Reaproveita app default se já estiver inicializado por outro módulo.
    if firebase_admin._apps:
        _FIREBASE_APP = firebase_admin.get_app()
        return _FIREBASE_APP

    cred = credentials.Certificate(str(cred_path))
    _FIREBASE_APP = firebase_admin.initialize_app(cred)
    return _FIREBASE_APP


def testar_conexao() -> tuple[bool, str]:
    try:
        inicializar_firebase()

        from firebase_admin import firestore

        db = firestore.client()
        docs = db.collection("teste").limit(1).stream()
        _ = list(docs)
        return True, "Conexão Firebase OK (coleção 'teste' acessada)."
    except Exception as e:
        return False, f"Falha na conexão Firebase: {e}"


def ensure_market_namespace(market_id: str | None = None, market_name: str = "", cnpj: str = "") -> str:
    inicializar_firebase()
    from firebase_admin import firestore

    resolved_market_id = _resolve_market_id(market_id)
    db = firestore.client()
    doc = db.collection("mercados").document(resolved_market_id)
    payload = {
        "market_id": resolved_market_id,
        "market_name": str(market_name or "").strip(),
        "cnpj": str(cnpj or "").strip(),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    # Merge mantém dados já existentes e garante o namespace do cliente.
    doc.set(payload, merge=True)
    return resolved_market_id


def market_scoped_collection(collection_name: str, market_id: str | None = None):
    inicializar_firebase()
    from firebase_admin import firestore

    resolved_market_id = _resolve_market_id(market_id)
    db = firestore.client()
    return db.collection(collection_name).where("market_id", "==", resolved_market_id)


def market_scoped_payload(payload: dict, market_id: str | None = None) -> dict:
    resolved_market_id = _resolve_market_id(market_id)
    out = dict(payload or {})
    out["market_id"] = resolved_market_id
    return out
