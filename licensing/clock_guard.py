"""Proteção contra retrocesso do relógio do sistema."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .errors import ClockRollbackError
from .storage import LicenseStorage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ClockGuard:
    def __init__(self, storage: LicenseStorage, tolerance_seconds: int = 300):
        self.storage = storage
        self.tolerance_seconds = max(0, int(tolerance_seconds))

    def check_and_update(self, now: datetime | None = None) -> datetime:
        current = (now or utc_now()).astimezone(timezone.utc)
        state = self.storage.read_json(self.storage.clock_path)
        try:
            previous = datetime.fromisoformat(str(state.get("last_seen_utc") or ""))
            previous = previous.replace(tzinfo=timezone.utc) if previous.tzinfo is None else previous.astimezone(timezone.utc)
        except Exception:
            previous = None
        if previous and current.timestamp() < previous.timestamp() - self.tolerance_seconds:
            raise ClockRollbackError("O relógio do sistema retrocedeu; licença bloqueada.")
        self.storage.write_json(
            self.storage.clock_path,
            {"schema": "FRS-CLOCK-STATE-V1", "last_seen_utc": current.isoformat()},
        )
        return current
