"""Finite DIRECT capture composes one application with immutable snapshot transport."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._capture_control import CaptureCreation
from agentworks.execution._capture_obligation import CAPTURE_OBLIGATION_KIND
from agentworks.execution._execution_operation import ExecutionOperation, InlineExecutionControlFact, OwnedInlineOutcome
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import Deadline
from agentworks.execution.models import Script, Shell
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExitCode
from agentworks.operations import OperationOwner

from ._capture_support import PLAN, CaptureCarrier
from .test_direct_input_operation import ClosureDelivery
from .test_prepared_helper_stage import BOOTSTRAP, RUNTIME, TARGET

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux finite VM capture")
LIMIT = 1_048_576 + 256
STDOUT = bytes(range(256)) * 4097
STDERR = bytes(reversed(range(256))) * 4097
SOURCE = (
    "#" + "x" * 40_000 + "\n/usr/bin/python3 -c 'import os,sys; d=sys.stdin.buffer.read(); assert len(d)==40000; "
    "os.write(1,bytes(range(256))*4097); os.write(2,bytes(reversed(range(256)))*4097); sys.exit(23)'\n"
)


@pytest.fixture
def owned(tmp_path):
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, TARGET.name), "capture"
    )
    operation = ExecutionOperation(owner, TARGET, bootstrap=BOOTSTRAP)
    try:
        yield owner, operation, CaptureCarrier(owner, tmp_path)
    finally:
        database.close()


def run(
    operation: ExecutionOperation,
    carrier: CaptureCarrier,
    *,
    limit: int | None = LIMIT,
    deadline: Deadline | None = None,
    sensitive: bool = False,
) -> OwnedInlineOutcome:
    return operation.run_inline(
        carrier,
        Script(SOURCE, Shell.SH),
        plan=PLAN,
        runtime_selection=RUNTIME,
        stdin=b"a" * 40_000,
        deadline=Deadline.after(45) if deadline is None else deadline,
        capture_limit=limit,
        sensitive=sensitive,
    )


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_independent_binary_streams_with_prepared_input_and_exact_carriers(owned, interpreter):
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    owner, operation, carrier = owned
    carrier.interpreter = interpreter
    deadline = Deadline.after(45)
    outcome = run(operation, carrier, deadline=deadline)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.application_state is ApplicationState.COMPLETED
    assert result.stdout.data == STDOUT and result.stderr.data == STDERR
    assert result.stdout.complete and result.stderr.complete and result.owned_cleanup_confirmed
    assert result.failure is None and not outcome.requires_owner_retention
    assert carrier.activation_calls == 1 and carrier.snapshot_requests
    assert all(value is deadline for value in carrier.deadlines)
    assert max(carrier.executed_sizes) <= 65_536
    assert not operation._captures and not operation._prepared_stages and owner._active_borrow is None
    assert not list(carrier.scratch.iterdir())


def test_truncation_drains_both_pipes_before_wait_and_keeps_exact_prefix(owned):
    _, operation, carrier = owned
    outcome = run(operation, carrier, limit=1_048_576)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.failure is ExecutionFailure.OUTPUT_LIMIT
    assert result.stdout.data == STDOUT[:1_048_576] and result.stderr.data == STDERR[:1_048_576]
    assert not result.stdout.complete and not result.stderr.complete and result.owned_cleanup_confirmed
    assert carrier.activation_calls == 1


@pytest.mark.parametrize("streams", [(0,), (0, 1)])
def test_publication_failure_after_known_exit_never_fabricates_ready_or_bytes(owned, streams):
    _, operation, carrier = owned
    carrier.fail_streams = streams
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.failure is ExecutionFailure.OUTPUT
    assert result.stdout.data == b"" and not result.stdout.complete
    assert result.stderr.data == (b"" if 1 in streams else STDERR)
    assert outcome.requires_owner_retention and not result.owned_cleanup_confirmed
    assert outcome.candidate is not None and outcome.candidate.observation is not None
    facts = outcome.candidate.observation.capture_facts
    assert all(facts[index].ready is None and facts[index].creation is CaptureCreation.POSSIBLE for index in streams)
    assert carrier.activation_calls == 1


def test_unentered_publication_is_positive_no_creation_and_cleans_without_reconcile(owned):
    _, operation, carrier = owned
    carrier.root_refusal = True
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.failure is ExecutionFailure.OUTPUT
    assert not result.stdout.complete and not result.stderr.complete
    assert result.owned_cleanup_confirmed and not outcome.requires_owner_retention
    assert carrier.snapshot_requests == [] and not list(carrier.scratch.iterdir())
    assert outcome.candidate is not None and outcome.candidate.observation is not None
    assert all(fact.creation is CaptureCreation.NOT_ENTERED for fact in outcome.candidate.observation.capture_facts)


def test_ten_thousand_input_capture_does_not_force_prepared_input(owned):
    _, operation, carrier = owned
    source = "/usr/bin/python3 -c 'import sys; assert len(sys.stdin.buffer.read())==10000; print(42)'"
    outcome = operation.run_inline(
        carrier,
        Script(source, Shell.SH),
        plan=PLAN,
        runtime_selection=RUNTIME,
        stdin=b"a" * 10_000,
        deadline=Deadline.after(30),
        capture_limit=LIMIT,
    )
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(0) and result.stdout.data == b"42\n" and result.stderr.data == b""
    assert result.stdout.complete and result.stderr.complete and result.owned_cleanup_confirmed
    assert carrier.requests == [] and carrier.activation_calls == 1 and max(carrier.executed_sizes) <= 65_536


def test_partial_range_failure_preserves_only_verified_prefix_and_status(owned):
    _, operation, carrier = owned
    carrier.fail_range = 262_144
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.failure is ExecutionFailure.OUTPUT
    assert result.stdout.data == STDOUT[:262_144] and result.stderr.data == STDERR[:262_144]
    assert not result.stdout.complete and not result.stderr.complete and result.owned_cleanup_confirmed
    assert carrier.activation_calls == 1


def test_cleanup_failure_keeps_known_status_output_and_fresh_retry_never_replays(owned):
    owner, operation, carrier = owned
    carrier.fail_cleanup = True
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.stdout.data == STDOUT and result.stderr.data == STDERR
    assert result.failure is ExecutionFailure.CLEANUP and not result.owned_cleanup_confirmed
    assert not outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    assert CAPTURE_OBLIGATION_KIND in [row.obligation_kind for row in owner.list_pending_lifecycle_obligations()]
    carrier.fail_cleanup = False
    fresh = Deadline.after(30)
    operation.finish_scratch_cleanup(fresh)
    assert not operation._captures and not list(carrier.scratch.iterdir())
    assert carrier.activation_calls == 1 and carrier.deadlines[-1] is fresh


def test_unknown_actual_helper_completion_blocks_all_retrieval_and_cleanup(owned):
    _, operation, carrier = owned
    carrier.helper_status = None
    outcome = run(operation, carrier)
    assert outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    assert carrier.snapshot_requests == [] and carrier.activation_calls == 1
    assert len(list(carrier.scratch.iterdir())) == 2


def test_local_custody_is_not_inferred_from_helper_zero_completion(owned):
    _, operation, carrier = owned
    carrier.unsettled_activation = True
    outcome = run(operation, carrier)
    assert outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    assert carrier.snapshot_requests == [] and carrier.activation_calls == 1


def test_uncertain_range_helper_preserves_application_status_and_stops_other_helpers(owned):
    _, operation, carrier = owned
    carrier.range_helper_status = None
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.failure is ExecutionFailure.OUTPUT
    assert result.stdout.data == b"" and result.stderr.data == b""
    assert not result.stdout.complete and not result.stderr.complete
    assert not outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    assert len(carrier.snapshot_requests) == 1 and len(operation.unfinished_inline_executions) == 1
    assert operation._captures[0].helper_pending
    assert carrier.activation_calls == 1


@pytest.mark.parametrize("where", ["activation", "range"])
def test_control_identity_and_original_cause_survive_without_payload_in_safe_fact(owned, where):
    _, operation, carrier = owned
    cause = RuntimeError("prior")
    control = KeyboardInterrupt()
    control.__cause__ = cause
    if where == "activation":
        carrier.activation_control = control
    else:
        carrier.range_control = control
    with pytest.raises(KeyboardInterrupt) as caught:
        run(operation, carrier)
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, InlineExecutionControlFact)
    assert fact.outcome.candidate is None
    prior = fact.__cause__
    while prior is not cause and prior is not None:
        prior = prior.__cause__
    assert prior is cause
    assert fact.outcome.scratch_cleanup_pending and SOURCE not in repr(fact)
    assert carrier.activation_calls == 1 and operation._captures


@pytest.mark.parametrize("sensitive", [False, True])
def test_sensitive_or_discard_output_creates_no_capture_row_or_artifact(owned, sensitive):
    owner, operation, carrier = owned
    outcome = run(operation, carrier, sensitive=sensitive, limit=LIMIT if sensitive else None)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.stdout.data == b"" and result.stderr.data == b""
    assert result.stdout.complete and result.stderr.complete and result.owned_cleanup_confirmed
    assert not operation._captures and not list(carrier.scratch.iterdir())
    assert CAPTURE_OBLIGATION_KIND not in [row.obligation_kind for row in owner.list_pending_lifecycle_obligations()]


def test_unbounded_or_host_capture_refuses_before_effects(owned):
    owner, operation, carrier = owned
    for deadline in (Deadline(None), Deadline.after(0)):
        with pytest.raises(ValidationError):
            run(operation, carrier, deadline=deadline)
    operation._bootstrap = None
    with pytest.raises(ValidationError):
        run(operation, carrier)
    assert carrier.activation_calls == 0 and carrier.requests == []
    assert not owner.list_pending_lifecycle_obligations() and not list(carrier.scratch.iterdir())


def test_expiry_retains_known_wait_and_exact_artifacts_for_fresh_cleanup(owned):
    _, operation, carrier = owned
    carrier.expire_after_activation = True
    deadline = Deadline.after(30)
    outcome = run(operation, carrier, deadline=deadline)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.deadline_exceeded
    assert not result.stdout.complete and not result.stderr.complete and outcome.scratch_cleanup_pending
    assert carrier.snapshot_requests == [] and all(value is deadline for value in carrier.deadlines)
    assert operation._prepared_stages and operation._captures
    fresh = Deadline.after(30)
    operation.finish_scratch_cleanup(fresh)
    assert not operation._prepared_stages and not operation._captures and not list(carrier.scratch.iterdir())
    assert carrier.activation_calls == 1 and carrier.deadlines[-1] is fresh


def test_cleanup_control_keeps_exact_helper_custody_and_original_cause_for_fresh_retry(owned):
    _, operation, carrier = owned
    from agentworks.execution.carrier import FiniteInput

    class CleanupDelivery(ClosureDelivery):
        def execute(self, invocation, *, io, deadline, custody=None):
            assert isinstance(io.input, FiniteInput)
            self.activation = b'"operation":"snapshot_cleanup"' in io.input.data
            report = self.carrier.execute(invocation, io=io, deadline=deadline, custody=custody)
            self.closed = True
            return report

    operation._native_binding = NativeExecutionBinding(
        carrier, "private", RUNTIME, _new_helper_delivery=lambda expectation: CleanupDelivery(carrier)
    )
    cause = ValueError("prior")
    control = KeyboardInterrupt()
    control.__cause__ = cause
    carrier.cleanup_control = control
    with pytest.raises(KeyboardInterrupt) as caught:
        run(operation, carrier)
    assert caught.value is control and isinstance(control.__cause__, InlineExecutionControlFact)
    fact = control.__cause__
    assert fact.outcome.candidate is None and fact.outcome.scratch_cleanup_pending
    prior = fact.__cause__
    while prior is not cause and prior is not None:
        prior = prior.__cause__
    assert prior is cause
    assert operation._captures[0].helper_pending and len(operation.active_inline_calls) == 1
    carrier.cleanup_control = None
    fresh = Deadline.after(30)
    operation.observe_inline_cleanup(fresh)
    operation.finish_scratch_cleanup(fresh)
    assert not operation._captures and not operation.active_inline_calls
    assert carrier.activation_calls == 1 and not list(carrier.scratch.iterdir())


def test_failed_write_retains_receipt_for_reconcile_and_exact_cleanup(owned):
    _, operation, carrier = owned
    carrier.fail_write = True
    outcome = run(operation, carrier)
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(23) and result.failure is ExecutionFailure.OUTPUT
    assert result.stdout.data == b"" and result.stderr.data == b""
    assert result.owned_cleanup_confirmed and not outcome.requires_owner_retention
    assert carrier.snapshot_requests and not list(carrier.scratch.iterdir())


def test_native_closure_is_required_after_wait_and_cleanup_reuses_exact_delivery(owned):
    _, operation, carrier = owned
    deliveries = []

    def delivery(expectation):
        value = ClosureDelivery(carrier)
        deliveries.append(value)
        return value

    operation._native_binding = NativeExecutionBinding(carrier, "private", RUNTIME, _new_helper_delivery=delivery)
    outcome = run(operation, carrier)
    assert outcome.helper_requires_owner_retention and outcome.scratch_cleanup_pending
    assert carrier.snapshot_requests == [] and carrier.activation_calls == 1
    operation.observe_inline_cleanup(Deadline.after(30))
    operation.finish_scratch_cleanup(Deadline.after(30))
    assert not operation._captures and not operation._prepared_stages
    assert carrier.activation_calls == 1 and sum(value.observations for value in deliveries) == 1


def test_complete_read_envelope_refusal_precedes_any_owner_or_scratch_effect(owned, monkeypatch):
    owner, operation, carrier = owned
    from agentworks.execution._file_snapshot_bundle import ROOT_PROGRAM
    from agentworks.execution.carrier import FiniteInput

    original = carrier.validate

    def validate(invocation, *, io):
        original(invocation, io=io)
        if isinstance(io.input, FiniteInput) and io.input.data.startswith(ROOT_PROGRAM.prefix):
            raise ValidationError("Synthetic complete read-envelope refusal")

    monkeypatch.setattr(carrier, "validate", validate)
    with pytest.raises(ValidationError):
        run(operation, carrier)
    assert carrier.requests == [] and carrier.activation_calls == 0
    assert not owner.list_pending_lifecycle_obligations() and not list(carrier.scratch.iterdir())


def test_mixed_input_and_capture_cleanup_stops_at_pending_first_closing_helper(owned):
    owner, operation, carrier = owned
    carrier.fail_stage_cleanup = carrier.fail_cleanup = True
    outcome = run(operation, carrier)
    assert outcome.scratch_cleanup_pending and operation._prepared_stages and operation._captures
    carrier.fail_stage_cleanup = carrier.fail_cleanup = False
    # This closing-only delivery has no native closure proof yet. Its receipt
    # acknowledgement alone cannot authorize the next resource's cleanup.
    from .test_prepared_stage_closing import NativeDelivery, ScriptedCarrier

    stage_cleanup = operation._prepared_stages[0]
    stage = stage_cleanup._stage
    base = ScriptedCarrier(owner)
    base.reference = stage.reference
    stage._carrier = base
    retained = []

    def new_delivery(expectation):
        value = NativeDelivery(base, expectation)
        retained.append(value)
        return value

    stage._new_helper_delivery = new_delivery
    owner.stop_admission()
    operation.finish()
    before = len(carrier.snapshot_requests)
    with pytest.raises(StateError):
        operation.finish_scratch_cleanup(Deadline.after(30))
    assert operation.closing_scratch_helper_pending and len(carrier.snapshot_requests) == before
    assert len(base.requests) == 0 and len(retained[0].requests) == 1
    fresh = Deadline.after(30)
    retained[0].proven = True
    operation.finish_scratch_cleanup(fresh)
    assert not operation._prepared_stages and not operation._captures and carrier.activation_calls == 1
