"""Caller-held delivery for standalone concrete-carrier helper proofs."""

from collections.abc import Callable, Iterator

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import (
    Carrier,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    PreparedInvocation,
)


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

    yield bind
    deadline = Deadline.after(5)
    for delivery in held:
        assert delivery.custody.close(deadline)
