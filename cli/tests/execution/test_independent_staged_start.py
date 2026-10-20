"""Independent START composes bounded staging with exact RESOURCE custody."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_start_exchange as exchange_module
from agentworks.execution import _managed_start_preparation as preparation_module
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import ManagedExecutionControlFact
from agentworks.execution._managed_runs import ManagedLaunchState, ManagedRunIdentity
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._managed_start_protocol import decode_request
from agentworks.execution._staged_helper_protocol import decode_request as decode_activation
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier
from agentworks.execution.models import Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.operations import OperationBorrow

from .test_independent_execution_reads import observer as observer
from .test_independent_execution_start import available as available
from .test_independent_execution_start import start
from .test_managed_staged_operation import StagedStartCarrier
from .test_managed_start_operation import _records
from .test_prepared_stage_closing import ScriptedCarrier as ClosingCarrier

if TYPE_CHECKING:
    from agentworks.execution.access import ExecutionAccess

pytestmark = pytest.mark.windows


class IndependentCarrier(StagedStartCarrier):
    def __init__(self, owner) -> None:
        super().__init__(owner)
        self.receipt = True
        self.activation_code: int | None = 0
        self.activation_dispatch = Dispatch.SENT
        self.activation_custody: LocalDeliveryCustody | None = None
        self.activation_unsettled = False
        self.proven = True
        self.observed = 0
        self.deliveries: list[NativeDelivery] = []

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        assert isinstance(io.input, FiniteInput)
        prefix = self.activation_prefix
        if not io.input.data.startswith(prefix):
            if not self.owner._close_requested.is_set():
                return super().execute(invocation, io=io, deadline=deadline, custody=custody)
            self.bodies.append(ProxmoxCarrier._request_body(invocation, io))
            report = ClosingCarrier.execute(self, invocation, io=io, deadline=deadline, custody=custody)
            self.events.append(self.requests[-1].operation.value)
            return report
        self.bodies.append(ProxmoxCarrier._request_body(invocation, io))
        self.events.append("activate")
        assert custody is not None and self.owner._active_borrow is not None
        assert self.owner._outstanding_attempt is not None
        assert self.owner._outstanding_attempt.local_delivery is custody
        self.activation_custody = custody
        if self.activation_unsettled:
            custody.begin_process()
        self.deadlines.append(deadline)
        control = decode_activation(io.input.data[len(prefix) :])
        assert self.data.startswith(FIXED_BUNDLE.prefix)
        assert hashlib.sha256(self.data).digest() == control.sha256
        assert len(self.data) == control.reference._ownership._length
        self.activated = decode_request(bytes(self.data[len(FIXED_BUNDLE.prefix) :]))
        assert self.activated.nonce == control.nonce
        if self.activation_error is not None:
            raise self.activation_error
        assert isinstance(io.output, SinkOutput)
        if self.activation_dispatch is Dispatch.SENT:
            io.output.stdout.try_write(
                memoryview(
                    f"AGW_RUNTIME_1:{self.activated.nonce}:ready:0\n".encode()
                    + _records(self.activated, receipt=self.receipt)
                )
            )
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(
            self.activation_dispatch,
            ExitStatus(self.activation_code) if self.activation_code is not None else None,
            stdout=output,
            stderr=output,
        )

    def new_delivery(self, expectation):
        delivery = NativeDelivery(self, expectation)
        self.deliveries.append(delivery)
        return delivery


class NativeDelivery:
    def __init__(self, carrier, expectation) -> None:
        self.carrier, self.expectation = carrier, expectation
        self.activation = False
        self.custody = None

    @property
    def features(self):
        return self.carrier.features

    @property
    def closure_proven(self):
        return not self.activation or self.carrier.proven

    def validate(self, invocation, *, io):
        self.carrier.validate(invocation, io=io)

    def execute(self, invocation, *, io, deadline, custody=None):
        self.activation = io.input.data.startswith(self.carrier.activation_prefix)
        self.custody = custody
        return self.carrier.execute(invocation, io=io, deadline=deadline, custody=custody)

    def observe_closure(self, *, deadline, custody):
        assert custody is self.custody
        self.carrier.observed += 1
        return self.closure_proven


@pytest.fixture
def staged(available):
    database, repository, operation, access, main, workflow = available
    carrier = IndependentCarrier(workflow.owner)
    operation._native_binding = replace(operation._native_binding, carrier=carrier)
    access._carrier = carrier
    return database, repository, operation, access, carrier, workflow, main


def bulk_start(access: ExecutionAccess) -> JobRef:
    return start(access, stdin=Input.bytes(bytes(range(250)) * 40))


def test_independent_exact_binary_and_source_have_one_null_tail_and_no_clock(staged):
    database, repository, operation, access, carrier, workflow, main = staged
    source = "printf '%s' 'original-source'"
    job = access.start(
        Script(source, Shell.SH),
        profile=Protection.MANAGED,
        lifetime=Lifetime.INDEPENDENT,
        stdin=Input.bytes(bytes(range(250)) * 40),
        output=Output.discard(),
    )
    assert carrier.activated.job.stdin == bytes(range(250)) * 40
    assert carrier.activated.job.source == source.encode()
    assert carrier.activated.job.operation_lease is None
    assert bytes(carrier.data[-512:]) == b"null" + b" " * 508
    assert all(len(body) <= 65_536 for body in carrier.bodies)
    assert main.clock.calls == 0 and carrier.events.count("activate") == 1
    assert operation.active_inline_calls == () and operation.managed_runs == ()
    (cleanup,) = operation._prepared_stages
    assert cleanup._stage._activation_settled
    payloads = database._conn.execute("SELECT payload FROM lifecycle_obligations").fetchall()
    assert all(b"original-source" not in row[0] and bytes(range(250)) * 40 not in row[0] for row in payloads)
    record = repository.inspect(ManagedRunIdentity(job.run_id))
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert workflow.owner._released and operation._prepared_stages == []
    assert repository.inspect(record.identity) == record
    assert all(len(body) <= 65_536 for body in carrier.bodies)


def test_structural_refusal_precedes_stage_reservation_and_borrow(staged, monkeypatch):
    _, repository, operation, access, carrier, workflow, _ = staged

    def refuse(*args, **kwargs):
        raise ValidationError("structural envelope")

    monkeypatch.setattr(carrier, "validate", refuse)
    monkeypatch.setattr(repository, "reserve", lambda *args, **kwargs: pytest.fail("reserve"))
    monkeypatch.setattr(workflow.owner, "borrow", lambda: pytest.fail("borrow"))
    with pytest.raises(ValidationError):
        bulk_start(access)
    assert carrier.events == [] and operation._prepared_stages == []


def test_staging_completes_before_actual_start_borrow(staged, monkeypatch):
    _, repository, operation, access, carrier, workflow, _ = staged
    original = workflow.owner.borrow
    seen = []

    def borrow():
        assert workflow.owner._active_borrow is None
        seen.append(len(carrier.data))
        return original()

    monkeypatch.setattr(workflow.owner, "borrow", borrow)
    budget = Deadline.after(30)
    access._deadline = lambda: budget
    job = bulk_start(access)
    assert seen[-1] == len(carrier.data) and seen[0] == 0
    assert all(deadline is budget for deadline in carrier.deadlines)
    assert repository.inspect(ManagedRunIdentity(job.run_id)).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED


def test_final_preparation_refusal_retains_cleanup_without_actual_start(staged, monkeypatch):
    _, repository, operation, access, carrier, workflow, _ = staged

    def refuse(self, control=None):
        assert control is not None
        raise ValidationError("final activation")

    monkeypatch.setattr(preparation_module.ManagedStartPreparation, "finish", refuse)
    monkeypatch.setattr(repository, "reserve", lambda *args, **kwargs: pytest.fail("reserve"))
    with pytest.raises(ValidationError):
        bulk_start(access)
    (cleanup,) = operation._prepared_stages
    assert cleanup._stage._activation_settled and "activate" not in carrier.events
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert cleanup._stage.cleaned and workflow.owner._released


@pytest.mark.parametrize("native", [False, True])
def test_closed_helper_with_unknown_launch_releases_borrow_and_cleans_exact_stage(staged, native):
    _, repository, operation, access, carrier, workflow, _ = staged
    carrier.receipt = False
    if native:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=carrier.new_delivery)
    with pytest.raises(StateError):
        bulk_start(access)
    (active,) = operation.active_inline_calls
    assert active.borrow.closed and active.start.local_delivery is carrier.activation_custody
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert active.start.stage._activation_settled
    with pytest.raises(StateError):
        workflow.owner.borrow()
    with pytest.raises(StateError):
        start(access)
    with pytest.raises(StateError):
        workflow.routes[0].views.files.stat(PurePosixPath("/tmp/other"))
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert operation._prepared_stages == [] and active.start.stage.cleaned
    assert not workflow.owner._released
    assert [row.obligation_kind for row in workflow.owner.list_pending_lifecycle_obligations()] == ["managed-start"]
    assert carrier.events.count("activate") == 1


@pytest.mark.parametrize("native", [False, True])
def test_closing_bookkeeping_retains_closed_unknown_start_until_finish(staged, native):
    _, repository, operation, access, carrier, workflow, _ = staged
    carrier.receipt = False
    if native:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=carrier.new_delivery)
    with pytest.raises(StateError):
        bulk_start(access)
    (active,) = operation.active_inline_calls
    record = repository.inspect(active.start.receipt.identity)
    obligations = workflow.owner.list_pending_lifecycle_obligations()
    events = list(carrier.events)
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    operation.retry_inline_bookkeeping(finishing=True)
    assert active.borrow.closed and not active.operation.requires_owner_retention
    assert workflow.owner._active_borrow is None and workflow.owner._outstanding_attempt is None
    with pytest.raises(StateError):
        operation.finish()
    assert operation.active_inline_calls == (active,) and not operation._finished
    assert not active.start.stage.cleaned and operation._prepared_stages
    assert repository.inspect(active.start.receipt.identity) == record
    assert workflow.owner.list_pending_lifecycle_obligations() == obligations
    assert carrier.events == events and not workflow.owner._released
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert active.start.stage.cleaned and operation._prepared_stages == []
    assert operation.active_inline_calls == (active,) and not workflow.owner._released


@pytest.mark.parametrize("committed", [False, True])
def test_reservation_control_after_staging_closes_unused_activation_and_preserves_control(
    staged, monkeypatch, committed
):
    _, repository, operation, access, carrier, workflow, _ = staged
    original = repository.reserve
    control = KeyboardInterrupt("reservation")

    def reserve(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise control

    monkeypatch.setattr(repository, "reserve", reserve)
    with pytest.raises(KeyboardInterrupt) as caught:
        bulk_start(access)
    assert caught.value is control
    (cleanup,) = operation._prepared_stages
    assert cleanup._stage._activation_settled and "activate" not in carrier.events
    assert operation.active_inline_calls == ()
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert cleanup._stage.cleaned and workflow.owner._released


def test_partial_stage_remains_parent_owned_before_actual_start_exists(staged):
    _, _, operation, access, carrier, workflow, _ = staged
    carrier.body_override = b"{}"
    with pytest.raises(StateError):
        bulk_start(access)
    (cleanup,) = operation._prepared_stages
    assert not cleanup._stage._activation_prepared and not cleanup._stage.cleaned
    assert operation.active_inline_calls == () and "activate" not in carrier.events
    carrier.body_override = None
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert cleanup._stage.cleaned and workflow.owner._released


def test_unconfirmed_stage_cleanup_blocks_aggregate_release_then_retries(staged):
    _, _, operation, access, carrier, workflow, _ = staged
    bulk_start(access)
    (cleanup,) = operation._prepared_stages
    carrier.body_override = b"{}"
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert operation._finished and operation._prepared_stages == [cleanup]
    assert not workflow._components_settled() and not workflow.owner._released
    carrier.body_override = None
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert workflow.owner._released and operation._prepared_stages == []


def test_finish_control_survives_secondary_stage_cleanup_control(staged, monkeypatch):
    _, _, operation, access, _, workflow, _ = staged
    bulk_start(access)
    (cleanup,) = operation._prepared_stages
    control = KeyboardInterrupt("finish")
    original_cause = OSError("original")
    control.__cause__ = original_cause

    def finish():
        raise control

    def secondary(deadline):
        raise SystemExit("cleanup")

    monkeypatch.setattr(operation, "finish", finish)
    monkeypatch.setattr(cleanup, "finish", secondary)
    with pytest.raises(KeyboardInterrupt) as caught:
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert caught.value is control and control.__cause__ is original_cause
    assert operation._prepared_stages == [cleanup] and not workflow.owner._released
    monkeypatch.undo()
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert workflow.owner._released


def test_native_closure_after_lost_candidate_is_not_no_carrier_entry(staged, monkeypatch):
    _, repository, operation, access, carrier, workflow, _ = staged
    operation._native_binding = replace(operation._native_binding, _new_helper_delivery=carrier.new_delivery)

    def lose(*args, **kwargs):
        raise MemoryError("candidate publication")

    monkeypatch.setattr(exchange_module, "ManagedStartCandidate", lose)
    with pytest.raises(MemoryError):
        bulk_start(access)
    (active,) = operation.active_inline_calls
    assert active.candidate is None and active.operation.delivery_started
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert active.borrow.closed and active.start.stage.cleaned
    assert operation.active_inline_calls == (active,) and not workflow.owner._released
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert [row.obligation_kind for row in workflow.owner.list_pending_lifecycle_obligations()] == ["managed-start"]
    assert carrier.events.count("activate") == 1


@pytest.mark.parametrize("native", [False, True])
def test_positive_not_sent_cleans_stage_without_launch_or_fabricated_zero(staged, native):
    _, repository, operation, access, carrier, workflow, _ = staged
    carrier.activation_dispatch = Dispatch.NOT_SENT
    carrier.activation_code = None
    carrier.proven = False
    if native:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=carrier.new_delivery)
    with pytest.raises(StateError) as caught:
        bulk_start(access)
    fact = caught.value.__cause__
    assert isinstance(fact, ManagedExecutionControlFact)
    identity = ManagedRunIdentity(fact.reference.run_id)
    assert repository.inspect(identity).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert operation.active_inline_calls == () and workflow.owner._active_borrow is None
    (cleanup,) = operation._prepared_stages
    assert cleanup._stage._activation_settled
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert workflow.owner._released and cleanup._stage.cleaned
    assert carrier.events.count("activate") == 1


@pytest.mark.parametrize("native", [False, True])
def test_actual_activation_local_delivery_must_drain_before_scratch_cleanup(staged, native):
    _, _, operation, access, carrier, workflow, _ = staged
    carrier.activation_unsettled = True
    if native:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=carrier.new_delivery)
    with pytest.raises(StateError):
        bulk_start(access)
    (active,) = operation.active_inline_calls
    assert active.start.local_delivery is carrier.activation_custody
    assert not active.start.local_delivery.settled and not active.start.stage._activation_settled
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert carrier.activation_custody.settled
    assert active.start.stage.cleaned and workflow.owner._released
    assert carrier.events.count("activate") == 1


def test_native_proof_governs_zero_return_and_exact_closure_continuation(staged):
    _, _, operation, access, carrier, workflow, _ = staged
    carrier.proven = False
    operation._native_binding = replace(operation._native_binding, _new_helper_delivery=carrier.new_delivery)
    with pytest.raises(StateError):
        bulk_start(access)
    (active,) = operation.active_inline_calls
    assert active.operation.wraps(carrier) and active.operation.outstanding_attempt is not None
    assert not active.start.stage._activation_settled
    assert active.start.local_delivery is carrier.activation_custody
    expectation = carrier.deliveries[-1].expectation
    assert expectation.guest == operation._bootstrap.guest
    assert expectation.nonce == carrier.activated.nonce
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert carrier.observed == 1 and not active.start.stage.cleaned
    carrier.proven = True
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert workflow.owner._released and active.start.stage.cleaned
    assert carrier.events.count("activate") == 1


def test_unknown_activation_keeps_actual_borrow_and_cleanup_debt(staged):
    _, _, operation, access, carrier, workflow, _ = staged
    carrier.activation_error = KeyboardInterrupt("activation")
    with pytest.raises(KeyboardInterrupt):
        bulk_start(access)
    (active,) = operation.active_inline_calls
    assert active.start.local_delivery is carrier.activation_custody
    assert not active.start.stage._activation_settled and not active.borrow.closed
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert operation._prepared_stages and not active.start.stage.cleaned
    assert carrier.events.count("activate") == 1


@pytest.mark.parametrize("committed", [False, True])
def test_lost_actual_close_reply_keeps_stage_and_retries_bookkeeping_without_replay(staged, monkeypatch, committed):
    _, _, operation, access, carrier, workflow, _ = staged
    original = OperationBorrow.close
    raised = False

    def close(borrow):
        nonlocal raised
        if carrier.events and carrier.events[-1] == "activate" and not raised:
            raised = True
            if committed:
                original(borrow)
            raise KeyboardInterrupt("close reply")
        return original(borrow)

    monkeypatch.setattr(OperationBorrow, "close", close)
    with pytest.raises(KeyboardInterrupt):
        bulk_start(access)
    assert operation._prepared_stages
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert workflow.owner._released and operation._prepared_stages == []
    assert carrier.events.count("activate") == 1
