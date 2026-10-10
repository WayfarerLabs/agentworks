"""Foreground managed composition under existing exact operation custody."""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import access as access_module
from agentworks.execution import carrier as carrier_module
from agentworks.execution._execution_operation import ExecutionOperation, ManagedExecutionControlFact
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_bound_run import ManagedDeadlineExpired
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_protocol import ManagedResultControl
from agentworks.execution._managed_start_protocol import ManagedStartRequest
from agentworks.execution.carrier import Deadline, Dispatch, Retention
from agentworks.execution.diagnostics import ExecutionFailureReason, ExecutionPhase, check_execution_result
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, CheckedExecutionError, ExecutionFailure, ExitCode
from agentworks.operations import OperationAttempt, OperationBorrow, OperationOwner

from . import test_managed_execution_access as managed_tests
from . import test_managed_observation as observation_tests
from .test_managed_execution_access import bound as bound
from .test_managed_execution_access import view as view

pytestmark = pytest.mark.windows


@pytest.fixture(autouse=True)
def framed_stream_reads(view):
    main = view[3]

    def response(request):
        if request.stream is None:
            return main.observe_response(request)
        name = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
        names = (FactName.LAUNCH, name)
        facts = tuple(managed_tests.fact(name, request.expected_launch) for name in names)
        return observation_tests._records(request.nonce, ManagedResultControl(names), facts)

    main.observe.response = response


