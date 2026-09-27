"""Challenge local para solicitação de licença do FRS Mercado."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .formats import CHALLENGE_SCHEMA, PRODUCT
from .machine_identity import MachineIdentity, collect_machine_identity
from .storage import LicenseStorage


def create_challenge(
    storage: LicenseStorage | None = None,
    machine_identity: MachineIdentity | None = None,
) -> dict:
    store = storage or LicenseStorage()
    identity = machine_identity or collect_machine_identity()
    now = datetime.now(timezone.utc).isoformat()
    challenge = {
        "schema": CHALLENGE_SCHEMA,
        "challenge_id": str(uuid.uuid4()),
        "product": PRODUCT,
        "installation_id": store.get_or_create_installation_id(),
        "binding_algorithm": identity.binding_algorithm,
        "component_hashes": dict(sorted(identity.component_hashes.items())),
        "required_components": list(identity.required_components),
        "minimum_matches": identity.minimum_matches,
        "created_at": now,
        "nonce": uuid.uuid4().hex,
    }
    store.save_challenge(challenge)
    return challenge
