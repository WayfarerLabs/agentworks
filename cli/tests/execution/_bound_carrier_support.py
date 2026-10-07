"""Caller-held delivery for standalone concrete-carrier helper proofs."""

from collections.abc import Callable, Iterator, Mapping

import pytest

from agentworks.db.operations import LifecycleObligation
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import (
    Carrier,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    Failure,
    PreparedInvocation,
)
from agentworks.execution.carriers._subprocess import ProcessResult, run_process
from agentworks.operations import OperationOwner


def obligation_receipt(owner: OperationOwner, obligation_id: str) -> LifecycleObligation:
    """Assert one retained receipt without enumerating completed history."""
    row = owner.inspect_lifecycle_obligation(obligation_id)
    assert row is not None
    return row


def fixture_dispatch(result: ProcessResult) -> Dispatch:
    """Preserve immutable admission uncertainty despite later local cleanup."""
    if result.started:
        return Dispatch.SENT
    return Dispatch.UNKNOWN if result.failure is Failure.OBSERVATION else Dispatch.NOT_SENT


def run_fixture_process(
    argv: list[str],
    *,
    io: CarrierIO,
    deadline: Deadline,
    custody: LocalDeliveryCustody | None,
    standalone_custody: LocalDeliveryCustody,
    live_stdio: bool = False,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Drain standalone proof custody before exposing cached process evidence.

    An externally supplied store remains with its operation owner so tests can
    observe pending delivery before the owner's fixture performs cleanup.
    """
    held = standalone_custody if custody is None else custody
    try:
        result = run_process(argv, io=io, deadline=deadline, custody=held, live_stdio=live_stdio, env=env)
        if custody is None:
            assert held.settled
        return result
    finally:
        if custody is None:
            assert held.close(Deadline.after(3))


class FixtureBoundCarrier:
    """Bind a raw carrier to the test's retained local delivery store."""

    def __init__(self, carrier: Carrier, custody: LocalDeliveryCustody) -> None:
        self.carrier = carrier
        self.custody = custody

    @property
    def features(self) -> ChannelFeatures:
        return self.carrier.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        return self.carrier.execute(invocation, io=io, deadline=deadline, custody=self.custody)


@pytest.fixture
def bind_carrier() -> Iterator[Callable[[Carrier], FixtureBoundCarrier]]:
    held: list[FixtureBoundCarrier] = []

    def bind(carrier: Carrier) -> FixtureBoundCarrier:
        delivery = FixtureBoundCarrier(carrier, LocalDeliveryCustody())
        held.append(delivery)
        return delivery

    try:
        yield bind
    finally:
        deadline = Deadline.after(5)
        cleaned = True
        for delivery in held:
            cleaned = delivery.custody.close(deadline) and cleaned
        assert cleaned


@pytest.fixture
def hold_operation_owner() -> Iterator[Callable[[OperationOwner], OperationOwner]]:
    """Retain exact aggregate owners through pending-outcome assertions."""
    held: list[OperationOwner] = []

    def hold(owner: OperationOwner) -> OperationOwner:
        held.append(owner)
        return owner

    try:
        yield hold
    finally:
        deadline = Deadline.after(5)
        cleaned = True
        for owner in held:
            cleaned = owner.close_local_delivery(deadline) and cleaned
        assert cleaned