def _status(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    original = managed_tests.fact

    def fact(name: FactName, launch: bytes) -> bytes:
        value = decode_fact(original(name, launch))
        if name is FactName.WAIT:
            value["exit_code"] = code
        return encode_fact(value)

    monkeypatch.setattr(managed_tests, "fact", fact)


@pytest.mark.parametrize("checked", [False, True])
def test_foreground_managed_zero_is_one_launch_and_wait(view, monkeypatch, checked):
    database, workflow, access, main, keeper = view
    epoch = time.monotonic()
    # This checks composition, not wall-clock performance of host coordination.
    monkeypatch.setattr(carrier_module, "time", SimpleNamespace(monotonic=lambda: epoch))
    _status(monkeypatch, 0)
    result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.discard(), check=checked)
    assert result.ok and result.status == ExitCode(0) and result.job is not None
    assert main.start.calls == 1 and main.observe.calls == 3
    assert result.stdout.retention is result.stderr.retention is Retention.DISCARDED
    assert keeper.stop.calls == keeper.observe.calls == 0
    assert not workflow.views.execution_operation.managed_runs[0].keeper._stop.is_set()
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("entry", ["run", "wait"])
def test_checked_managed_nonzero_retains_exact_contextual_result(view, monkeypatch, entry):
    _, workflow, access, _, _ = view
    checked_results = []

    def check(result, **context):
        checked_results.append(result)
        return check_execution_result(result, **context)

    monkeypatch.setattr(access_module, "check_execution_result", check)
    if entry == "wait":
        job = access.start(
            Command(["/bin/false"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
        )
    with pytest.raises(CheckedExecutionError) as caught:
        if entry == "run":
            access.run(Command(["/bin/false"]), profile=Protection.MANAGED, output=Output.discard(), check=True)
        else:
            access.wait(job, check=True)
    error = caught.value
    assert error.result is checked_results[0] and error.result.job is not None
    assert error.result.status == ExitCode(7)
    assert (error.entity_kind, error.entity_name) == ("vm", "vm-one")
    assert error.details is not None
    assert error.details.phase is ExecutionPhase.APPLICATION
    assert error.details.reason is ExecutionFailureReason.APPLICATION_STATUS
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_foreground_budget_is_selected_once_and_shared(view, monkeypatch):
    _, workflow, access, main, _ = view
    _status(monkeypatch, 0)
    composition, requested = Deadline.after(5), Deadline.after(3)
    policies, delivered = [], []

    def policy():
        policies.append(True)
        return composition

    monkeypatch.setattr(access, "_deadline", policy)
    for carrier in (main.start, main.observe):
        original = carrier.execute

        def execute(*args, _original=original, **kwargs):
            delivered.append(kwargs["deadline"])
            return _original(*args, **kwargs)

        monkeypatch.setattr(carrier, "execute", execute)
    assert access.run(
        Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.discard(), deadline=requested
    ).ok
    assert len(policies) == 1 and len(delivered) == 4 and all(value is requested for value in delivered)
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("policy", ["capture", "discard", "sensitive-input", "sensitive-request"])
@pytest.mark.parametrize("checked", [False, True])
def test_acknowledgement_consumes_budget_without_losing_job(view, monkeypatch, policy, checked):
    database, workflow, access, main, keeper = view
    deadline = Deadline.after(5)
    monkeypatch.setattr(access, "_deadline", lambda: deadline)
    original = ExecutionOperation.start_managed

    def start(self, *args, **kwargs):
        job = original(self, *args, **kwargs)
        object.__setattr__(deadline, "expires_at", 0.0)
        return job

    monkeypatch.setattr(ExecutionOperation, "start_managed", start)
    options = {
        "output": Output.discard() if policy == "discard" else Output.capture(),
        "stdin": Input.sensitive(b"private-input") if policy == "sensitive-input" else Input.eof(),
        "sensitive": policy == "sensitive-request",
    }
    if checked:
        with pytest.raises(CheckedExecutionError) as caught:
            access.run(Command(["/bin/true"]), profile=Protection.MANAGED, check=True, **options)
        result = caught.value.result
        assert caught.value.details is not None
        assert caught.value.details.phase is ExecutionPhase.OBSERVATION
        assert caught.value.details.reason is ExecutionFailureReason.DEADLINE
    else:
        result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, **options)
    assert result.job is not None and result.failure is ExecutionFailure.DEADLINE and result.deadline_exceeded
    assert result.dispatch is Dispatch.UNKNOWN and result.application_state is ApplicationState.UNKNOWN
    assert not result.owned_cleanup_confirmed and not result.stdout.complete and not result.stderr.complete
    retention = (
        Retention.SUPPRESSED
        if policy.startswith("sensitive")
        else (Retention.DISCARDED if policy == "discard" else Retention.CAPTURED)
    )
    assert result.stdout.retention is result.stderr.retention is retention
    assert main.start.calls == 1 and main.observe.calls == 0 and keeper.stop.calls == 0
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.acknowledged and not run.keeper._stop.is_set()
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("reference", [JobRef("f" * 32), "invalid"])
def test_pre_first_wait_expiry_never_adopts_foreign_reference(view, reference):
    _, workflow, _, main, _ = view
    with pytest.raises(ValidationError):
        workflow.views.execution_operation.wait_job(reference, main, Deadline.after(0))
    assert main.start.calls == main.observe.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize(
    "options",
    [
        {"check": 1},
        {"sudo": 1},
        {"sensitive": 1},
        {"stdin": b"raw"},
        {"output": "raw"},
        {"env": {"bad-name": "private-value"}},
        {"cwd": "relative"},
        {"lifetime": Lifetime.INDEPENDENT},
        {"deadline": Deadline.after(0)},
        {"deadline": "invalid"},
        {"profile": "managed"},
    ],
)
def test_foreground_flags_refuse_before_managed_effects(view, options):
    _, workflow, access, main, keeper = view
    values = {"profile": Protection.MANAGED, **options}
    with pytest.raises((ValidationError, StateError)):
        access.run(Command(["/bin/true"]), **values)
    assert not workflow.views.execution_operation.managed_runs
    assert main.start.calls == main.observe.calls == keeper.clock.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("stage", ["launch", "wait"])
def test_foreground_controls_preserve_identity_reference_and_cause(view, monkeypatch, stage):
    database, workflow, access, main, _ = view
    control = KeyboardInterrupt("controlled interruption")
    carrier = main.start if stage == "launch" else main.observe

    def interrupt(*args, **kwargs):
        raise control

    monkeypatch.setattr(carrier, "execute", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    assert caught.value is control and isinstance(control.__cause__, ManagedExecutionControlFact)
    (run,) = workflow.views.execution_operation.managed_runs
    assert control.__cause__.reference.run_id == run.receipt.identity.run_id
    assert control.__cause__.__cause__ is not None
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


def test_foreground_partial_output_retains_bytes_and_safe_checked_error(view, monkeypatch):
    _, workflow, access, main, _ = view
    _status(monkeypatch, 0)
    payload = b"\x00\xffprefix"
    original = managed_tests.fact

    def fact(name: FactName, launch: bytes) -> bytes:
        value = decode_fact(original(name, launch))
        if name is FactName.STDOUT_END:
            value.update(
                retained_bytes=len(payload),
                retained_sha256=hashlib.sha256(payload).hexdigest(),
                disposition="truncated-capture",
            )
        elif name is FactName.STDERR_END:
            value["disposition"] = "complete-capture"
        return encode_fact(value)

    monkeypatch.setattr(managed_tests, "fact", fact)

    def response(request):
        if request.stream is None:
            return main.observe_response(request)
        name = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
        names = (FactName.LAUNCH, name)
        facts = tuple(fact(name, request.expected_launch) for name in names)
        return observation_tests._records(
            request.nonce, ManagedResultControl(names), facts, payload if name is FactName.STDOUT_END else b""
        )

    main.observe.response = response
    with pytest.raises(CheckedExecutionError) as caught:
        access.run(
            Script("private-source", Shell.SH),
            profile=Protection.MANAGED,
            env={"PRIVATE": "private-value"},
            output=Output.capture(100),
            check=True,
        )
    result = caught.value.result
    assert result.job is not None and result.failure is ExecutionFailure.OUTPUT_LIMIT
    assert result.stdout.data == payload and not result.stdout.complete
    assert "private-source" not in str(caught.value) and "private-value" not in str(caught.value)
    assert repr(payload) not in repr(result)
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_custody_bearing_deadline_control_is_not_reduced_to_timeout(view, monkeypatch):
    _, workflow, access, main, _ = view
    job = access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    control = ManagedDeadlineExpired("controlled custody expiry")
    cause = StateError("retained custody")

    def wait(*args, **kwargs):
        raise control from cause

    from agentworks.execution import _execution_operation as operation_module

    monkeypatch.setattr(operation_module, "wait_bound_managed_result", wait)
    with pytest.raises(ManagedDeadlineExpired) as caught:
        access.wait(job)
    assert caught.value is control and control.__cause__ is not None and control.__cause__.__cause__ is cause
    assert main.observe.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("checked", [False, True])
def test_observation_timeout_keeps_keeper_and_owner_live(view, monkeypatch, checked):
    database, workflow, access, main, keeper = view
    deadline = Deadline.after(5)
    main.facts = (FactName.LAUNCH,)
    monkeypatch.setattr(access, "_deadline", lambda: deadline)

    monkeypatch.setattr(time, "sleep", lambda _: object.__setattr__(deadline, "expires_at", 0.0))
    if checked:
        with pytest.raises(CheckedExecutionError) as caught:
            access.run(Command(["/bin/true"]), profile=Protection.MANAGED, check=True)
        result = caught.value.result
        assert caught.value.details is not None and caught.value.details.phase is ExecutionPhase.OBSERVATION
    else:
        result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    assert result.job is not None and result.failure is ExecutionFailure.DEADLINE
    assert main.start.calls == main.observe.calls == 1 and keeper.stop.calls == 0
    (run,) = workflow.views.execution_operation.managed_runs
    assert not run.keeper._stop.is_set() and not run.cleanup_complete
    assert run.keeper._worker is not None and run.keeper._worker.is_alive()
    assert run.keeper.obligation is not None
    rows = database.operations.list_pending_lifecycle_obligations(workflow.owner.ownership)
    assert any(
        row.obligation_id == run.keeper.obligation.obligation_id
        and row.state is LifecycleObligationState.POSSIBLE_EFFECT
        for row in rows
    )
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("attempt_started", [False, True])
def test_foreground_overlap_retains_reservation_but_never_launches(view, monkeypatch, attempt_started):
    database, workflow, access, main, keeper = view
    held: list[OperationBorrow] = []
    attempts: list[OperationAttempt] = []
    original = main.start.validate

    def validate(*args, **kwargs):
        original(*args, **kwargs)
        if not held:
            held.append(workflow.owner.borrow())
            if attempt_started:
                attempts.append(held[0].begin_attempt())

    monkeypatch.setattr(main.start, "validate", validate)
    with pytest.raises(StateError) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.reserved is not None and run.keeper.obligation is not None
    assert isinstance(caught.value.__cause__, ManagedExecutionControlFact)
    assert caught.value.__cause__.reference.run_id == run.receipt.identity.run_id
    assert main.start.calls == main.observe.calls == 0 and keeper.clock.calls == 1
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None
    for attempt in attempts:
        attempt.settle()
    held[0].close()
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert run.cleanup_complete and keeper.stop.calls == 0


def test_entry_overlap_retains_conservative_admission_uncertainty(view):
    database, workflow, access, main, keeper = view
    borrow = workflow.owner.borrow()
    with pytest.raises(StateError):
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.reserved is not None and run.keeper.admission_uncertain and run.keeper.obligation is None
    assert main.start.calls == main.observe.calls == keeper.clock.calls == 0
    borrow.close()
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


def test_stale_generation_cannot_launch_foreground_work(view):
    database, workflow, access, main, keeper = view
    OperationOwner.recover(database.operations, workflow.owner.ownership, "f" * 32)
    with pytest.raises(StateError):
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    assert main.start.calls == main.observe.calls == keeper.clock.calls == 0


def test_unknown_wait_helper_keeps_reference_and_blocks_later_observation(view):
    database, workflow, access, main, keeper = view
    main.observe.code = None
    result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    assert result.job is not None and result.failure is ExecutionFailure.OBSERVATION
    assert result.application_state is ApplicationState.UNKNOWN and not result.owned_cleanup_confirmed
    assert main.start.calls == main.observe.calls == 1 and keeper.stop.calls == 0
    with pytest.raises(StateError):
        access.wait(result.job)
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert main.start.calls == main.observe.calls == 1
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


def test_pre_first_wait_expiry_cannot_promote_unknown_original_start(view):
    database, workflow, access, main, _ = view
    main.start.code = None
    with pytest.raises(StateError) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    assert isinstance(caught.value.__cause__, ManagedExecutionControlFact)
    with pytest.raises(ValidationError):
        workflow.views.execution_operation.wait_job(caught.value.__cause__.reference, main, Deadline.after(0))
    assert main.start.calls == 1 and main.observe.calls == 0
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


@pytest.mark.parametrize("sudo", [False, True])
@pytest.mark.parametrize("sensitive", [False, True])
def test_foreground_preserves_body_identity_and_sensitive_bytes(view, monkeypatch, sudo, sensitive):
    _, workflow, access, main, _ = view
    ordinary = IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT)
    monkeypatch.setattr(access, "_ordinary_plan", ordinary)
    captured: list[ManagedStartRequest] = []
    original = main.start.response

    def response(request):
        captured.append(request)
        return original(request)

    main.start.response = response
    payload = b"\x00\xffprivate-input"
    source = "printf private-source"
    result = access.run(
        Script(source, Shell.SH),
        profile=Protection.MANAGED,
        sudo=sudo,
        stdin=Input.sensitive(payload) if sensitive else Input.bytes(payload),
        output=Output.discard(),
        env={"PRIVATE": "private-value"},
        cwd="/approved",
    )
    assert result.job is not None and len(captured) == 1
    body = captured[0].job
    assert body.source == source.encode() and body.stdin == payload
    assert body.environment == (("PRIVATE", "private-value"),) and body.cwd == "/approved"
    assert body.output_mode == ("sensitivity-suppressed" if sensitive else "discard")
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.receipt.spec.workload == (access._elevated_plan.expected if sudo else ordinary.expected)
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_plain_expired_wait_refuses_without_delivery(view):
    _, workflow, access, main, _ = view
    job = access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    with pytest.raises(ValidationError):
        access.wait(job, deadline=Deadline.after(0))
    assert main.observe.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_unbounded_selected_managed_budget_refuses_before_launch(view, monkeypatch):
    _, workflow, access, main, _ = view
    monkeypatch.setattr(access, "_deadline", lambda: Deadline.after(None))
    with pytest.raises(ValidationError):
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED)
    assert main.start.calls == main.observe.calls == 0 and not workflow.views.execution_operation.managed_runs
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_foreground_closed_resources_without_wait_remain_unknown_application(view):
    _, workflow, access, main, _ = view
    main.facts = tuple(name for name in main.facts if name is not FactName.WAIT)
    with pytest.raises(CheckedExecutionError) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.discard(), check=True)
    result = caught.value.result
    assert result.job is not None and result.application_state is ApplicationState.UNKNOWN and result.status is None
    assert result.failure is ExecutionFailure.OBSERVATION and result.owned_cleanup_confirmed
    assert caught.value.details is not None and caught.value.details.phase is ExecutionPhase.OBSERVATION
    assert main.start.calls == 1 and main.observe.calls == 3
    workflow.close(cleanup_deadline=Deadline.after(5))
