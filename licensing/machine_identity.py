"""Identidade de instalação e componentes fortes do Windows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from .errors import LicenseError

_INVALID_VALUES = {
    "",
    "0",
    "00000000-0000-0000-0000-000000000000",
    "BASE BOARD SERIAL NUMBER",
    "DEFAULT STRING",
    "FILL BY O.E.M.",
    "NOT APPLICABLE",
    "NOT AVAILABLE",
    "NOT SPECIFIED",
    "NONE",
    "SYSTEM SERIAL NUMBER",
    "TO BE FILLED BY O.E.M.",
    "UNKNOWN",
    "DEFAULT",
}


@dataclass(frozen=True)
class MachineIdentity:
    component_hashes: dict[str, str]
    required_components: tuple[str, ...]
    minimum_matches: int
    binding_algorithm: str = "FRS-MACHINE-V1"

    def as_dict(self) -> dict:
        return {
            "binding_algorithm": self.binding_algorithm,
            "component_hashes": dict(sorted(self.component_hashes.items())),
            "required_components": list(self.required_components),
            "minimum_matches": self.minimum_matches,
        }


def _normalize_component(name: str, value: object) -> str | None:
    raw = str(value or "").strip().upper()
    text = re.sub(r"[\s-]+", "", raw)
    invalid = {re.sub(r"[\s-]+", "", item) for item in _INVALID_VALUES}
    if not text or text in invalid or text == "0" * len(text):
        return None
    if name == "machine_guid":
        try:
            import uuid
            return str(uuid.UUID(text)).upper()
        except Exception:
            return None
    return text or None


def _component_hash(name: str, normalized: str) -> str:
    material = f"FRS-MERCADO-MACHINE:{name}:{normalized}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _windows_components() -> dict[str, str]:
    if os.name != "nt":
        raise LicenseError("Identidade robusta está disponível apenas no Windows.")
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$machineGuid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
$product = Get-CimInstance Win32_ComputerSystemProduct
$bios = Get-CimInstance Win32_BIOS
$board = Get-CimInstance Win32_BaseBoard
$tpm = Get-CimInstance -Namespace root\CIMV2\Security\MicrosoftTpm -ClassName Win32_Tpm
$tpmPublic = $null
if ($tpm -and $tpm.PSObject.Properties.Name -contains 'PublicKey') { $tpmPublic = $tpm.PublicKey }
[pscustomobject]@{
  machine_guid = $machineGuid
  smbios_uuid = $product.UUID
  bios_serial = $bios.SerialNumber
  baseboard_serial = $board.SerialNumber
  tpm_ek_public_hash = $tpmPublic
} | ConvertTo-Json -Compress
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise LicenseError("Não foi possível coletar a identidade do Windows.") from exc
    if not isinstance(raw, dict):
        raise LicenseError("Resposta de identidade do Windows inválida.")
    return {str(k): str(v) for k, v in raw.items()}


def collect_machine_identity(
    component_collector: Callable[[], dict[str, str]] | None = None,
) -> MachineIdentity:
    raw = component_collector() if component_collector else _windows_components()
    hashes: dict[str, str] = {}
    for name in ("machine_guid", "smbios_uuid", "bios_serial", "baseboard_serial", "tpm_ek_public_hash"):
        normalized = _normalize_component(name, raw.get(name))
        if normalized:
            hashes[name] = _component_hash(name, normalized)

    stable = [name for name in ("machine_guid", "smbios_uuid", "bios_serial", "baseboard_serial") if name in hashes]
    if "tpm_ek_public_hash" in hashes:
        required = ("tpm_ek_public_hash", *stable[:2])
        minimum = 2
    else:
        if len(stable) < 2:
            raise LicenseError(
                "Identidade insuficiente: são necessários ao menos dois componentes fortes do Windows."
            )
        required = tuple(stable[:3])
        minimum = 2
    return MachineIdentity(hashes, required, minimum)
