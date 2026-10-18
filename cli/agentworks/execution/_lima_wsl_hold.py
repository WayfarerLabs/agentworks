"""Operation-length WSL availability under Lima's exact selected registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence, HandleSettlement, HostClientStatus, JobAssignment
from agentworks.execution._wsl2_platform_hold import (
    OBLIGATION_KIND,
    PAYLOAD_VERSION,
    WSL2PlatformHold,
    decode_hold_payload,
    locator_digest,
)
from agentworks.execution._wsl2_windows import WindowsWSL2HostClient
from agentworks.execution.carriers.wsl2 import WSL2Connection

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from agentworks.execution._lima_selection import LimaSelection
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner


class LimaWSLHold:
    """Retain the physical hold before dispatch, including uncertain startup."""

    def __init__(self, vm: VMRow, selected: LimaSelection, owner: OperationOwner) -> None:
        registration = selected.registration
        if registration is None or registration.guid is None or vm.instance_marker is None:
            raise StateError("Lima WSL availability requires an exact registration and VM marker")
        self.selected = selected
        self.owner = owner
        self._marker = vm.instance_marker
        # This fixed core support effect precedes Lima's delivery-account lookup.
        connection = WSL2Connection(registration.name, "root", "wsl")
        self.hold = WSL2PlatformHold(
            owner,
            vm.name,
            selected.locator.token,
            vm.instance_marker,
            connection,
            WindowsWSL2HostClient(),
            WSL2GuestObserver(connection),
        )

    def start(self, deadline: Deadline) -> None:
        self.hold.start(deadline)
        self.require_ready()

    def require_ready(self) -> None:
        """Require both durable READY identity and the retained live native owner."""
        hold = self.hold
        identity, obligation = hold.evidence.identity, hold.obligation
        local = hold._current_local_snapshot()  # noqa: SLF001
        if (
            identity is None
            or obligation is None
            or hold.registration_uncertain
            or local.host_client_status is not HostClientStatus.ACTIVE
            or local.job_assignment is not JobAssignment.ASSIGNED_AT_CREATION
            or local.job_handle_settlement is not HandleSettlement.OPEN
            or local.host_handle_settlement is not HandleSettlement.OPEN
        ):
            raise StateError("Lima WSL availability has no live READY hold")
        for row in self.owner.list_pending_lifecycle_obligations():
            if (
                row.obligation_id != obligation.obligation_id
                or row.state is not LifecycleObligationState.POSSIBLE_EFFECT
                or row.obligation_kind != OBLIGATION_KIND
                or row.payload_version != PAYLOAD_VERSION
                or row.payload_revision != obligation.payload_revision
            ):
                continue
            payload = decode_hold_payload(row.payload)
            if (
                payload == hold.payload
                and payload.guest == identity
                and payload.locator_sha256 == locator_digest(self.selected.locator.token)
                and payload.instance_marker == self._marker
            ):
                return
        raise StateError("Lima WSL availability has no durable READY hold")

    def require_guest(self, guest: VMGuestIdentity) -> None:
        self.require_ready()
        anchor = self.hold.evidence.identity
        assert anchor is not None
        if guest.boot_id != anchor.boot_id or guest.init_start_ticks != anchor.init_start_ticks:
            raise StateError("Lima prepared guest does not match the held WSL identity")

    def release(self, deadline: Deadline) -> bool:
        """Release only after unrelated obligations have settled, retaining retries."""
        hold = self.hold
        if hold.registration_uncertain:
            return False
        obligation = hold.obligation
        held_row = obligation._persisted_obligation if obligation is not None else None  # noqa: SLF001
        if any(row != held_row for row in self.owner.list_pending_lifecycle_obligations()):
            return False
        evidence = hold.evidence if hold.payload is None else hold.release(deadline)
        if not evidence.local.settled:
            return False
        if evidence.local.host_client_status is not HostClientStatus.NOT_CREATED and (
            evidence.identity is None or evidence.guest_anchor_presence is not GuestAnchorPresence.ABSENT_CONFIRMED
        ):
            return False
        return not self.owner.list_pending_lifecycle_obligations()
