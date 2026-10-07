"""Closed setup identity for one not-yet-bound Linux VM file gate."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity

_NAMESPACE = "/run/agentworks/file-gates-v1"
_MAX_LINUX_UID = (1 << 32) - 1


def file_effect_gate_path(target: ManagedTargetIdentity, euid: int, guest: VMGuestIdentity) -> str:
    """Choose the fixed boot-local gate name for one VM scope and helper UID."""
    if (
        type(target) is not ManagedTargetIdentity
        or target.kind is not ManagedTargetKind.VM
        or type(euid) is not int
        or not 0 <= euid <= _MAX_LINUX_UID
        or type(guest) is not VMGuestIdentity
    ):
        raise ValueError("invalid file-effect gate setup identity")
    identity = "\0".join(
        (
            target.name,
            guest.instance_marker,
            guest.boot_id,
            str(guest.init_start_ticks),
        )
    )
    digest = hashlib.sha256(identity.encode("ascii")).hexdigest()
    return f"{_NAMESPACE}/{euid}/{digest}.db"


@dataclass(frozen=True, slots=True, repr=False)
class FileEffectGateSetup:
    """Durable path and verified raw guest epoch before exact gate binding."""

    path: str
    guest: VMGuestIdentity

    @classmethod
    def for_target(cls, target: ManagedTargetIdentity, euid: int, guest: VMGuestIdentity) -> FileEffectGateSetup:
        """Build a descriptor from the selected verified VM identity."""
        return cls(file_effect_gate_path(target, euid, guest), guest)
