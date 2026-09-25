"""Private recovery of one persisted WSL2 platform-hold obligation."""

from __future__ import annotations

from dataclasses import replace
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
    decode_hold_payload,
    encode_hold_payload,
    locator_digest,
)
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation
    from agentworks.execution.carriers.wsl2 import WSL2Connection
    from agentworks.operations import OperationOwner


class ControllerObserver(Protocol):
    def observe(self, identity: ControllerIdentity, deadline: Deadline) -> ControllerPresence: ...


def recover_platform_hold(
    owner: OperationOwner,
    obligation: LifecycleObligation,
    *,
    locator: str,
    instance_marker: str,
    connection: WSL2Connection,
    deadline: Deadline,
    controller_observer: ControllerObserver | None = None,
    guest_observer: WSL2GuestObserver | None = None,
) -> bool:
    """Resolve an exact retained hold only on a proved no-effect or guest-absence path.

    The caller supplies a row listed after generic takeover. A false result
    retains its obligation and coarse claim. No path replays the anchor launch.
    """
    if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
        raise ValidationError("WSL2 hold recovery requires a finite live deadline")
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
    if current.state is LifecycleObligationState.RESOLVED:
        return True
    if current.state is LifecycleObligationState.REGISTERED:
        # The old generation cannot mark possible effect after takeover.
        # The original hold could not dispatch before that mark committed.
        bound.resolve()
        return True
    if current.state is not LifecycleObligationState.POSSIBLE_EFFECT:
        raise StateError("WSL2 hold recovery obligation state is invalid")
    if payload.guest is None or payload.query_may_have_been_admitted:
        return False

    controller = controller_observer if controller_observer is not None else WindowsControllerObserver()
    if controller.observe(payload.controller, deadline) is not ControllerPresence.ABSENT_CONFIRMED:
        return False
    if deadline.expired:
        return False

    # READY means this anchor launch reached the guest. The former controller's
    # death closes its non-inherited Job handle; it does not prove guest absence.
    # Persist query admission before opening a fresh WSL client. A later recovery
    # cannot repeat the query unless its service-side dispatch has been drained.
    admitted = replace(payload, query_may_have_been_admitted=True)
    encoded = encode_hold_payload(admitted)
    published = bound.publish_payload(
        expected_revision=current.payload_revision,
        payload_version=PAYLOAD_VERSION,
        payload=encoded,
    )
    recovery = owner.rebind_possible_effect_lifecycle_obligation(
        obligation.obligation_id,
        OBLIGATION_KIND,
        payload_version=PAYLOAD_VERSION,
        payload=encoded,
        payload_revision=published.payload_revision,
    )
    dispatch = recovery.open_dispatch()
    attempt = None
    try:
        attempt = dispatch.begin_attempt()
        observer = guest_observer if guest_observer is not None else WSL2GuestObserver(connection)
        presence = observer.observe(payload.guest, deadline)
        if presence is not GuestAnchorPresence.ABSENT_CONFIRMED or deadline.expired:
            dispatch.handoff_unresolved()
            return False
        attempt.settle()
        dispatch.close()
    except BaseException:
        if attempt is None:
            dispatch._abort_unreturned_attempt()  # noqa: SLF001
        else:
            dispatch.handoff_unresolved()
        raise
    bound.resolve()
    return True
