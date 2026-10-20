"""Private new-only creation result and caller-retained platform lifetime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.errors import ValidationError
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.vms._native_execution_access import OwnedVMPlatformAccess

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline


def creation_metadata_snapshot(metadata: Mapping[str, str]) -> dict[str, str]:
    """Copy metadata crossing the independently implemented platform boundary."""
    if not isinstance(metadata, Mapping):
        raise ValidationError("VM creation metadata must map strings to strings")
    snapshot = dict(metadata)
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in snapshot.items()):
        raise ValidationError("VM creation metadata must map strings to strings")
    return snapshot


@dataclass(frozen=True, slots=True, repr=False)
class VMCreationResult:
    """Observed public addressability and delivery, never a legacy transport.

    Platform implementations can construct this outside static typing. Core
    stores the copied metadata without interpreting its provider-owned fields.
    This result does not close the platform bootstrap rollback window.
    """

    platform_metadata: Mapping[str, str]
    binding: NativeExecutionBinding
    locator: ProviderLocator

    def __post_init__(self) -> None:
        metadata = creation_metadata_snapshot(self.platform_metadata)
        if type(self.binding) is not NativeExecutionBinding or type(self.locator) is not ProviderLocator:
            raise ValidationError("VM creation requires an independent binding and observed provider locator")
        object.__setattr__(self, "platform_metadata", MappingProxyType(metadata))


class OwnedVMCreationAccess(OwnedVMPlatformAccess, Protocol):
    """Retain creation and its rollback window under the caller's exact owner.

    Construction is passive. Provisioning is one-shot under the original finite
    budget. Neither cleanup nor a fresh budget can replay provisioning. Unknown
    effects retain the same adapter and durable intent rather than adopting a VM
    by candidate identifier or display name.
    """

    @property
    def platform_metadata(self) -> Mapping[str, str]:
        """Only proved resource addressability, including after partial failure."""
        ...

    def provision(self, deadline: Deadline) -> VMCreationResult: ...

    def complete_bootstrap(self, deadline: Deadline) -> None:
        """Transfer resources after core proves bootstrap and durable addressability."""
        ...

    def request_rollback(self) -> None:
        """Select cleanup of an unaccepted creation, without performing I/O.

        Aggregate settlement performs eligible cleanup only after guest work
        settles. Completion of bootstrap permanently closes this window.
        """
        ...
