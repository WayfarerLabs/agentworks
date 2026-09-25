"""Private composition of the managed identity for one VM target.

The provider locator remains opaque here. Core only binds its exact UTF-8
representation to the core-owned instance marker, then keeps the guest boot
UUID as a separate fence on :class:`ManagedTargetIdentity`.
"""

from __future__ import annotations

import hashlib
import struct
from typing import TYPE_CHECKING
from uuid import NAMESPACE_DNS, uuid5

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.db import VMRow
from agentworks.errors import StateError, ValidationError
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform.base import ProviderLocatorObservation


_INCARNATION_DOMAIN = b"agentworks/vm-incarnation"
_INCARNATION_VERSION = b"v1"
_BOOT_FENCE_DOMAIN = "agentworks/linux-guest-boot/v1"


def vm_incarnation_fingerprint(locator: ProviderLocator, instance_marker: str) -> str:
    """Return the version-one fingerprint for one provider VM incarnation.

    The digest input is a domain and version tag, followed by a big-endian
    four-byte length and the locator's exact UTF-8 bytes, then the validated
    32-character marker bytes.  Length-prefixing the opaque locator makes the
    framing unambiguous without parsing or normalizing provider data.
    """

    if type(locator) is not ProviderLocator:
        raise ValidationError("VM incarnation requires a provider locator")
    marker = validate_vm_instance_marker(instance_marker)
    locator_bytes = locator.token.encode("utf-8")
    framed = b"\0".join((_INCARNATION_DOMAIN, _INCARNATION_VERSION))
    framed += struct.pack(">I", len(locator_bytes)) + locator_bytes + marker.encode("ascii")
    return f"v1:{hashlib.sha256(framed).hexdigest()}"


def vm_guest_boot_id(guest: VMGuestIdentity) -> str:
    """Derive one boot ID from the kernel boot and the guest init process.

    WSL2 distributions share a kernel boot but have distinct PID 1 lifetimes.
    The resulting UUID changes when either observed value changes. It is an
    ordinary cooperative-guest fence, not proof against a forged procfs view.
    """
    if type(guest) is not VMGuestIdentity:
        raise ValidationError("VM boot identity requires a guest observation")
    return str(uuid5(NAMESPACE_DNS, f"{_BOOT_FENCE_DOMAIN}:{guest.boot_id}:{guest.init_start_ticks}"))


def compose_managed_vm_target_identity(
    vm: VMRow,
    locator: ProviderLocatorObservation,
    guest: VMGuestIdentity,
) -> ManagedTargetIdentity:
    """Compose a managed VM identity from persisted and live exact facts.

    This pure function never creates, adopts, persists or repairs a marker.
    Existing rows without one require an explicit adoption operation before
    ordinary managed composition can proceed.
    """

    if not isinstance(vm, VMRow):
        raise ValidationError("Managed VM target requires a VM row")
    if type(locator) is ProviderLocatorUnavailable:
        raise StateError(
            f"Provider locator is unavailable for VM '{vm.name}'",
            entity_kind="vm",
            entity_name=vm.name,
            hint="This platform needs an approved identity alternative before managed execution.",
        )
    if type(locator) is not ProviderLocator:
        raise ValidationError("Managed VM target requires a provider locator observation")
    if vm.instance_marker is None:
        raise StateError(
            f"VM '{vm.name}' has no persisted instance marker",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Explicit adoption is required before managed execution.",
        )
    persisted_marker = validate_vm_instance_marker(vm.instance_marker)
    if type(guest) is not VMGuestIdentity:
        raise ValidationError("Managed VM target requires a guest identity")
    guest_marker = validate_vm_instance_marker(guest.instance_marker)
    if guest_marker != persisted_marker:
        raise StateError(
            f"Guest instance marker does not match VM '{vm.name}'",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Refuse the target and verify the selected VM; never adopt over an existing marker mismatch.",
        )
    return ManagedTargetIdentity(
        ManagedTargetKind.VM,
        vm.name,
        vm_incarnation_fingerprint(locator, persisted_marker),
        vm_guest_boot_id(guest),
    )
