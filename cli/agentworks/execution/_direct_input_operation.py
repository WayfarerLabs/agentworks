"""Serial prepared DIRECT input under the original operation's custody."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError

from ._direct_input_preparation import prepare_direct_input
from ._inline import prepare_inline_candidate
from ._prepared_helper_stage import PreparedHelperStage
from ._prepared_stage_closing import PreparedStageCleanup
from .carrier import Dispatch

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._capture_cleanup import CaptureCleanup
    from ._execution_operation import ExecutionOperation, OwnedInlineOutcome, _ActiveHelperCall
    from ._helper_launcher import IdentityPlan
    from ._runtime_prerequisite import RuntimeSelection
    from .carrier import ByteSink, ByteSource, Carrier, Deadline
    from .models import Command, Script


def note_input_activation_closed(active: _ActiveHelperCall) -> None:
    """Record exact nondispatch or actual helper closure, never application WAITED."""
    stage = active.input_stage
    if stage is None or not stage._activation_prepared or stage._activation_settled:
        return
    operation = active.operation
    attempt = operation.outstanding_attempt
    if attempt is not None:
        active.input_delivery = attempt.local_delivery
    if not operation.delivery_started:
        stage.note_activation_not_dispatched()
        return
    custody = active.input_delivery
    if custody is None or not custody.settled:
        return
    candidate, delivery = active.candidate, active.closure_delivery
    if active.not_sent or (candidate is not None and candidate.dispatch is Dispatch.NOT_SENT):
        stage.note_activation_completion(Dispatch.NOT_SENT, None, custody)
    elif delivery is not None:
        if delivery.closure_proven:
            stage.note_activation_closure(custody)
    elif candidate is not None:
        stage.note_activation_completion(candidate.dispatch, candidate.carrier_completion, custody)


def _cleanup_input(operation: ExecutionOperation, cleanup: PreparedStageCleanup, deadline: Deadline) -> None:
    """Use the retained stage controller only after the helper relinquishes its borrow."""
    if operation._active_inline_calls or operation._unfinished_inline_executions or deadline.expired:
        return
    try:
        finished = cleanup.finish(deadline)
    except Exception:
        # The existing controller retains exact custody for aggregate closing.
        # A cleanup-only failure does not discard known application evidence.
        return
    if finished:
        operation._prepared_stages.remove(cleanup)


def run_direct_input(
    operation: ExecutionOperation,
    carrier: Carrier,
    request: Command | Script,
    *,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    stdin: bytes,
    env: Mapping[str, str] | None,
    cwd: str | None,
    capture_limit: int | None,
    sensitive: bool,
    source: ByteSource | None,
    stdout: ByteSink | None,
    stderr: ByteSink | None,
    allow_staging: bool = True,
    capture: CaptureCleanup | None = None,
) -> OwnedInlineOutcome:
    """Reserve preparation, select complete delivery, then transfer into one attempt."""
    from ._execution_operation import InlineExecutionControlFact, OwnedInlineOutcome

    reservation = object()
    with operation._admission_guard:
        operation._require_no_inline_preparation()
        if (
            operation._finishing
            or operation._finished
            or operation._active_inline_calls
            or operation._unfinished_inline_executions
        ):
            raise StateError("Inline execution cannot admit new work")
        operation._inline_preparation = reservation
    prepared = None
    preparation = None
    stage = None
    cleanup = None
    bootstrap = operation._bootstrap
    try:
        try:
            prepared = prepare_inline_candidate(
                request,
                plan=plan,
                stdin=stdin,
                env=env,
                cwd=cwd,
                capture_limit=capture_limit,
                sensitive=sensitive,
                source=source,
                stdout=stdout,
                stderr=stderr,
                runtime_selection=runtime_selection,
                bootstrap=operation._bootstrap,
                _capture_tokens=None if capture is None else capture.binding.tokens,
            )
            carrier.validate(prepared.invocation, io=prepared.io)
        except ValidationError:
            if prepared is not None:
                prepared.release()
                prepared = None
            if (
                not allow_staging
                or bootstrap is None
                or deadline.expires_at is None
                or source is not None
                or stdout is not None
                or stderr is not None
            ):
                raise
        if prepared is None:
            assert bootstrap is not None
            preparation = prepare_direct_input(
                carrier,
                request,
                plan=plan,
                bootstrap=bootstrap,
                deadline=deadline,
                runtime_selection=runtime_selection,
                stdin=stdin,
                env=env,
                cwd=cwd,
                capture_limit=capture_limit,
                sensitive=sensitive,
                capture_tokens=None if capture is None else capture.binding.tokens,
            )
            prepared = preparation.candidate
            binding = operation._native_binding
            stage = PreparedHelperStage(
                operation._owner,
                carrier,
                operation._target,
                bootstrap,
                runtime_selection,
                deadline,
                len(preparation.data),
                before_dispatch=operation._route_check,
                new_helper_delivery=(
                    binding._new_helper_delivery if binding is not None and binding.carrier is carrier else None
                ),
            )
            cleanup = PreparedStageCleanup(stage)
            operation._prepared_stages.append(cleanup)
            if capture is not None:
                capture.register()
            if not stage.begin() or not stage.append(preparation.data):
                raise StateError("Prepared DIRECT input could not be staged")
            preparation.activate(stage.prepare_activation(prepared.nonce, preparation.digest))
            preparation.clear()
            carrier.validate(prepared.invocation, io=prepared.io)
        if capture is not None and not capture.registered:
            capture.register()
        outcome = operation._run_inline_prepared(carrier, prepared, deadline, reservation, stage, capture)
        if cleanup is not None:
            _cleanup_input(operation, cleanup, deadline)
        return replace(
            outcome,
            scratch_cleanup_pending=operation._scratch_cleanup_pending,
            deadline_exceeded=deadline.expired,
        )
    except BaseException as control:
        if stage is not None:
            # An escaping registration can publish the active call before its
            # caller receives it. Inspect that actual link before nondispatch.
            try:
                calls = tuple(
                    active for active in operation._active_inline_calls.values() if active.input_stage is stage
                )
                for active in calls:
                    note_input_activation_closed(active)
                if (
                    not calls
                    and operation._inline_preparation is reservation
                    and stage._activation_prepared
                    and not stage._activation_settled
                ):
                    stage.note_activation_not_dispatched()
                if cleanup is not None:
                    _cleanup_input(operation, cleanup, deadline)
            except BaseException:
                # Preserve the original control and its cause even if draining
                # or local accounting is interrupted independently.
                pass
            prior = control.__cause__
            if isinstance(prior, InlineExecutionControlFact):
                prior.outcome = replace(prior.outcome, scratch_cleanup_pending=operation._scratch_cleanup_pending)
            else:
                fact = InlineExecutionControlFact(
                    OwnedInlineOutcome(scratch_cleanup_pending=operation._scratch_cleanup_pending)
                )
                fact.__cause__ = prior
                raise control from fact
        raise
    finally:
        if preparation is not None:
            preparation.clear()
        if prepared is not None:
            prepared.release()
        with operation._admission_guard:
            if operation._inline_preparation is reservation:
                operation._inline_preparation = None
