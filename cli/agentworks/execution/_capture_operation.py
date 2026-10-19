"""One finite Linux VM application followed by bounded capture retrieval and cleanup."""

from __future__ import annotations

import secrets
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._capture_cleanup import CaptureCleanup
from ._capture_obligation import CaptureObligation
from ._capture_preparation import validate_capture
from ._capture_read import retrieve_capture
from ._direct_input_operation import run_direct_input
from ._prepared_helper_obligation import PreparedHelperObligation
from ._runtime_prerequisite import RuntimeTargetOS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._execution_operation import ExecutionOperation, OwnedInlineOutcome
    from ._helper_launcher import IdentityPlan
    from ._runtime_prerequisite import RuntimeSelection
    from .carrier import ByteSink, ByteSource, Carrier, Deadline
    from .models import Command, Script


def _finish(operation: ExecutionOperation, cleanup: CaptureCleanup, deadline: Deadline) -> None:
    if deadline.expired or operation._active_inline_calls or operation._unfinished_inline_executions:
        return
    if operation.scratch_helper_pending:
        return
    try:
        if cleanup.finish(deadline):
            operation._captures.remove(cleanup)
    except Exception:
        # The exact controller remains retained for a fresh finite continuation.
        pass


def run_capture(
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
    capture_limit: int,
    sensitive: bool,
    source: ByteSource | None,
    stdout: ByteSink | None,
    stderr: ByteSink | None,
) -> OwnedInlineOutcome:
    """Admit all fixed controls before any scratch creation or application dispatch."""
    from ._execution_operation import InlineExecutionControlFact, OwnedInlineOutcome

    if not 4096 < capture_limit <= 2**63 - 2:
        raise ValidationError("Scratch capture limit exceeds its fixed admission")
    if sensitive:
        return run_direct_input(
            operation,
            carrier,
            request,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            stdin=stdin,
            env=env,
            cwd=cwd,
            capture_limit=4096,
            sensitive=True,
            source=source,
            stdout=stdout,
            stderr=stderr,
        )
    bootstrap = operation._bootstrap
    if (
        bootstrap is None
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or deadline.expires_at is None
        or deadline.expired
        or source is not None
        or stdout is not None
        or stderr is not None
        or type(stdin) is not bytes
    ):
        raise ValidationError("Scratch capture requires finite Linux VM input, budget, and buffered output")
    binding = CaptureObligation(
        PreparedHelperObligation(operation._target, bootstrap, runtime_selection, secrets.token_bytes(16)),
        plan,
        secrets.token_bytes(16),
    )
    cleanup = CaptureCleanup(operation, carrier, binding)
    validate_capture(carrier, binding, capture_limit, deadline)
    operation._captures.append(cleanup)
    try:
        outcome = run_direct_input(
            operation,
            carrier,
            request,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            stdin=stdin,
            env=env,
            cwd=cwd,
            capture_limit=capture_limit,
            sensitive=False,
            source=None,
            stdout=None,
            stderr=None,
            capture=cleanup,
        )
        candidate = outcome.candidate
        if (
            candidate is not None
            and candidate.observation is not None
            and candidate.observation.trusted_terminal
            and candidate.observation.error is None
            and not outcome.helper_requires_owner_retention
            and cleanup.application_closed
            and not operation.scratch_helper_pending
        ):
            retrieved = retrieve_capture(cleanup, candidate.observation.capture_facts, deadline)
            candidate = replace(candidate, retrieved_capture=retrieved)
        _finish(operation, cleanup, deadline)
        return replace(
            outcome,
            candidate=candidate,
            scratch_cleanup_pending=operation._scratch_cleanup_pending,
            deadline_exceeded=deadline.expired,
        )
    except BaseException as control:
        calls = tuple(active for active in operation._active_inline_calls.values() if active.capture is cleanup)
        if not calls and not cleanup.activation_seen:
            # No actual activation was published; registration/staging failures
            # cannot have entered the capture publisher.
            cleanup.note_not_dispatched()
        # Cleanup interruption cannot replace the original control.
        with suppress(BaseException):
            _finish(operation, cleanup, deadline)
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
        if not cleanup.pending and cleanup in operation._captures:
            operation._captures.remove(cleanup)
