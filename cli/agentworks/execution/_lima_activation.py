"""Owned one-shot local Lima start with conservative request settlement."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Capture, CarrierIO, Failure
from agentworks.execution.carriers._subprocess import run_process
from agentworks.operations import LifecycleObligation

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._lima_selection import LimaLocalSelection, LimaSelection
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner

OBLIGATION_KIND = "lima-local-start"
PAYLOAD_VERSION = 1


@dataclass(frozen=True, slots=True, repr=False)
class ActivationPayload:
    """Opaque selected locator, without process or startup completion claims."""

    locator: str


def encode_activation_payload(payload: ActivationPayload) -> bytes:
    """Validate persisted recovery input, not private typed selection values."""
    if (
        type(payload) is not ActivationPayload
        or type(payload.locator) is not str
        or re.fullmatch(r"lima:v1:[0-9a-f]{64}", payload.locator) is None
    ):
        raise ValidationError("Lima activation payload is invalid")
    return json.dumps(
        {"version": PAYLOAD_VERSION, "locator": payload.locator}, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def decode_activation_payload(data: bytes) -> ActivationPayload:
    """Reject corrupt or noncanonical lifecycle data before reconciliation."""
    try:
        if type(data) is not bytes or len(data) > 256:
            raise ValueError
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or set(value) != {"version", "locator"} or type(value["version"]) is not int:
            raise ValueError
        payload = ActivationPayload(value["locator"])
        if value["version"] != PAYLOAD_VERSION or encode_activation_payload(payload) != data:
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise ValidationError("Lima activation payload is invalid") from None


class LimaActivation:
    """Retain before start; recovery retries ledger writes and never starts again.

    A naturally successful client and a fresh exact running observation jointly
    settle this request. A failed, lost or interrupted start remains debt: local
    process cleanup alone cannot prove the fate of Lima's runtime work.
    """

    def __init__(
        self, owner: OperationOwner, selected: LimaSelection, route: LimaLocalSelection, custody: LocalDeliveryCustody
    ) -> None:
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != route.vm_name
        ):
            raise ValidationError("Lima activation requires exact VM ownership")
        self.owner = owner
        self.selected = selected
        self.route = route
        self.custody = custody
        self.obligation_id = uuid4().hex
        self.payload = ActivationPayload(selected.locator.token)
        self.obligation: LifecycleObligation | None = None
        self.attempted = False
        self.mark_began = False
        self.client_completed = False
        self.no_dispatch = False
        self.running_observed = False

    def start(self, deadline: Deadline) -> None:
        provider_locator_remaining(deadline, vm_name=self.route.vm_name)
        if self.attempted:
            raise StateError("Lima activation was already attempted")
        current = self.route.observe(deadline, self.custody)
        self.selected.require_same_route(current)
        if current.power is not self.selected.power:
            raise StateError("Lima selected power changed before startup")
        self.attempted = True
        self.obligation = self.owner.register_lifecycle_obligation(
            OBLIGATION_KIND,
            payload_version=PAYLOAD_VERSION,
            payload=encode_activation_payload(self.payload),
            obligation_id=self.obligation_id,
        )
        provider_locator_remaining(deadline, vm_name=self.route.vm_name)
        assert self.route.directory is not None
        self.route.directory.check()
        self.mark_began = True
        self.obligation.mark_possible_effect()
        provider_locator_remaining(deadline, vm_name=self.route.vm_name)
        self.route.directory.check()
        result = run_process(
            [self.selected.executable, "start", "--tty=false", self.selected.instance],
            io=CarrierIO(output=Capture(16_384)),
            deadline=deadline,
            custody=self.custody,
            env={**os.environ, "LIMA_HOME": self.selected.home},
        )
        # Keep original result facts before any late-response rejection or DB write.
        self.client_completed = result.started and result.exit_status == 0 and result.failure is None
        self.no_dispatch = not result.started and result.failure is Failure.DISPATCH and self.custody.settled
        self.reconcile(deadline)
        if not self.client_completed:
            raise StateError("Lima startup lacks a successful natural client completion")

    def confirm_running(self, current: LimaSelection, deadline: Deadline) -> None:
        provider_locator_remaining(deadline, vm_name=self.route.vm_name)
        self.selected.require_same_route(current)
        from agentworks.db import VMStatus

        if not self.client_completed or current.power is not VMStatus.RUNNING or not self.custody.settled:
            raise StateError("Lima startup did not establish fresh running readiness")
        self.running_observed = True
        self.reconcile(deadline)

    def reconcile(self, deadline: Deadline) -> bool:
        """Use exact original ledger identity and retained evidence after lost replies."""
        provider_locator_remaining(deadline, vm_name=self.route.vm_name)
        if not self.attempted:
            return True
        row = self.owner.inspect_lifecycle_obligation(self.obligation_id)
        if row is None:
            # Registration may have committed without returning its handle.
            raise StateError("Lima activation registration remains uncertain")
        if (
            row.obligation_kind != OBLIGATION_KIND
            or row.payload_version != PAYLOAD_VERSION
            or row.payload_revision != 0
            or decode_activation_payload(row.payload) != self.payload
        ):
            raise StateError("Lima activation custody conflicts")
        self.obligation = LifecycleObligation(self.owner, row)
        complete = not self.mark_began or self.no_dispatch or self.client_completed and self.running_observed
        if row.state is LifecycleObligationState.RESOLVED:
            if not complete:
                raise StateError("Lima activation custody was prematurely resolved")
            return True
        if complete and self.custody.settled:
            self.obligation.resolve()
            return True
        return False
