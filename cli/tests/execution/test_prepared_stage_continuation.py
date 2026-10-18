"""Exact same-process stage continuations over real SQLite and local custody."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._prepared_helper_obligation import PREPARED_HELPER_OBLIGATION_KIND
from agentworks.execution._runtime_prerequisite import HelperClosureExpectation
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, PreparedInvocation
from agentworks.operations import LifecycleObligation, OperationAttempt, OperationBorrow, OperationOwner
from tests.execution.test_prepared_helper_stage import TARGET, Carrier, pending, stage


@pytest.fixture
def owned(tmp_path: Path) -> Generator[tuple[Database, OperationOwner, Carrier]]:
    database = Database(tmp_path / "continuation.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, TARGET.name), "test")
    try:
        yield database, owner, Carrier(owner)
    finally:
        database.close()


class Delivery(Carrier):
    def __init__(self, owner: OperationOwner, expectation: HelperClosureExpectation) -> None:
        super().__init__(owner)
        self.expectation = expectation
        self.proven = False
        self.observed: list[tuple[Deadline, LocalDeliveryCustody]] = []

    @property
    def closure_proven(self) -> bool:
        return self.proven

    def observe_closure(self, *, deadline: Deadline, custody: LocalDeliveryCustody) -> bool:
        assert custody is self.custody and custody.settled
        self.observed.append((deadline, custody))
        return self.proven

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        report = super().execute(invocation, io=io, deadline=deadline, custody=custody)
        assert self.requests[-1].nonce == self.expectation.nonce
        return report


def test_uncertain_retains_actual_borrow_and_attempt_without_redelivery(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.code = 255
    assert not prepared.begin()
    active = prepared._active
    assert active is not None and owner._active_borrow is active.borrow and not active.borrow.closed
    assert owner._outstanding_attempt is active.operation.outstanding_attempt
    for action in (owner.borrow, lambda: prepared.observe_cleanup(Deadline.after(30)), prepared.cleanup):
        with pytest.raises(StateError):
            action()
    assert prepared._active is active and len(carrier.requests) == 1
    assert set(pending(owner)) == {PREPARED_HELPER_OBLIGATION_KIND, "carrier-dispatch"}


def test_local_drain_uses_actual_custody_and_never_reopens_preparation(owned, monkeypatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.unsettled = True
    assert not prepared.begin()
    active = prepared._active
    assert active is not None and active.operation.outstanding_attempt is not None
    custody = active.operation.outstanding_attempt.local_delivery
    assert custody is carrier.custody and not custody.settled
    monkeypatch.setattr(prepared, "_deadline", Deadline.after(0))
    prepared.observe_cleanup(Deadline.after(30))
    assert custody.settled and active.borrow.closed and not prepared.helper_uncertain and prepared.failed
    assert owner._outstanding_attempt is None and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    for action in (lambda: prepared.append(b"abc"), lambda: prepared.prepare_activation("f" * 32, b"d" * 32)):
        with pytest.raises(StateError):
            action()
    assert len(carrier.requests) == 1
    carrier.unsettled = False
    assert prepared.cleanup(deadline=Deadline.after(30))


def test_native_closure_false_then_true_under_fresh_route_and_same_custody(owned) -> None:
    _, owner, carrier = owned
    deliveries: list[Delivery] = []
    routes: list[Deadline] = []

    def factory(expectation: HelperClosureExpectation) -> Delivery:
        delivery = Delivery(owner, expectation)
        delivery.unsettled = True
        deliveries.append(delivery)
        return delivery

    prepared = stage(owned, before_dispatch=routes.append, new_helper_delivery=factory)
    assert not prepared.begin()
    active = prepared._active
    delivery = deliveries[0]
    assert active is not None and active.operation.wraps(delivery) and carrier.requests == []
    fresh = Deadline.after(30)
    with pytest.raises(StateError):
        prepared.observe_cleanup(fresh)
    assert len(delivery.observed) == 1 and routes == [prepared.deadline, fresh]
    assert len(delivery.requests) == 1 and not active.borrow.closed

    # Model a later positive native observation, rather than local SSH exit.
    def observe(*, deadline: Deadline, custody: LocalDeliveryCustody) -> bool:
        assert deadline is fresh and custody is delivery.custody
        delivery.proven = True
        delivery.observed.append((deadline, custody))
        return True

    delivery.observe_closure = observe
    prepared.observe_cleanup(fresh)
    assert not prepared.helper_uncertain and active.borrow.closed and prepared.failed
    assert len(delivery.requests) == 1 and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]


@pytest.mark.parametrize("point", ["register", "arm", "settle", "close", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_exact_bookkeeping_never_replays(owned, monkeypatch, point: str, committed: bool) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    if point == "resolve":
        assert prepared.begin()
    if point == "register":
        target, name = owner, "register_lifecycle_obligation"
    else:
        target, name = {
            "arm": (LifecycleObligation, "mark_possible_effect"),
            "settle": (OperationAttempt, "settle"),
            "close": (OperationBorrow, "close"),
            "resolve": (LifecycleObligation, "resolve"),
        }[point]
    original = getattr(target, name)
    interrupted = KeyboardInterrupt("local reply interrupted")

    def interrupt(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise interrupted

    with monkeypatch.context() as patch:
        patch.setattr(target, name, interrupt)
        with pytest.raises(KeyboardInterrupt) as caught:
            prepared.cleanup() if point == "resolve" else prepared.begin()
        assert caught.value is interrupted
    before = len(carrier.requests)
    if prepared.helper_uncertain:
        prepared.observe_cleanup(Deadline.after(30))
    else:
        prepared.retry_bookkeeping()
    assert len(carrier.requests) == before and not prepared.helper_uncertain
    assert not prepared.registration_uncertain and not prepared.cleanup_resolution_uncertain
    assert all(value is not interrupted for value in vars(prepared).values())
    if point in ("register", "arm", "resolve"):
        assert prepared.cleaned and pending(owner) == []
    else:
        assert prepared.failed and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]


@pytest.mark.parametrize("point", ["register", "arm"])
def test_dispatch_admission_lost_reply_proves_no_carrier_entry(owned, monkeypatch, point: str) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    repository = owner._repository
    name = "register_lifecycle_obligation" if point == "register" else "mark_lifecycle_obligation_possible_effect"
    original = getattr(repository, name)

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        identity = kwargs.get("obligation_id") if point == "register" else args[1]
        if identity != prepared.obligation_id:
            raise KeyboardInterrupt("generic dispatch bookkeeping reply lost")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(repository, name, interrupted)
        with pytest.raises(KeyboardInterrupt):
            prepared.begin()
    assert carrier.requests == [] and prepared.helper_uncertain
    prepared.observe_cleanup(Deadline.after(30))
    assert not prepared.helper_uncertain and prepared.cleaned and pending(owner) == []
    with pytest.raises(StateError):
        prepared.append(b"abc")
    assert carrier.requests == []


def test_route_control_drain_after_stop_admission_has_no_remote_work(owned) -> None:
    _, owner, carrier = owned
    control = KeyboardInterrupt("route refused")

    def route(deadline: Deadline) -> None:
        assert deadline is prepared.deadline
        raise control

    prepared = stage(owned, before_dispatch=route)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.begin()
    assert caught.value is control
    owner.stop_admission()
    prepared.observe_cleanup(Deadline.after(30))
    assert not prepared.helper_uncertain and prepared.failed and carrier.requests == []
    assert prepared.cleaned and pending(owner) == []


@pytest.mark.parametrize("changed", ["borrow", "attempt", "guest", "carrier"])
def test_changed_exact_binding_refuses_continuation(owned, changed: str) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.unsettled = True
    assert not prepared.begin()
    active = prepared._active
    assert active is not None and active.closure_expectation is not None
    if changed == "borrow":
        owner._active_borrow = OperationBorrow(owner)
    elif changed == "attempt":
        owner._outstanding_attempt = OperationAttempt(active.borrow)
    elif changed == "guest":
        active.closure_expectation = replace(active.closure_expectation, guest=None)
    else:
        prepared._carrier = Carrier(owner)
    with pytest.raises(StateError):
        prepared.observe_cleanup(Deadline.after(30))
    assert prepared._active is active and len(carrier.requests) == 1
    assert carrier.custody is not None
    carrier.custody.close(Deadline.after(30))


@pytest.mark.parametrize("deadline", [Deadline.after(None), Deadline.after(0)])
def test_observation_requires_live_finite_budget(owned, deadline: Deadline) -> None:
    prepared = stage(owned)
    with pytest.raises(ValidationError):
        prepared.observe_cleanup(deadline)


def test_chunk_control_retains_no_payload_or_escaping_exception(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    secret = b"abc"
    control = KeyboardInterrupt("private data")
    carrier.error = control
    with pytest.raises(KeyboardInterrupt):
        prepared.append(secret)
    active = prepared._active
    assert active is not None and active.candidate is None
    assert all(value is not control and value != secret for value in vars(prepared).values())
    assert not hasattr(active, "prepared") and len(carrier.requests) == 2


def test_stale_owner_refuses_local_drain_and_keeps_exact_debt(owned) -> None:
    database, owner, carrier = owned
    prepared = stage(owned)
    carrier.unsettled = True
    assert not prepared.begin()
    active = prepared._active
    successor = OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
    with pytest.raises(StateError):
        prepared.observe_cleanup(Deadline.after(30))
    assert prepared._active is active and prepared.helper_uncertain
    assert set(pending(successor)) == {PREPARED_HELPER_OBLIGATION_KIND, "carrier-dispatch"}
    assert carrier.custody is not None and not carrier.custody.settled
    carrier.custody.close(Deadline.after(30))


@pytest.mark.parametrize("point", ["settle", "close"])
def test_cleaned_reply_retained_across_local_interruption_resolves_without_execute(owned, monkeypatch, point) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    target, name = (OperationAttempt, "settle") if point == "settle" else (OperationBorrow, "close")
    original = getattr(target, name)

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt("cleanup bookkeeping interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(target, name, interrupted)
        with pytest.raises(KeyboardInterrupt):
            prepared.cleanup()
    assert len(carrier.requests) == 2 and prepared.helper_uncertain
    prepared.observe_cleanup(Deadline.after(30))
    assert prepared.cleaned and not prepared.helper_uncertain and pending(owner) == []
    assert len(carrier.requests) == 2


@pytest.mark.parametrize("committed", [False, True])
def test_actual_stage_database_registration_loss_reconciles_before_return(owned, monkeypatch, committed) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    repository = owner._repository
    original = repository.register_lifecycle_obligation

    def interrupted(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise KeyboardInterrupt("database registration reply lost")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "register_lifecycle_obligation", interrupted)
        with pytest.raises(KeyboardInterrupt):
            prepared.begin()
    prepared.retry_bookkeeping()
    assert not owner._transition_uncertain and prepared.cleaned and pending(owner) == []
    assert carrier.requests == []


@pytest.mark.parametrize("observation", ["mismatch", "unavailable"])
def test_registration_mismatch_and_observation_failure_never_resolve(owned, monkeypatch, observation) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    original_register = owner.register_lifecycle_obligation

    def interrupted(*args, **kwargs):
        original_register(*args, **kwargs)
        raise KeyboardInterrupt("registration interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(owner, "register_lifecycle_obligation", interrupted)
        with pytest.raises(KeyboardInterrupt):
            prepared.begin()
    original_inspect = owner.inspect_lifecycle_obligation

    def changed(identity):
        if observation == "unavailable":
            raise StateError("exact row observation unavailable")
        row = original_inspect(identity)
        assert row is not None
        return replace(row, payload=b"other stage")

    with monkeypatch.context() as patch:
        patch.setattr(owner, "inspect_lifecycle_obligation", changed)
        with pytest.raises(StateError):
            prepared.retry_bookkeeping()
    assert prepared.registration_uncertain and not prepared.cleaned
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND] and carrier.requests == []


def test_actual_cleanup_route_uses_fresh_budget(owned, monkeypatch) -> None:
    _, _, carrier = owned
    routes: list[Deadline] = []
    prepared = stage(owned, before_dispatch=routes.append)
    assert prepared.begin() and prepared.append(b"abc")
    original = prepared.deadline
    monkeypatch.setattr(prepared, "_deadline", Deadline.after(0))
    fresh = Deadline.after(30)
    assert prepared.cleanup(deadline=fresh)
    assert routes == carrier.deadlines == [original, original, fresh]


def test_chunk_pre_entry_failure_does_not_resolve_previously_created_stage(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()

    def refuse(deadline: Deadline) -> None:
        raise KeyboardInterrupt("chunk route refused")

    prepared._before_dispatch = refuse
    with pytest.raises(KeyboardInterrupt):
        prepared.append(b"abc")
    prepared.observe_cleanup(Deadline.after(30))
    assert not prepared.helper_uncertain and prepared.failed and not prepared.cleaned
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND] and len(carrier.requests) == 1
    prepared._before_dispatch = None
    assert prepared.cleanup() and pending(owner) == []
