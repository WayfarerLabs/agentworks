"""Concrete host-only WSL registration and power observations."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.db import VMStatus
from agentworks.errors import ConnectivityError, LimitExceededError, StateError
from agentworks.execution._borrowed_io import DiscardSink
from agentworks.execution.carrier import CarrierIO, Failure, SinkOutput
from agentworks.execution.carriers._subprocess import run_process

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Deadline

_REGISTRATION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$distro = $env:AGENTWORKS_WSL_DISTRO
$root = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
$registrations = @(
    if (Test-Path -LiteralPath $root) {
        Get-ChildItem -LiteralPath $root | ForEach-Object {
            $properties = Get-ItemProperty -LiteralPath $_.PSPath
            if ($properties.DistributionName -ieq $distro) {
                [pscustomobject]@{
                    guid = $_.PSChildName
                    name = $properties.DistributionName
                    base_path = $properties.BasePath
                }
            }
        }
    }
)
if ($registrations.Count -gt 1) { exit 3 }
[pscustomobject]@{
    machine_guid = Get-ItemPropertyValue -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid
    user_sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    registration = $(if ($registrations.Count -eq 1) { $registrations[0] } else { $null })
} | ConvertTo-Json -Compress
"""


class HostObservationSink:
    """Bound external stdout and discard diagnostics without retaining secrets."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.oversized = False

    def try_write(self, data: memoryview) -> int:
        if not self.oversized:
            if len(data) > 65_536 - len(self.data):
                self.data.clear()
                self.oversized = True
            else:
                self.data.extend(data)
        return len(data)


@dataclass(frozen=True, slots=True, repr=False)
class WSLRegistration:
    machine: str
    sid: str
    guid: str | None
    name: str
    base_path: str | None


def decode_registration(data: bytes, name: str) -> WSLRegistration:
    """Reject malformed registry output before using its placement or identity."""
    try:
        value = json.loads(data.decode("utf-8-sig"))
        if type(value) is not dict or set(value) != {"machine_guid", "user_sid", "registration"}:
            raise ValueError
        machine, sid, registration = value["machine_guid"], value["user_sid"], value["registration"]
        if type(machine) is not str or type(sid) is not str or re.fullmatch(r"S-\d+(?:-\d+)+", sid, re.ASCII) is None:
            raise ValueError
        machine = str(uuid.UUID(machine))
        if registration is None:
            return WSLRegistration(machine, sid, None, name, None)
        if type(registration) is not dict or set(registration) != {"guid", "name", "base_path"}:
            raise ValueError
        guid, registered_name, base = registration["guid"], registration["name"], registration["base_path"]
        if type(guid) is not str or registered_name != name or type(base) is not str or not base or "\0" in base:
            raise ValueError
        return WSLRegistration(machine, sid, str(uuid.UUID(guid)), name, base)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StateError("WSL registration observation is invalid") from None


def observe_registration(name: str, vm_name: str, deadline: Deadline, custody: LocalDeliveryCustody) -> WSLRegistration:
    provider_locator_remaining(deadline, vm_name=vm_name)
    sink = HostObservationSink()
    result = run_process(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _REGISTRATION_SCRIPT],
        io=CarrierIO(output=SinkOutput(sink, DiscardSink(), require_live=False)),
        deadline=deadline,
        custody=custody,
        env={**os.environ, "AGENTWORKS_WSL_DISTRO": name},
    )
    if result.failure is Failure.DEADLINE:
        raise LimitExceededError("WSL registration observation timed out", entity_kind="vm", entity_name=vm_name)
    if result.exit_status == 3:
        raise StateError("WSL registration is unavailable or ambiguous")
    if (
        result.failure is not None
        or result.exit_status != 0
        or not result.stdout.complete
        or not custody.settled
        or sink.oversized
    ):
        raise ConnectivityError("WSL registration observation is unavailable", entity_kind="vm", entity_name=vm_name)
    provider_locator_remaining(deadline, vm_name=vm_name)
    return decode_registration(bytes(sink.data), name)


def execution_power_from_listing(raw: bytes, distro_name: str) -> VMStatus:
    """Accept exactly one English WSL2 state row, never a guest probe."""
    if len(raw) > 65_536:
        return VMStatus.UNKNOWN
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            listing = raw.decode("utf-16")
        else:
            listing = raw.decode("utf-16-le") if b"\x00" in raw else raw.decode("utf-8")
    except UnicodeDecodeError:
        return VMStatus.UNKNOWN
    matches = []
    for line in listing.splitlines():
        parts = line.strip().removeprefix("*").strip().rsplit(None, 2)
        if len(parts) == 3 and parts[0] == distro_name:
            matches.append(parts)
    if len(matches) != 1 or matches[0][2] != "2":
        return VMStatus.UNKNOWN
    return {"Running": VMStatus.RUNNING, "Stopped": VMStatus.STOPPED}.get(matches[0][1], VMStatus.UNKNOWN)
