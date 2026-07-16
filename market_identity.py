import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app_paths import obter_caminho_dados


@dataclass
class MarketProvisionResult:
    market_id: str
    firebase_ok: bool
    drive_ok: bool
    firebase_error: str = ""
    drive_error: str = ""


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return cleaned


def _config_path() -> Path:
    return Path(obter_caminho_dados("config.json"))


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config(config: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8")


def derive_market_id(config: dict) -> str:
    existing = str(config.get("market_id") or "").strip()
    if existing:
        return existing

    cnpj_digits = re.sub(r"\D", "", str(config.get("cnpj") or ""))
    if cnpj_digits:
        return f"market-{cnpj_digits}"

    razao = str(config.get("razao_social") or config.get("nome_estabelecimento") or "").strip()
    if razao:
        return f"market-{_slugify(razao)}"

    # Fallback seguro para bases vazias no primeiro bootstrap.
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"market-local-{stamp}"


def ensure_local_market_id() -> str:
    config = _load_config()
    market_id = derive_market_id(config)
    if str(config.get("market_id") or "").strip() != market_id:
        config["market_id"] = market_id
        _save_config(config)
    return market_id


def add_market_id_to_payload(payload: dict, market_id: str | None = None) -> dict:
    mid = str(market_id or ensure_local_market_id()).strip()
    out = dict(payload or {})
    out["market_id"] = mid
    return out


def ensure_market_identity_provisioning() -> MarketProvisionResult:
    market_id = ensure_local_market_id()
    cfg = _load_config()

    firebase_ok = True
    firebase_error = ""
    try:
        from firebase_manager import ensure_market_namespace

        ensure_market_namespace(market_id=market_id, market_name=str(cfg.get("razao_social") or ""), cnpj=str(cfg.get("cnpj") or ""))
    except Exception as e:
        firebase_ok = False
        firebase_error = str(e)

    drive_ok = True
    drive_error = ""
    try:
        from modulo_relatorio import ModuloRelatorio

        ModuloRelatorio.ensure_market_drive_root(market_id)
    except Exception as e:
        drive_ok = False
        drive_error = str(e)

    return MarketProvisionResult(
        market_id=market_id,
        firebase_ok=firebase_ok,
        drive_ok=drive_ok,
        firebase_error=firebase_error,
        drive_error=drive_error,
    )
