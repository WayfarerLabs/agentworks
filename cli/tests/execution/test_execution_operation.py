"""Custody behavior for the private foreground inline execution operation."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as execution_operation
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import InlineCandidateResult, prepare_inline_candidate
from agentworks.execution._inline_control import StreamRetention
from agentworks.execution._inline_observer import InlineObservation, StreamObservation
from agentworks.execution._managed_runs import ManagedRunIdentity, ManagedRunRepository
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    PreparedInvocation,
)
from agentworks.execution.models import Command, Input, JobRef, Output
from agentworks.operations import OperationOwner
from tests.execution.files._target_support import target_for_owner
from tests.execution.test_numeric_inline_bootstrap_adoption import _CONTEXT


@dataclass
class RecordingCarrier:
    report: CarrierReport | None = None
    control: BaseException | None = None
    before_dispatch: Callable[[], None] | None = None
    calls: int = 0
    validations: int = 0
    deadlines: list[Deadline] | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.validate(invocation, io=io)
        del invocation, io
        self.calls += 1
        if self.deadlines is not None:
            self.deadlines.append(deadline)
        if self.before_dispatch is not None:
            self.before_dispatch()
        if self.control is not None:
            raise self.control
        assert self.report is not None
        return self.report


def test_borrowed_validation_does_not_begin_an_operation_attempt(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
) -> None:
    _, owner, _ = operation
    borrow = owner.borrow()
    underlying = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    carrier = BorrowedFixedHelperCarrier(underlying, borrow)
    try:
        carrier.validate(PreparedInvocation(("/bin/true",)), io=CarrierIO())
        assert underlying.validations == 1
        assert underlying.calls == 0
        assert not carrier.has_outstanding_attempt
    finally:
        borrow.close()


@pytest.fixture
def operation(
    tmp_path: Path,
) -> Generator[tuple[Database, OperationOwner, execution_operation.ExecutionOperation]]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "inline-operation-vm"),
        "inline-operation",
    )
    try:
        yield database, owner, execution_operation.ExecutionOperation(owner, target_for_owner(owner))
    finally:
        database.close()


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1, 1, (1,)), IdentityMode.DIRECT)


@pytest.fixture
def runtime_selection() -> RuntimeSelection:
    return RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")


@pytest.mark.parametrize("native_binding", [False, True])
@pytest.mark.parametrize(
    "action",
    [
        "binding",
        "access",
        "run",
        "observe",
        "read",
        "wait",
        "stop",
        "dispose",
        "relay",
        "output_policy",
        "terminal",
        "managed_read",
        "start",
        "resource_start",
    ],
)
def test_host_without_managed_target_refuses_job_admission_before_effects(
    tmp_path, monkeypatch, plan, runtime_selection, action, native_binding
) -> None:
    database = Database(tmp_path / "host.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.PLATFORM_HOST, "host-jobs"), "host-job-refusal"
    )
    carrier = RecordingCarrier()

    def refuse(*args, **kwargs):
        pytest.fail("unavailable managed target crossed local admission")

    repository = ManagedRunRepository(database)
    binding = NativeExecutionBinding(carrier, "delivery", runtime_selection, _new_managed_delivery=refuse)
    owned = execution_operation.ExecutionOperation(
        owner,
        None,
        managed_repository=repository,
        native_binding=binding if native_binding else None,
        route_check=refuse,
    )
    monkeypatch.setattr(owner, "borrow", refuse)
    monkeypatch.setattr(owner, "list_pending_lifecycle_obligations", refuse)
    monkeypatch.setattr(repository, "reserve", refuse)
    monkeypatch.setattr(repository, "inspect", refuse)
    monkeypatch.setattr(execution_operation, "prepare_managed_read", refuse)
    monkeypatch.setattr(execution_operation, "prepare_managed_live", refuse)
    monkeypatch.setattr(execution_operation, "compose_managed_body", refuse)
    reference = JobRef("a" * 32)
    deadline = Deadline.after(30)
    context = dict(
        repository=repository,
        owner=owner,
        target=target_for_owner(owner),
        guest=_CONTEXT.guest,
        root_plan=_CONTEXT.root_entry,
        runtime_selection=runtime_selection,
    )
    start = dict(
        plan=plan,
        runtime_selection=runtime_selection,
        deadline=deadline,
        input=Input.eof(),
        output=Output.capture(),
        env=None,
        cwd=None,
        sensitive=False,
    )
    calls: dict[str, Callable[[], object]] = {
        "binding": lambda: owned.require_job_binding(carrier, runtime_selection),
        "access": lambda: owned.require_managed_access(ManagedRunIdentity(reference.run_id), **context),
        "run": lambda: owned.require_managed_run(ManagedRunIdentity(reference.run_id), **context),
        "observe": lambda: owned.observe_job(reference, carrier, deadline),
        "read": lambda: owned.read_job(reference, carrier, deadline, "stdout"),
        "wait": lambda: owned.wait_job(reference, carrier, deadline),
        "stop": lambda: owned.stop_job(reference, deadline),
        "dispose": lambda: owned.dispose_job(reference, carrier, deadline),
        "relay": lambda: owned.relay_managed(reference, carrier, deadline=deadline),
        "output_policy": lambda: owned.unobserved_managed_output(reference),
        "terminal": lambda: owned.retain_job_terminal_observation(reference, None),
        "managed_read": lambda: owned.observe_managed(
            carrier,
            expected_launch=b"",
            root_plan=_CONTEXT.root_entry,
            runtime_selection=runtime_selection,
            deadline=deadline,
            guest=_CONTEXT.guest,
            stream=None,
        ),
        "start": lambda: owned.start_managed(carrier, Command(("/bin/true",)), **start),
        "resource_start": lambda: owned.start_resource_managed(carrier, Command(("/bin/true",)), **start),
    }
    try:
        with pytest.raises(StateError):
            calls[action]()
        assert carrier.calls == 0 and carrier.validations == 0
        assert owned.managed_runs == () and owned.active_inline_calls == ()
        assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        owned.finish()
        owner.close()
    finally:
        database.close()


def _candidate(report: CarrierReport, *, payload: bytes = b"") -> InlineCandidateResult:
    observation = None
    if payload:
        stream = StreamObservation(
            payload,
            len(payload),
            "0" * 64,
            True,
            False,
            StreamRetention.CAPTURED,
        )
        observation = InlineObservation(True, stream, stream, None, None, True, None)
    return InlineCandidateResult(
        report.dispatch,
        report.completion,
        report.local_status,
        report.failure,
        RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
        observation,
    )


def _patch_candidate_execution(monkeypatch: pytest.MonkeyPatch, *, payload: bytes = b"") -> list[Deadline]:
    deadlines: list[Deadline] = []

    def execute(
        carrier: BorrowedFixedHelperCarrier,
        prepared: object,
        *,
        deadline: Deadline,
    ) -> InlineCandidateResult:
        del prepared
        deadlines.append(deadline)
        report = carrier.execute(PreparedInvocation(("/bin/true",)), io=CarrierIO(), deadline=deadline)
        return _candidate(report, payload=payload)

    monkeypatch.setattr(execution_operation, "execute_inline_candidate", execute)
    return deadlines


def _run(
    operation: execution_operation.ExecutionOperation,
    carrier: RecordingCarrier,
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
) -> execution_operation.OwnedInlineOutcome:
    return operation.run_inline(
        carrier,
        Command(("/bin/true",)),
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
    )


def test_preparation_expiring_deadline_refuses_before_borrow(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, owner, owned = operation
    prepared = False

    def prepare(*args: object, **kwargs: object) -> object:
        nonlocal prepared
        prepared = True
        return prepare_inline_candidate(*args, **kwargs)

    def refuse_borrow(self: OperationOwner) -> None:
        del self
        pytest.fail("expired preparation borrowed operation ownership")

    monkeypatch.setattr(execution_operation, "prepare_inline_candidate", prepare)
    monkeypatch.setattr(time, "monotonic", lambda: 2.0 if prepared else 0.0)
    monkeypatch.setattr(OperationOwner, "borrow", refuse_borrow)
    carrier = RecordingCarrier()

    with pytest.raises(ValidationError):
        _run(owned, carrier, plan, runtime_selection, Deadline(1.0))

    assert prepared
    assert carrier.calls == 0
    assert owned.active_inline_calls == ()
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    owner.close()


@pytest.mark.parametrize("control_type", [StateError, RuntimeError, KeyboardInterrupt])
def test_route_refusal_releases_preparation_before_borrow(
    operation, plan, runtime_selection, monkeypatch, control_type
) -> None:
    database, owner, owned = operation
    control = control_type("synthetic route refusal")
    prepared = []
    deadline = Deadline.after(30)

    def prepare(*args, **kwargs):
        candidate = prepare_inline_candidate(*args, **kwargs)
        prepared.append(candidate)
        return candidate

    def route_check(selected):
        assert selected is deadline
        raise control

    def deny_borrow():
        pytest.fail("route refusal borrowed helper custody")

    monkeypatch.setattr(execution_operation, "prepare_inline_candidate", prepare)
    monkeypatch.setattr(owned, "_route_check", route_check)
    monkeypatch.setattr(owner, "borrow", deny_borrow)
    carrier = RecordingCarrier()
    with pytest.raises(control_type) as caught:
        _run(owned, carrier, plan, runtime_selection, deadline)
    assert caught.value is control and control.__cause__ is None
    assert len(prepared) == 1 and prepared[0].io == CarrierIO()
    assert carrier.calls == 0 and owned.active_inline_calls == ()
    assert not owner.list_pending_lifecycle_obligations()
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.RESERVED
    owned.finish()
    owner.close()


@pytest.mark.parametrize("close_during_check", [False, True])
def test_route_admission_checks_closure_before_provider_and_again_before_borrow(
    operation, plan, runtime_selection, monkeypatch, close_during_check
) -> None:
    _, owner, owned = operation
    checked = []

    def route_check(deadline):
        checked.append(deadline)
        owned.finish()

    def deny_borrow():
        pytest.fail("closing execution borrowed helper custody")

    monkeypatch.setattr(owned, "_route_check", route_check)
    monkeypatch.setattr(owner, "borrow", deny_borrow)
    if not close_during_check:
        owned.finish()
    carrier = RecordingCarrier()
    deadline = Deadline.after(30)
    with pytest.raises(StateError):
        _run(owned, carrier, plan, runtime_selection, deadline)
    assert checked == ([deadline] if close_during_check else [])
    assert carrier.calls == 0 and owned.active_inline_calls == ()
    owner.close()


def test_route_check_consuming_budget_refuses_before_borrow(operation, plan, runtime_selection, monkeypatch) -> None:
    _, owner, owned = operation
    checked = []
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    deadline = Deadline(1.0)

    def route_check(selected):
        checked.append(selected)
        monkeypatch.setattr(time, "monotonic", lambda: 2.0)

    def deny_borrow():
        pytest.fail("expired route admission borrowed helper custody")

    monkeypatch.setattr(owned, "_route_check", route_check)
    monkeypatch.setattr(owner, "borrow", deny_borrow)
    carrier = RecordingCarrier()
    with pytest.raises(ValidationError):
        _run(owned, carrier, plan, runtime_selection, deadline)
    assert checked == [deadline] and carrier.calls == 0 and owned.active_inline_calls == ()
    owned.finish()
    owner.close()


def test_arms_durable_attempt_before_single_carrier_dispatch(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, owner, owned = operation
    candidate_deadlines = _patch_candidate_execution(monkeypatch)
    observed_states: list[OperationClaimState] = []

    def observe_admission() -> None:
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None
        observed_states.append(claim.state)

    deadline = Deadline.after(30)
    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0)), before_dispatch=observe_admission)
    outcome = _run(owned, carrier, plan, runtime_selection, deadline)

    assert observed_states == [OperationClaimState.POSSIBLE_DISPATCH]
    assert carrier.calls == 1
    assert candidate_deadlines == [deadline]
    assert carrier.deadlines is None
    assert outcome.candidate is not None and outcome.candidate.dispatch is Dispatch.SENT
    assert not outcome.requires_owner_retention
    owned.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_captures_candidate_before_settlement(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, owned = operation
    _patch_candidate_execution(monkeypatch)
    settled_after_capture: list[bool] = []
    original = BorrowedFixedHelperCarrier.settle

    def settle(self: BorrowedFixedHelperCarrier, dispatch: Dispatch, completion: ExitStatus | None) -> bool:
        active = owned.active_inline_calls
        settled_after_capture.append(len(active) == 1 and active[0].candidate is not None)
        return original(self, dispatch, completion)

    monkeypatch.setattr(BorrowedFixedHelperCarrier, "settle", settle)
    outcome = _run(
        owned,
        RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0))),
        plan,
        runtime_selection,
        Deadline.after(30),
    )

    assert settled_after_capture == [True]
    assert outcome.candidate is not None
    assert not outcome.requires_owner_retention
    owned.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("report", "retained"),
    [
        (CarrierReport(Dispatch.NOT_SENT), False),
        (CarrierReport(Dispatch.SENT, ExitStatus(code=0)), False),
        (CarrierReport(Dispatch.SENT), True),
        (CarrierReport(Dispatch.SENT, ExitStatus(code=1)), True),
        (CarrierReport(Dispatch.UNKNOWN), True),
    ],
)
def test_preserves_dispatch_and_refuses_release_without_supported_termination(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
    report: CarrierReport,
    retained: bool,
) -> None:
    _, owner, owned = operation
    _patch_candidate_execution(monkeypatch)
    outcome = _run(owned, RecordingCarrier(report), plan, runtime_selection, Deadline.after(30))

    assert outcome.candidate is not None and outcome.candidate.dispatch is report.dispatch
    assert outcome.requires_owner_retention is retained
    assert bool(owned.unfinished_inline_executions) is retained
    if retained:
        with pytest.raises(StateError):
            owner.borrow()
    else:
        owned.finish()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


def test_carrier_failure_and_expired_deadline_remain_separate_facts(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, owned = operation
    _patch_candidate_execution(monkeypatch)
    expired = False

    def clock() -> float:
        return 2.0 if expired else 0.0

    def expire_at_dispatch() -> None:
        nonlocal expired
        expired = True

    monkeypatch.setattr(time, "monotonic", clock)
    deadline = Deadline(1.0)
    report = CarrierReport(Dispatch.NOT_SENT, failure=Failure.DEADLINE)
    outcome = _run(
        owned, RecordingCarrier(report, before_dispatch=expire_at_dispatch), plan, runtime_selection, deadline
    )

    assert outcome.candidate is not None
    assert outcome.candidate.dispatch is Dispatch.NOT_SENT
    assert outcome.candidate.carrier_failure is Failure.DEADLINE
    assert outcome.deadline_exceeded
    assert not outcome.requires_owner_retention
    owned.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_carrier_failure_is_retained_as_a_candidate_fact(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, owned = operation
    _patch_candidate_execution(monkeypatch)
    report = CarrierReport(Dispatch.SENT, ExitStatus(code=0), failure=Failure.OUTPUT)
    outcome = _run(owned, RecordingCarrier(report), plan, runtime_selection, Deadline.after(30))

    assert outcome.candidate is not None
    assert outcome.candidate.carrier_failure is Failure.OUTPUT
    assert not outcome.deadline_exceeded
    assert not outcome.requires_owner_retention
    owned.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_unfinished_archive_omits_captured_inline_bytes(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, owned = operation
    payload = b"captured-inline-bytes"
    _patch_candidate_execution(monkeypatch, payload=payload)

    outcome = _run(owned, RecordingCarrier(CarrierReport(Dispatch.SENT)), plan, runtime_selection, Deadline.after(30))

    assert outcome.candidate is not None
    observation = outcome.candidate.observation
    assert observation is not None and observation.stdout is not None
    assert observation.stdout.data == payload
    (unfinished,) = owned.unfinished_inline_executions
    assert unfinished.outcome.candidate is None
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("control", [KeyboardInterrupt("control-canary"), RuntimeError("provider-canary")])
def test_escaping_control_preserves_identity_with_safe_custody(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
) -> None:
    _, owner, owned = operation
    _patch_candidate_execution(monkeypatch)
    carrier = RecordingCarrier(control=control)

    with pytest.raises(type(control)) as raised:
        _run(owned, carrier, plan, runtime_selection, Deadline.after(30))

    assert raised.value is control
    fact = raised.value.__cause__
    assert isinstance(fact, execution_operation.InlineExecutionControlFact)
    assert fact.outcome.candidate is None
    assert fact.outcome.pending_remote_effects
    assert fact.outcome.requires_owner_retention
    assert "provider-canary" not in repr(fact)
    assert "control-canary" not in repr(fact)
    with pytest.raises(StateError):
        owner.borrow()


def test_reuses_the_identical_deadline_for_preparation_and_dispatch(
    operation: tuple[Database, OperationOwner, execution_operation.ExecutionOperation],
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, owned = operation
    candidate_deadlines = _patch_candidate_execution(monkeypatch)
    carrier_deadlines: list[Deadline] = []
    deadline = Deadline.after(30)
    carrier = RecordingCarrier(
        CarrierReport(Dispatch.SENT, ExitStatus(code=0)),
        deadlines=carrier_deadlines,
    )

    outcome = _run(owned, carrier, plan, runtime_selection, deadline)

    assert candidate_deadlines == [deadline]
    assert carrier_deadlines == [deadline]
    assert candidate_deadlines[0] is deadline is carrier_deadlines[0]
    assert not outcome.requires_owner_retention
    owned.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_outcome_representation_does_not_expose_inline_payload() -> None:
    payload = b"script-canary stdin-canary environment-canary provider-canary"
    report = CarrierReport(Dispatch.SENT, ExitStatus(code=0))
    outcome = execution_operation.OwnedInlineOutcome(candidate=_candidate(report, payload=payload))

    assert payload.decode() not in repr(outcome)
    assert payload.decode() not in str(outcome)


@pytest.mark.windows
def test_execution_operation_imports_in_a_fresh_process_without_legacy_modules() -> None:
    script = r"""
import importlib.abc
import sys

retired = ("agentworks.transports", "agentworks.ssh", "agentworks.remote_exec")
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in retired):
            raise ImportError(fullname)

sys.meta_path.insert(0, BlockRetired())
from agentworks.execution._execution_operation import ExecutionOperation, OwnedInlineOutcome
assert ExecutionOperation and OwnedInlineOutcome
assert not any(loaded == name or loaded.startswith(name + ".") for loaded in sys.modules for name in retired)
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
