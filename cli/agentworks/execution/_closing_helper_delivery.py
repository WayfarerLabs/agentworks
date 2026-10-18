"""Actual local and native custody shared by two fixed cleanup consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

from ._delivery_custody import LocalDeliveryCustody
from .carrier import Dispatch, ExitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._helper_closure import HelperClosureDelivery
    from ._runtime_prerequisite import HelperClosureExpectation
    from .carrier import Carrier, CarrierIO, CarrierReport, ChannelFeatures, Deadline, PreparedInvocation


class ClosingHelperDelivery:
    """One fixed cleanup delivery; resource controllers supply exact admission.

    Only scalar report facts survive here. Protocol candidates and ownership
    decisions belong to the resource-specific controller.
    """

    def __init__(
        self,
        base: Carrier,
        require: Callable[[ClosingHelperDelivery, Deadline], None],
        route: Callable[[Deadline], None],
    ) -> None:
        self._base = base
        self._require, self._route = require, route
        self.custody = LocalDeliveryCustody()
        self.delivery: HelperClosureDelivery | None = None
        self.expectation: HelperClosureExpectation | None = None
        self.entered = False
        self.dispatch: Dispatch | None = None
        self.completion: ExitStatus | None = None

    @property
    def base_carrier(self) -> Carrier:
        return self._base

    @property
    def carrier(self) -> Carrier:
        return self._base if self.delivery is None else self.delivery

    @property
    def features(self) -> ChannelFeatures:
        return self.carrier.features

    @property
    def helper_pending(self) -> bool:
        closed = (
            not self.entered
            or self.dispatch is Dispatch.NOT_SENT
            or (
                self.delivery.closure_proven
                if self.delivery is not None
                else self.dispatch is Dispatch.SENT and self.completion == ExitStatus(0)
            )
        )
        return not closed or not self.custody.settled

    def bind(
        self,
        expectation: HelperClosureExpectation,
        factory: Callable[[HelperClosureExpectation], HelperClosureDelivery] | None,
    ) -> ClosingHelperDelivery:
        if self.entered or self.expectation is not None:
            raise StateError("Closing helper delivery is already bound")
        self.expectation = expectation
        if factory is not None:
            self.delivery = factory(expectation)
        return self

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        self._require(self, deadline)
        if self.entered:
            raise StateError("Closing helper delivery is one-shot")
        self._route(deadline)
        self._require(self, deadline)
        self.entered = True
        report = self.carrier.execute(invocation, io=io, deadline=deadline, custody=self.custody)
        self.dispatch, self.completion = report.dispatch, report.completion
        return report

    def settle(self, deadline: Deadline, *, observe: bool) -> bool:
        self._require(self, deadline)
        if not self.custody.close(deadline):
            return False
        self._require(self, deadline)
        delivery = self.delivery
        if (
            self.entered
            and self.dispatch is not Dispatch.NOT_SENT
            and delivery is not None
            and not delivery.closure_proven
        ):
            if not observe:
                return False
            self._route(deadline)
            self._require(self, deadline)
            if not delivery.observe_closure(deadline=deadline, custody=self.custody):
                return False
            self._require(self, deadline)
        return not self.helper_pending
