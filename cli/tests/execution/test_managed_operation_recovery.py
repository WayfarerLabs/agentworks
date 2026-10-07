"""Real recovery fences/protocols through the synthetic protected WSL span.

No native Windows endpoint or restore-incarnation proof is supplied here.
"""

from __future__ import annotations

import gc
import sys
import weakref
from dataclasses import replace
from types import FrameType
from typing import Any

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_wire import FileRecordKind
from agentworks.execution._managed_lease_protocol import ClockObservation, encode_result
from agentworks.execution._managed_lease_wire import MAX_CLOCK_NS, WINDOW_NS
from agentworks.execution._managed_operation_recovery import ManagedOperationRecovery
from agentworks.execution._managed_run_obligation import encode_managed_run_obligation
from agentworks.execution._managed_runs import (
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.carriers.wsl2 import WSL2Carrier
from agentworks.operations import OperationOwner, RecoveredLifecycleObligation, RecoveryAttempt, RecoveryDispatch
from agentworks.vms.target_identity import compose_managed_vm_target_identity
from tests.execution.test_managed_lease_exchange import _records
from tests.execution.test_managed_operation_keeper_closing import ClosingCarrier
from tests.execution.test_recovery_guest_preparation import _ADMIN
from tests.execution.test_wsl2_platform_hold import BOOT
from tests.vms import test_recovery_vm_span as span_fixtures
from tests.vms.test_target_preparation import _MARKER

pytestmark = pytest.mark.windows
RUN = ManagedRunIdentity("a" * 32)


class Helpers(ClosingCarrier):
    def __init__(self) -> None:
        super().__init__()
        self.sample = 1000
        self.response = lambda request: _records(
            request,
            ((FileRecordKind.RESULT, encode_result(ClockObservation(self.sample))), (FileRecordKind.FINISHED, b"")),
        )


@pytest.fixture(params=["managed-start", "managed-operation-keeper"])
def recovery_setup(tmp_path, monkeypatch, request):
    original = OperationOwner.register_lifecycle_obligation

    def register(owner, kind, **options):
        if kind == "file-call":
            database = owner._repository._controller_identity
            guest = VMGuestIdentity(_MARKER, BOOT, 4096)
            spec = ManagedRunSpec(
                compose_managed_vm_target_identity(database.get_vm("box"), ProviderLocator("wsl2:registration"), guest),
                _ADMIN,
                ManagedShellIdentity(None, None),
                ManagedRunOwner(ManagedRunOwnerKind.OPERATION, owner.ownership.operation_id),
                ManagedRunLifetime.OPERATION,
            )
            repository = ManagedRunRepository(database)
            record = repository.reserve(
                spec, output_policy=ManagedOutputPolicy(ManagedOutputMode.DISCARD), identity=RUN
            )
            repository.mark_possible_dispatch(record)
            kind = request.param
            options.update(payload_version=1, payload=encode_managed_run_obligation(RUN.run_id))
        return original(owner, kind, **options)

    monkeypatch.setattr(OperationOwner, "register_lifecycle_obligation", register)
    fixture = span_fixtures.setup.__wrapped__(tmp_path, monkeypatch)  # type: ignore[attr-defined]
    values = next(fixture)
    database, owner, old_id, _, _, _, _, span, _ = values
    prepared = span.open(Deadline.after(10))
    helpers = Helpers()
    prior_execute = WSL2Carrier.execute

    def execute(carrier, invocation, *, io, deadline, custody):
        return helpers.execute(invocation, io=io, deadline=deadline, custody=custody)  # type: ignore[no-untyped-call]

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    row = next(row for row in owner.list_pending_lifecycle_obligations() if row.obligation_id == old_id)
    repository = ManagedRunRepository(database)
    recovery = ManagedOperationRecovery(repository, owner, row, prepared)
    try:
        yield database, owner, row, span, prepared, helpers, recovery, repository
    finally:
        try:
            recovery.drain(Deadline.after(1))
        finally:
            monkeypatch.setattr(WSL2Carrier, "execute", prior_execute)
            fixture.close()


def test_fresh_fixed_ceiling_and_current_poll_without_high_water_writes(recovery_setup) -> None:
    database, owner, row, span, _, helpers, recovery, _ = recovery_setup
    changes = database._conn.total_changes
    accepted = 1000
    for sample in (1000, 2000, 1000 + WINDOW_NS, 0):
        helpers.sample = sample
        deadline = Deadline.after(2)
        with span.action(deadline) as context:
            candidate = recovery.observe_clock(context, deadline)
        assert recovery.last_clock is candidate
        accepted = max(accepted, sample)
        assert recovery._accepted_sample_ns == accepted
        assert recovery.ceiling_ns == 1000 + WINDOW_NS
        assert recovery.authority_elapsed == (sample >= recovery.ceiling_ns)
        assert recovery.dispatch is None and recovery.attempt is None
    helpers.complete = False
    with span.action(Deadline.after(2)) as context:
        candidate = recovery.observe_clock(context, Deadline.after(1))
    assert candidate.result is None and not recovery.authority_elapsed
    assert database._conn.total_changes == changes
    assert all(request.lease is None for request in helpers.requests)
    assert (
        next(value for value in owner.list_pending_lifecycle_obligations() if value.obligation_id == row.obligation_id)
        == row
    )


def test_exact_stop_precedes_clock_and_absent_controller_is_only_observation(recovery_setup) -> None:
    _, owner, row, span, _, helpers, recovery, _ = recovery_setup
    with span.action(Deadline.after(2)) as context:
        stop = recovery.request_stop(context, Deadline.after(1))
    assert stop.observation is not None and recovery.last_stop is stop
    assert recovery.ceiling_ns is None and helpers.requests == []
    with span.action(Deadline.after(2)) as context:
        observation = recovery.observe_cleanup(context, Deadline.after(1))
    assert observation.observation is not None and observation.observation.controller is not None
    assert recovery.last_observation is observation and not recovery.authority_elapsed
    assert (
        next(value for value in owner.list_pending_lifecycle_obligations() if value.obligation_id == row.obligation_id)
        == row
    )


@pytest.mark.parametrize(
    "fault", ["kind", "version", "revision", "registered", "resolved", "run", "owner", "boot", "vm"]
)
def test_invalid_passive_binding_has_no_helper_dispatch(recovery_setup, fault) -> None:
    _, owner, row, _, prepared, helpers, _, repository = recovery_setup
    if fault in {"kind", "version", "revision", "registered", "resolved", "run"}:
        changes = {
            "kind": {"obligation_kind": "other"},
            "version": {"payload_version": 2},
            "revision": {"payload_revision": 1},
            "registered": {"state": LifecycleObligationState.REGISTERED},
            "resolved": {"state": LifecycleObligationState.RESOLVED},
            "run": {"payload": encode_managed_run_obligation("f" * 32)},
        }
        row = replace(row, **changes[fault])
    elif fault == "owner":
        owner = OperationOwner(owner._repository, owner.ownership)
    elif fault == "boot":
        prepared = replace(prepared, guest=replace(prepared.guest, init_start_ticks=1))
    else:
        prepared = replace(prepared, target=replace(prepared.target, name="other"))
    with pytest.raises((StateError, ValidationError)):
        ManagedOperationRecovery(repository, owner, row, prepared)
    assert helpers.calls == 0


@pytest.mark.parametrize("fault", ["escaped", "expired", "budget", "generation", "guest"])
def test_action_refusal_precedes_open_and_attempt(recovery_setup, fault) -> None:
    database, owner, _, span, prepared, helpers, recovery, _ = recovery_setup
    deadline = Deadline.after(1)
    if fault == "escaped":
        context = prepared
        with pytest.raises(StateError):
            recovery.request_stop(context, deadline)
    else:
        with span.action(deadline) as context:
            if fault == "expired":
                object.__setattr__(deadline, "expires_at", 0.0)
            elif fault == "budget":
                deadline = Deadline.after(10)
            elif fault == "generation":
                OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
            else:
                context = replace(context, guest=replace(context.guest, init_start_ticks=1))
            with pytest.raises((StateError, ValidationError)):
                recovery.request_stop(context, deadline)
    assert helpers.calls == 0 and recovery.dispatch is None and recovery.attempt is None


@pytest.mark.parametrize("fault", ["overflow", "late", "unknown"])
def test_unusable_clock_does_not_authorize_expiry(recovery_setup, fault) -> None:
    _, _, _, span, _, helpers, recovery, _ = recovery_setup
    if fault == "overflow":
        helpers.sample = MAX_CLOCK_NS
    elif fault == "unknown":
        helpers.dispatch = Dispatch.UNKNOWN
    deadline = Deadline.after(2)
    original = helpers.response

    def response(request):
        result = original(request)
        if fault == "late":
            object.__setattr__(deadline, "expires_at", 0.0)
        return result

    helpers.response = response
    with span.action(deadline) as context:
        candidate = recovery.observe_clock(context, deadline)
    assert recovery.last_clock is candidate and recovery.ceiling_ns is None and not recovery.authority_elapsed
    if fault == "unknown":
        facts = recovery.drain(Deadline.after(1))
        assert facts.local_settled and not facts.helper_termination_known and facts.dispatch_retained
        with pytest.raises(StateError):
            recovery.request_stop(context, Deadline.after(1))
        assert helpers.calls == 1


@pytest.mark.parametrize("phase", ["open", "begin", "settle", "close"])
def test_interrupted_local_transition_retains_exact_custody_without_replay(recovery_setup, monkeypatch, phase) -> None:
    _, _, _, span, _, helpers, recovery, _ = recovery_setup
    error = KeyboardInterrupt()
    cls, method = {
        "open": (RecoveredLifecycleObligation, "open_dispatch"),
        "begin": (RecoveryDispatch, "begin_attempt"),
        "settle": (RecoveryAttempt, "settle"),
        "close": (RecoveryDispatch, "close"),
    }[phase]
    original = getattr(cls, method)

    def interrupted(resource):
        original(resource)
        raise error

    monkeypatch.setattr(cls, method, interrupted)
    with pytest.raises(KeyboardInterrupt) as caught, span.action(Deadline.after(2)) as context:
        recovery.observe_clock(context, Deadline.after(1))
    assert caught.value is error and recovery.failed
    assert helpers.calls == (0 if phase in {"open", "begin"} else 1)
    monkeypatch.setattr(cls, method, original)
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and facts.helper_termination_known and not facts.dispatch_retained
    assert recovery.dispatch is None and recovery.attempt is None
    with span.action(Deadline.after(1)):
        pass
    if phase in {"settle", "close"}:
        assert recovery.last_clock is not None and recovery.ceiling_ns == 1000 + WINDOW_NS


@pytest.mark.parametrize("method", ["observe_clock", "request_stop", "observe_cleanup"])
def test_interrupted_execute_local_drain_never_settles_remote_or_retains_payload(recovery_setup, method) -> None:
    _, _, _, span, _, helpers, recovery, _ = recovery_setup

    class Payload:
        pass

    def caller() -> weakref.ReferenceType[Payload]:
        payload = Payload()
        error = KeyboardInterrupt()
        helpers.interrupt = error
        helpers.closing_error = error
        try:
            with span.action(Deadline.after(2)) as context:
                getattr(recovery, method)(context, Deadline.after(1))
        except KeyboardInterrupt as escaped:
            assert escaped is error
        finally:
            helpers.interrupt = None
            helpers.closing_error = None
        return weakref.ref(payload)

    reference = caller()
    gc.collect()
    assert reference() is None and recovery.failed and helpers.calls == 1
    assert recovery.attempt is not None and recovery.dispatch is not None
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and not facts.helper_termination_known and facts.dispatch_retained
    with pytest.raises(StateError), span.action(Deadline.after(1)):
        pass
    assert helpers.calls == 1


def test_known_termination_waits_for_exact_local_delivery_drain(recovery_setup, monkeypatch) -> None:
    _, _, _, span, _, helpers, recovery, _ = recovery_setup
    helpers.unsettled = True
    from agentworks.execution._delivery_custody import LocalDeliveryCustody

    original = LocalDeliveryCustody.close
    monkeypatch.setattr(LocalDeliveryCustody, "close", lambda self, deadline: False)
    with span.action(Deadline.after(2)) as context:
        candidate = recovery.observe_clock(context, Deadline.after(1))
    assert candidate.result is None and recovery.attempt is not None
    custody = recovery.attempt.local_delivery
    assert helpers.custody is custody
    facts = recovery.drain(Deadline.after(1))
    assert not facts.local_settled and facts.helper_termination_known and facts.dispatch_retained
    with pytest.raises(StateError):
        recovery.observe_clock(context, Deadline.after(1))
    monkeypatch.setattr(LocalDeliveryCustody, "close", original)
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and facts.helper_termination_known and not facts.dispatch_retained
    assert helpers.calls == 1


def test_protected_carrier_revalidates_generation_at_native_delivery(recovery_setup, monkeypatch) -> None:
    database, owner, _, span, _, helpers, recovery, _ = recovery_setup
    previous = WSL2Carrier.validate

    def validate(carrier, invocation, *, io):
        previous(carrier, invocation, io=io)
        OperationOwner.recover(database.operations, owner.ownership, "e" * 32)

    with span.action(Deadline.after(2)) as context:
        monkeypatch.setattr(WSL2Carrier, "validate", validate)
        with pytest.raises(StateError):
            recovery.request_stop(context, Deadline.after(1))
    assert helpers.calls == 0 and recovery.attempt is not None
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and not facts.helper_termination_known and facts.dispatch_retained


@pytest.mark.parametrize("fault", ["revision", "run_owner"])
def test_each_exchange_checks_current_persisted_binding(recovery_setup, fault) -> None:
    database, owner, row, span, _, helpers, recovery, _ = recovery_setup
    if fault == "revision":
        bound = owner.rebind_lifecycle_obligation(
            row.obligation_id, row.obligation_kind, payload_version=1, payload=row.payload
        )
        updated = bound.publish_payload(
            expected_revision=0, payload_version=1, payload=encode_managed_run_obligation("f" * 32)
        )
        assert updated.payload_revision == 1
    else:
        database._conn.execute("UPDATE execution_runs SET owner_id = ? WHERE run_id = ?", ("f" * 32, RUN.run_id))
        database._conn.commit()
    with span.action(Deadline.after(2)) as context, pytest.raises(StateError):
        recovery.request_stop(context, Deadline.after(1))
    assert helpers.calls == 0
    assert not recovery.drain(Deadline.after(1)).dispatch_retained


def test_regressing_clock_above_fixed_ceiling_is_not_elapsed_authority(recovery_setup) -> None:
    _, _, _, span, _, helpers, recovery, _ = recovery_setup
    for sample, elapsed in (
        (1000, False),
        (1000 + 2 * WINDOW_NS, True),
        (1000 + WINDOW_NS, False),
        (1000 + WINDOW_NS, False),
        (1000 + 3 * WINDOW_NS, True),
    ):
        helpers.sample = sample
        with span.action(Deadline.after(2)) as context:
            candidate = recovery.observe_clock(context, Deadline.after(1))
        assert candidate.result == ClockObservation(sample) and candidate.issue is None
        assert recovery.last_clock is candidate and recovery.ceiling_ns == 1000 + WINDOW_NS
        assert recovery.authority_elapsed is elapsed
        if sample == 1000 + WINDOW_NS:
            assert recovery._accepted_sample_ns == 1000 + 2 * WINDOW_NS


@pytest.mark.parametrize("after", [False, True])
def test_interrupted_abort_reply_can_be_drained_again_without_helper_replay(recovery_setup, monkeypatch, after) -> None:
    _, _, _, span, _, helpers, recovery, _ = recovery_setup
    begin_error, abort_error = KeyboardInterrupt(), KeyboardInterrupt()
    original_begin = RecoveryDispatch.begin_attempt
    original_abort = RecoveryDispatch._abort_unreturned_attempt

    def begin(dispatch):
        original_begin(dispatch)
        raise begin_error

    def abort(dispatch):
        if after:
            original_abort(dispatch)
        raise abort_error

    monkeypatch.setattr(RecoveryDispatch, "begin_attempt", begin)
    with pytest.raises(KeyboardInterrupt) as caught, span.action(Deadline.after(2)) as context:
        recovery.observe_clock(context, Deadline.after(1))
    assert caught.value is begin_error and helpers.calls == 0
    monkeypatch.setattr(RecoveryDispatch, "_abort_unreturned_attempt", abort)
    with pytest.raises(KeyboardInterrupt) as caught:
        recovery.drain(Deadline.after(1))
    assert caught.value is abort_error and recovery.dispatch is not None and recovery._beginning
    monkeypatch.setattr(RecoveryDispatch, "_abort_unreturned_attempt", original_abort)
    facts = recovery.drain(Deadline.after(1))
    assert not facts.dispatch_retained and facts.local_settled and facts.helper_termination_known
    with span.action(Deadline.after(1)):
        pass
    assert recovery.dispatch is None and recovery.attempt is None and helpers.calls == 0


@pytest.mark.parametrize("dispatch_cleared", [False, True])
def test_interrupted_unreturned_begin_field_clear_preserves_repeat_drain(
    recovery_setup, monkeypatch, dispatch_cleared
) -> None:
    _, owner, _, span, _, helpers, recovery, _ = recovery_setup
    begin_error, drain_error = KeyboardInterrupt(), KeyboardInterrupt()
    original = RecoveryDispatch.begin_attempt

    def begin(dispatch):
        original(dispatch)
        raise begin_error

    monkeypatch.setattr(RecoveryDispatch, "begin_attempt", begin)
    with pytest.raises(KeyboardInterrupt) as caught, span.action(Deadline.after(2)) as context:
        recovery.observe_clock(context, Deadline.after(1))
    assert caught.value is begin_error and helpers.calls == 0
    retained = recovery.dispatch
    assert retained is not None

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        boundary = (
            recovery.dispatch is None if dispatch_cleared else not recovery._beginning and recovery.dispatch is retained
        )
        if (
            event == "line"
            and frame.f_code is ManagedOperationRecovery.drain.__code__
            and retained._closed
            and boundary
        ):
            raise drain_error
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            recovery.drain(Deadline.after(1))
        assert caught.value is drain_error
    finally:
        sys.settrace(None)
    assert owner._active_recovery_dispatch is None and owner._outstanding_attempt is None
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and facts.helper_termination_known and not facts.dispatch_retained
    with span.action(Deadline.after(1)):
        pass
    assert recovery.dispatch is None and recovery.attempt is None and not recovery._beginning and helpers.calls == 0
