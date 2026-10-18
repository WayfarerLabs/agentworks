"""Exact START continuation separates helper closure from launch receipt debt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedLaunchState
from agentworks.execution._managed_start_exchange import prepare_managed_start
from agentworks.execution._managed_start_operation import (
    ManagedStartControlFact,
    ManagedStartCustody,
    start_owned_managed_run,
)
from agentworks.execution._runtime_prerequisite import HelperClosureExpectation, RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.operations import OperationAttempt, OperationBorrow

from .test_managed_start_operation import GUEST, OBLIGATION, ROOT, RUN, Carrier, _records, _request
from .test_managed_start_operation import owned as owned


class Delivery(Carrier):
    def __init__(self, *, error: BaseException | None = None) -> None:
        super().__init__(lambda request: _records(request, receipt=False), code=None, error=error)
        self.closure_proven = False
        self.prove = True
        self.observations: list[tuple[Deadline, LocalDeliveryCustody]] = []

    def observe_closure(self, *, deadline: Deadline, custody: LocalDeliveryCustody) -> bool:
        assert custody.settled and not deadline.expired
        self.observations.append((deadline, custody))
        self.closure_proven = self.prove
        return self.closure_proven


def start(
    owned: Any,
    carrier: Carrier,
    *,
    delivery: Delivery | None = None,
    route_check: Callable[[Deadline], None] | None = None,
    retain: Callable[[ManagedStartCustody], None] | None = None,
    deadline: Deadline | None = None,
) -> tuple[list[ManagedStartCustody], Callable[[], Any]]:
    _, repository, record, owner = owned
    budget = Deadline.after(10) if deadline is None else deadline
    prepared = prepare_managed_start(
        carrier,
        record.identity,
        record.spec,
        record.output_policy,
        _request(record),
        IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
        budget,
        RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        GUEST,
    )
    expectation = HelperClosureExpectation(
        prepared.runtime.nonce, prepared.runtime.candidates, prepared.runtime.system_shim, GUEST
    )
    held: list[ManagedStartCustody] = []

    def publish(custody):
        assert not owner._active_borrow and not carrier.calls
        assert not owner.list_pending_lifecycle_obligations()
        held.append(custody)
        if retain is not None:
            retain(custody)

    def factory(actual):
        assert actual is expectation and len(held) == 1
        assert owner._active_borrow is None and not carrier.calls
        return delivery

    return held, lambda: start_owned_managed_run(
        repository,
        record,
        carrier,
        prepared=prepared,
        deadline=budget,
        owner=owner,
        obligation_id=OBLIGATION,
        retain_custody=publish,
        closure_expectation=expectation,
        new_helper_delivery=factory if delivery is not None else None,
        route_check=route_check,
    )


@pytest.mark.parametrize("escaped", [False, True])
def test_later_exact_closure_keeps_original_report_and_launch_debt(owned, escaped):
    _, repository, _, owner = owned
    original = KeyboardInterrupt("private-canary") if escaped else None
    delivery = Delivery(error=original)
    carrier = Carrier(lambda request: _records(request, receipt=True))
    routes: list[Deadline] = []
    deadline = Deadline.after(10)
    held, launch = start(owned, carrier, delivery=delivery, route_check=routes.append, deadline=deadline)
    if escaped:
        with pytest.raises(KeyboardInterrupt) as caught:
            launch()
        assert caught.value is original
        assert isinstance(original.__cause__, ManagedStartControlFact)
    else:
        outcome = launch()
        assert outcome.requires_owner_retention and outcome.pending_remote_effects
    custody = held[0]
    borrow = owner._active_borrow
    assert borrow is not None and not borrow.closed and borrow.has_outstanding_attempt
    assert custody.completion_view is None
    with pytest.raises(StateError):
        custody.retry_bookkeeping()
    object.__setattr__(deadline, "expires_at", 0.0)
    with pytest.raises(ValidationError):
        custody.observe_cleanup(deadline)
    fresh = Deadline.after(10)
    custody.observe_cleanup(fresh)
    view = custody.completion_view
    assert view is not None
    assert view[:2] == (Dispatch.UNKNOWN if escaped else Dispatch.SENT, None)
    assert view[2] is delivery.observations[0][1]
    assert routes == [deadline, fresh]
    custody.retry_bookkeeping()
    custody.retry_bookkeeping()
    assert borrow.closed and not borrow.has_outstanding_attempt
    assert owner._active_borrow is None
    assert carrier.calls == 0 and delivery.calls == 1 and len(delivery.observations) == 1
    assert carrier.validations == 1 and delivery.validations == 1
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    row = owner.inspect_lifecycle_obligation(OBLIGATION)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert b"private-canary" not in repr(custody).encode()


def test_selected_delivery_does_not_change_original_carrier_matching(owned):
    _, _, _, owner = owned
    carrier = Carrier(lambda request: b"")
    delivery = Delivery()
    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow, delivery=delivery)
    assert operation.wraps(carrier) and not operation.wraps(delivery)
    borrow.close()


@pytest.mark.parametrize("committed", [False, True])
def test_lost_local_cleanup_reply_keeps_actual_custody_until_retry(owned, monkeypatch, committed):
    _, _, _, owner = owned
    delivery = Delivery()
    carrier = Carrier(lambda request: b"")
    held, launch = start(owned, carrier, delivery=delivery)
    launch()
    custody = held[0]
    local = owner._outstanding_attempt.local_delivery
    local.begin_process()
    original = local.close
    control = KeyboardInterrupt("local cleanup reply")

    def interrupt(deadline):
        if committed:
            original(deadline)
        raise control

    monkeypatch.setattr(local, "close", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        custody.observe_cleanup(Deadline.after(10))
    assert caught.value is control and not delivery.observations
    assert not owner._active_borrow.closed and custody.completion_view is None
    monkeypatch.setattr(local, "close", original)
    custody.observe_cleanup(Deadline.after(10))
    custody.retry_bookkeeping()
    assert owner._active_borrow is None and len(delivery.observations) == 1


def test_unknown_native_closure_and_route_refusal_preserve_exact_attempt(owned):
    _, _, _, owner = owned
    delivery = Delivery()
    delivery.prove = False
    carrier = Carrier(lambda request: b"")
    routes: list[Deadline] = []
    held, launch = start(owned, carrier, delivery=delivery, route_check=routes.append)
    launch()
    custody = held[0]
    attempt = owner._outstanding_attempt
    with pytest.raises(StateError):
        custody.observe_cleanup(Deadline.after(10))
    assert custody.completion_view is None and owner._outstanding_attempt is attempt
    custody._route_check = lambda deadline: (_ for _ in ()).throw(StateError("changed route"))
    with pytest.raises(StateError):
        custody.observe_cleanup(Deadline.after(10))
    assert len(delivery.observations) == 1 and delivery.calls == 1
    assert owner._outstanding_attempt is attempt


def test_local_cleanup_precedes_native_observation_and_guards_view(owned, monkeypatch):
    _, _, _, owner = owned
    delivery = Delivery()
    carrier = Carrier(lambda request: b"")
    held, launch = start(owned, carrier, delivery=delivery)
    launch()
    custody = held[0]
    local = owner._outstanding_attempt.local_delivery
    local.begin_process()
    monkeypatch.setattr(local, "close", lambda deadline: False)
    with pytest.raises(StateError):
        custody.observe_cleanup(Deadline.after(10))
    assert not delivery.observations and custody.completion_view is None
    delivery.closure_proven = True
    assert custody.completion_view is None


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize(
    "transition", ["install_dispatch_obligation", "arm_dispatch_obligation", "begin_attempt", "settle", "close"]
)
def test_lost_bookkeeping_reply_reconciles_exact_handles_without_dispatch(owned, monkeypatch, committed, transition):
    _, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    cls = OperationAttempt if transition == "settle" else OperationBorrow
    original = getattr(cls, transition)
    cause = ValueError("prior cause")
    control = KeyboardInterrupt("lost reply")
    control.__cause__ = cause

    def interrupt(self, *args, **kwargs):
        if committed:
            original(self, *args, **kwargs)
        raise control

    held, launch = start(owned, carrier)
    monkeypatch.setattr(cls, transition, interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        launch()
    assert caught.value is control
    assert isinstance(control.__cause__, ManagedStartControlFact)
    assert control.__cause__.__cause__ is cause
    monkeypatch.setattr(cls, transition, original)
    custody = held[0]
    custody.observe_cleanup(Deadline.after(0))
    custody.retry_bookkeeping()
    assert owner._active_borrow is None and owner._outstanding_attempt is None
    assert custody.completion_view is not None
    sent = transition in ("settle", "close")
    assert carrier.calls == int(sent)
    assert repository.inspect(RUN).launch_state is (
        ManagedLaunchState.RECEIPT_CONFIRMED
        if sent
        else ManagedLaunchState.POSSIBLE_DISPATCH
        if transition == "begin_attempt"
        else ManagedLaunchState.RESERVED
    )


@pytest.mark.parametrize("changed", ["borrow", "attempt", "row", "run", "generation"])
def test_continuation_refuses_changed_exact_binding_before_observation(owned, monkeypatch, changed):
    _, repository, record, owner = owned
    delivery = Delivery()
    carrier = Carrier(lambda request: b"")
    held, launch = start(owned, carrier, delivery=delivery)
    launch()
    custody = held[0]
    if changed == "borrow":
        owner._active_borrow = None
    elif changed == "attempt":
        owner._outstanding_attempt = OperationAttempt(OperationBorrow(owner))
    elif changed == "row":
        row = owner.inspect_lifecycle_obligation(OBLIGATION)
        monkeypatch.setattr(
            owner._repository,
            "inspect_lifecycle_obligation",
            lambda ownership, identity: replace(row, payload=b"wrong"),
        )
    elif changed == "run":
        monkeypatch.setattr(repository, "inspect", lambda identity: replace(record, created_at="wrong"))
    else:
        claim = owner._repository.inspect(owner.ownership.scope)
        changed_ownership = replace(owner.ownership, generation_id="f" * 32)
        monkeypatch.setattr(owner._repository, "inspect", lambda scope: replace(claim, ownership=changed_ownership))
    with pytest.raises(StateError):
        custody.observe_cleanup(Deadline.after(10))
    assert not delivery.observations and delivery.calls == 1
