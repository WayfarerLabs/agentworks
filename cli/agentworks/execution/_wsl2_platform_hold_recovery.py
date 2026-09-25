"""Caller-retained recovery of one persisted WSL2 platform hold."""

from __future__ import annotations

from dataclasses import replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Protocol

from agentworks.db.operations import LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution._wsl2_controller_observer import (
    ControllerIdentity,
    ControllerPresence,
    WindowsControllerObserver,
)
from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution._wsl2_platform_hold import (
    OBLIGATION_KIND,
    PAYLOAD_VERSION,
    WSL2HoldPayload,
    decode_hold_payload,
    encode_hold_payload,
    locator_digest,
)
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation
    from agentworks.execution.carriers.wsl2 import WSL2Connection
    from agentworks.operations import LifecycleObligation as BoundObligation
    from agentworks.operations import OperationOwner


class ControllerObserver(Protocol):
    def observe(self, identity: ControllerIdentity, deadline: Deadline) -> ControllerPresence: ...


class WSL2PlatformHoldRecovery:
    """Retain one observer, its native client, and exact recovery identity.

    Keep this object alive after every unsuccessful attempt. An UNKNOWN query
    may settle its local client again, but cannot dispatch another query: local
    settlement does not drain a request already admitted by WSLService. A
    complete, settled PRESENT result permits another query in this controller.
    """

    def __init__(
        self,
        owner: OperationOwner,
        obligation: LifecycleObligation,
        *,
        locator: str,
        instance_marker: str,
        connection: WSL2Connection,
        controller_observer: ControllerObserver | None = None,
        guest_observer: WSL2GuestObserver | None = None,
    ) -> None:
        if (
            obligation.ownership != owner.ownership
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or obligation.obligation_kind != OBLIGATION_KIND
            or obligation.payload_version != PAYLOAD_VERSION
        ):
            raise StateError("WSL2 hold recovery requires an exact taken-over VM obligation")
        payload = decode_hold_payload(obligation.payload)
        if (
            payload.locator_sha256 != locator_digest(locator)
            or payload.instance_marker != instance_marker
            or payload.distribution != connection.distribution
            or payload.user != connection.user
            or connection.wsl_executable.casefold() not in {"wsl", "wsl.exe"}
        ):
            raise StateError("WSL2 hold recovery preparation does not match")
        bound = owner.rebind_lifecycle_obligation(
            obligation.obligation_id,
            OBLIGATION_KIND,
            payload_version=PAYLOAD_VERSION,
            payload=obligation.payload,
        )
        current = bound._persisted_obligation  # noqa: SLF001
        if current.payload_revision != obligation.payload_revision:
            raise StateError("WSL2 hold recovery payload revision changed during binding")
        self._owner = owner
        self._obligation_id = obligation.obligation_id
        self._bound: BoundObligation = bound
        self._payload: WSL2HoldPayload = payload
        self._state = current.state
        self._controller = controller_observer if controller_observer is not None else WindowsControllerObserver()
        self._guest = guest_observer if guest_observer is not None else WSL2GuestObserver(connection)
        self._lock = Lock()
        self._query_uncertain = False
        self._completed_present = False
        self._admission_uncertain = False
        self._terminal_absence = False

    def recover(self, deadline: Deadline) -> bool:
        """Return true only after this exact obligation is durably resolved."""
        remaining = _remaining(deadline)
        if not self._lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            return False
        try:
            return self._recover_locked(deadline)
        finally:
            self._lock.release()

    def settle_pending(self, deadline: Deadline) -> bool:
        """Retry native cleanup without authorizing another guest query."""
        remaining = _remaining(deadline)
        if not self._lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            return False
        try:
            return self._guest.settle_pending(deadline)
        finally:
            self._lock.release()

    def _recover_locked(self, deadline: Deadline) -> bool:
        if deadline.expired:
            return False
        if self._state is LifecycleObligationState.RESOLVED:
            return True
        if self._state is LifecycleObligationState.REGISTERED:
            # The old generation cannot mark possible effect after takeover.
            # The original hold could not dispatch before that mark committed.
            self._bound.resolve()
            self._state = LifecycleObligationState.RESOLVED
            return True
        if self._terminal_absence:
            # The complete, settled exact absence has already closed its
            # recovery attempt. Resolve retries never issue another query.
            self._bound.resolve()
            self._state = LifecycleObligationState.RESOLVED
            return True
        if self._state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("WSL2 hold recovery obligation state is invalid")
        if (
            self._payload.guest is None
            or self._query_uncertain
            or self._admission_uncertain
            or (self._payload.query_may_have_been_admitted and not self._completed_present)
        ):
            return False
        if self._controller.observe(self._payload.controller, deadline) is not ControllerPresence.ABSENT_CONFIRMED:
            return False
        if deadline.expired:
            return False
        if not self._payload.query_may_have_been_admitted:
            self._admit_query()
        return self._query(deadline)

    def _admit_query(self) -> None:
        # READY proves this anchor launch reached the guest. Controller death
        # closes its non-inherited Job handle, but cannot prove guest absence.
        # Persist the one-way marker before any recovery query can dispatch.
        admitted = replace(self._payload, query_may_have_been_admitted=True)
        self._admission_uncertain = True
        self._bound.publish_payload(
            expected_revision=self._bound.payload_revision,
            payload_version=PAYLOAD_VERSION,
            payload=encode_hold_payload(admitted),
        )
        self._payload = admitted
        self._admission_uncertain = False

    def _query(self, deadline: Deadline) -> bool:
        encoded = encode_hold_payload(self._payload)
        recovery = self._owner.rebind_possible_effect_lifecycle_obligation(
            self._obligation_id,
            OBLIGATION_KIND,
            payload_version=PAYLOAD_VERSION,
            payload=encoded,
            payload_revision=self._bound.payload_revision,
        )
        dispatch = recovery.open_dispatch()
        attempt = None
        try:
            attempt = dispatch.begin_attempt()
            self._query_uncertain = True
            identity = self._payload.guest
            assert identity is not None
            presence = self._guest.observe(identity, deadline)
            if presence is GuestAnchorPresence.UNKNOWN or deadline.expired:
                dispatch.handoff_unresolved()
                return False
            attempt.settle()
            dispatch.close()
            self._query_uncertain = False
            if presence is GuestAnchorPresence.PRESENT:
                self._completed_present = True
                return False
        except BaseException:
            self._query_uncertain = True
            if attempt is None:
                dispatch._abort_unreturned_attempt()  # noqa: SLF001
            else:
                dispatch.handoff_unresolved()
            raise
        self._terminal_absence = True
        self._bound.resolve()
        self._state = LifecycleObligationState.RESOLVED
        return True


def _remaining(deadline: Deadline) -> float:
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("WSL2 hold recovery requires a finite deadline")
    remaining = deadline.remaining()
    assert remaining is not None
    if remaining <= 0:
        raise ValidationError("WSL2 hold recovery deadline has expired")
    return remaining
