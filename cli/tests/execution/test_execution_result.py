"""Reduction of owned inline evidence into safe public execution results."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import traceback

import pytest

from agentworks.execution._execution_operation import OwnedInlineOutcome
from agentworks.execution._execution_result import check_owned_inline_result, reduce_owned_inline_result
from agentworks.execution._inline import InlineCandidateResult
from agentworks.execution._inline_control import (
    FailureCode,
    FailureFact,
    FailurePhase,
    StreamRetention,
    WaitFact,
    WaitKind,
)
from agentworks.execution._inline_observer import InlineObservation, ObservationError, StreamObservation
from agentworks.execution._inline_request import OutputMode
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteObservation, RuntimePrerequisiteState
from agentworks.execution.carrier import Dispatch, ExitStatus, Failure, Retention
from agentworks.execution.diagnostics import ExecutionFailureReason, ExecutionPhase
from agentworks.execution.result import ApplicationState, CheckedExecutionError, ExecutionFailure, ExitCode

_NORMAL_WAIT = WaitFact(WaitKind.EXIT, 0)


def _stream(
    data: bytes = b"",
    *,
    complete: bool = True,
    truncated: bool = False,
    retention: StreamRetention = StreamRetention.CAPTURED,
) -> StreamObservation:
    return StreamObservation(data, len(data), hashlib.sha256(data).hexdigest(), complete, truncated, retention)


def _observation(
    *,
    wait: WaitFact | None = _NORMAL_WAIT,
    failure: FailureFact | None = None,
    error: ObservationError | None = None,
    terminal: bool = True,
    stdout: StreamObservation | None = None,
    stderr: StreamObservation | None = None,
) -> InlineObservation:
    return InlineObservation(
        launching=True,
        stdout=_stream() if stdout is None else stdout,
        stderr=_stream() if stderr is None else stderr,
        wait=wait,
        failure=failure,
        trusted_terminal=terminal,
        error=error,
    )


def _candidate(
    observation: InlineObservation | None = None,
    *,
    dispatch: Dispatch = Dispatch.SENT,
    runtime: RuntimePrerequisiteState = RuntimePrerequisiteState.READY,
    carrier_failure: Failure | None = None,
    carrier_completion: ExitStatus | None = None,
    output: OutputMode = OutputMode.CAPTURE,
) -> InlineCandidateResult:
    return InlineCandidateResult(
        dispatch,
        carrier_completion,
        None,
        carrier_failure,
        RuntimePrerequisiteObservation(runtime, None),
        observation if observation is not None else _observation(),
        output,
    )


def _reduce(
    candidate: InlineCandidateResult | None = None,
    **outcome: bool,
):
    return reduce_owned_inline_result(OwnedInlineOutcome(candidate=candidate or _candidate(), **outcome))


def _check(
    candidate: InlineCandidateResult | None = None,
    **outcome: bool,
):
    return check_owned_inline_result(
        OwnedInlineOutcome(candidate=candidate or _candidate(), **outcome),
        entity_kind="vm",
        entity_name="test-vm",
    )


def test_retrospective_normal_wait_returns_exact_exit_code_for_every_value() -> None:
    for status in range(256):
        result = _reduce(_candidate(_observation(wait=WaitFact(WaitKind.EXIT, status))))
        assert result.application_state is ApplicationState.COMPLETED
        assert result.status == ExitCode(status)


def test_exact_signal_remains_unknown_without_a_synthetic_started_state() -> None:
    result = _reduce(_candidate(_observation(wait=WaitFact(WaitKind.SIGNAL, 15))))

    assert result.application_state is ApplicationState.UNKNOWN
    assert result.status is None


@pytest.mark.parametrize(
    "runtime",
    [
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.SHIM,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    ],
)
def test_runtime_refusal_is_a_clean_preparation_result(runtime: RuntimePrerequisiteState) -> None:
    result = _reduce(_candidate(None, runtime=runtime, output=OutputMode.DISCARD))

    assert result.application_state is ApplicationState.NOT_STARTED
    assert result.failure is ExecutionFailure.PREPARATION
    assert result.stdout.retention is result.stderr.retention is Retention.DISCARDED


def test_unknown_runtime_admission_stays_unknown() -> None:
    result = _reduce(_candidate(runtime=RuntimePrerequisiteState.UNKNOWN))

    assert result.application_state is ApplicationState.UNKNOWN
    assert result.failure is ExecutionFailure.OBSERVATION


@pytest.mark.parametrize(
    "failure",
    [
        FailureFact(FailurePhase.PREPARE, FailureCode.RUNTIME),
        FailureFact(FailurePhase.LAUNCH, FailureCode.DISPATCH),
    ],
)
def test_trusted_prelaunch_and_launch_refusals_establish_not_started(failure: FailureFact) -> None:
    result = _reduce(_candidate(_observation(wait=None, failure=failure)))

    assert result.application_state is ApplicationState.NOT_STARTED
    assert result.status is None
    assert result.failure is (
        ExecutionFailure.PREPARATION if failure.phase is FailurePhase.PREPARE else ExecutionFailure.DELIVERY
    )


@pytest.mark.parametrize(
    "error",
    [ObservationError.MISSING_TERMINAL, ObservationError.CONTROL, ObservationError.POST_TERMINAL],
)
def test_missing_corrupt_or_post_terminal_evidence_fails_closed(error: ObservationError) -> None:
    result = _reduce(_candidate(_observation(error=error, terminal=False)))

    assert result.application_state is ApplicationState.UNKNOWN
    assert result.status is None
    assert result.failure is ExecutionFailure.PROTOCOL
    assert not result.owned_cleanup_confirmed
    assert result.stdout.data == result.stderr.data == b""


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FailureFact(FailurePhase.OBSERVE, FailureCode.INPUT), ExecutionFailure.INPUT),
        (FailureFact(FailurePhase.OBSERVE, FailureCode.OUTPUT), ExecutionFailure.OUTPUT),
        (FailureFact(FailurePhase.OBSERVE, FailureCode.OBSERVATION), ExecutionFailure.OBSERVATION),
        (FailureFact(FailurePhase.CLEANUP, FailureCode.RESOURCE), ExecutionFailure.CLEANUP),
    ],
)
def test_helper_failures_keep_their_safe_public_category(
    failure: FailureFact,
    expected: ExecutionFailure,
) -> None:
    result = _reduce(_candidate(_observation(failure=failure)))

    assert result.failure is expected
    assert result.owned_cleanup_confirmed is (failure.phase is not FailurePhase.CLEANUP)


@pytest.mark.parametrize(
    ("failure", "phase", "reason"),
    [
        (
            FailureFact(FailurePhase.OBSERVE, FailureCode.INPUT),
            ExecutionPhase.OBSERVATION,
            ExecutionFailureReason.INPUT,
        ),
        (
            FailureFact(FailurePhase.OBSERVE, FailureCode.OUTPUT),
            ExecutionPhase.OBSERVATION,
            ExecutionFailureReason.OUTPUT,
        ),
        (
            FailureFact(FailurePhase.OBSERVE, FailureCode.OBSERVATION),
            ExecutionPhase.OBSERVATION,
            ExecutionFailureReason.OBSERVATION,
        ),
        (
            FailureFact(FailurePhase.CLEANUP, FailureCode.RESOURCE),
            ExecutionPhase.CLEANUP,
            ExecutionFailureReason.CLEANUP,
        ),
    ],
)
def test_inline_checked_reducer_preserves_trusted_helper_phase(
    failure: FailureFact,
    phase: ExecutionPhase,
    reason: ExecutionFailureReason,
) -> None:
    with pytest.raises(CheckedExecutionError) as caught:
        _check(_candidate(_observation(wait=None, failure=failure)))

    assert caught.value.result.failure is not None
    assert caught.value.details is not None
    assert caught.value.details.phase is phase
    assert caught.value.details.reason is reason


def test_inline_checked_reducer_keeps_carrier_input_failure_phase_unknown() -> None:
    with pytest.raises(CheckedExecutionError) as caught:
        _check(_candidate(carrier_failure=Failure.INPUT))

    assert caught.value.result.failure is ExecutionFailure.INPUT
    assert caught.value.details is not None
    assert caught.value.details.phase is ExecutionPhase.UNKNOWN
    assert caught.value.details.reason is ExecutionFailureReason.INPUT


def test_inline_checked_reducer_returns_a_successful_result() -> None:
    result = _check()

    assert result.ok
    assert result.status == ExitCode(0)


def test_inline_checked_reducer_projects_known_nonzero_application_status() -> None:
    with pytest.raises(CheckedExecutionError) as caught:
        _check(_candidate(_observation(wait=WaitFact(WaitKind.EXIT, 1))))

    assert caught.value.result.status == ExitCode(1)
    assert caught.value.details is not None
    assert caught.value.details.phase is ExecutionPhase.APPLICATION
    assert caught.value.details.reason is ExecutionFailureReason.APPLICATION_STATUS


def test_inline_checked_reducer_does_not_retain_payload_or_caller_exception_context() -> None:
    payload = b"private-output-canary"
    context = "provider-exception-canary"

    try:
        raise RuntimeError(context)
    except RuntimeError:
        with pytest.raises(CheckedExecutionError) as caught:
            _check(_candidate(_observation(wait=WaitFact(WaitKind.EXIT, 1), stdout=_stream(payload))))

    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert payload.decode() not in rendered
    assert context not in rendered


@pytest.mark.parametrize(
    ("mode", "stream_retention"),
    [
        (OutputMode.DISCARD, StreamRetention.DISCARDED),
        (OutputMode.SUPPRESS, StreamRetention.SUPPRESSED),
    ],
)
def test_non_capture_output_preserves_retention_and_complete_observation(
    mode: OutputMode,
    stream_retention: StreamRetention,
) -> None:
    observation = _observation(
        stdout=_stream(retention=stream_retention),
        stderr=_stream(retention=stream_retention),
    )
    result = _reduce(_candidate(observation, output=mode))

    assert result.application_state is ApplicationState.COMPLETED
    assert result.stdout.retention.value == result.stderr.retention.value == stream_retention.value
    assert result.stdout.complete and result.stderr.complete


def test_captured_prefix_and_output_limit_are_preserved() -> None:
    observation = _observation(
        stdout=_stream(b"prefix", complete=False, truncated=True),
        stderr=_stream(),
    )
    result = _reduce(_candidate(observation))

    assert result.application_state is ApplicationState.COMPLETED
    assert result.stdout.data == b"prefix"
    assert not result.stdout.complete
    assert result.stdout.retention is Retention.CAPTURED
    assert result.failure is ExecutionFailure.OUTPUT_LIMIT


def test_trusted_terminal_survives_later_carrier_failure() -> None:
    result = _reduce(_candidate(carrier_failure=Failure.OUTPUT))

    assert result.application_state is ApplicationState.COMPLETED
    assert result.status == ExitCode(0)
    assert result.failure is ExecutionFailure.OUTPUT
    assert result.owned_cleanup_confirmed


def test_deadline_is_preserved_independently_of_carrier_failure() -> None:
    result = _reduce(_candidate(carrier_failure=Failure.DEADLINE), deadline_exceeded=True)

    assert result.application_state is ApplicationState.COMPLETED
    assert result.failure is ExecutionFailure.DEADLINE
    assert result.deadline_exceeded


@pytest.mark.parametrize(
    ("dispatch", "expected"),
    [
        (Dispatch.NOT_SENT, ApplicationState.UNKNOWN),
        (Dispatch.SENT, ApplicationState.COMPLETED),
        (Dispatch.UNKNOWN, ApplicationState.COMPLETED),
    ],
)
def test_dispatch_never_substitutes_for_helper_evidence(dispatch: Dispatch, expected: ApplicationState) -> None:
    result = _reduce(_candidate(dispatch=dispatch))

    assert result.application_state is expected
    assert result.status == (ExitCode(0) if expected is ApplicationState.COMPLETED else None)


def test_raw_carrier_completion_never_substitutes_for_a_helper_terminal() -> None:
    result = _reduce(
        _candidate(
            _observation(terminal=False),
            carrier_completion=ExitStatus(code=0),
        )
    )

    assert result.application_state is ApplicationState.UNKNOWN
    assert result.status is None


@pytest.mark.parametrize("retained", ["pending_remote_effects", "coordination_uncertain", "requires_owner_retention"])
def test_retained_operation_ownership_blocks_completion_and_cleanup(retained: str) -> None:
    result = _reduce(**{retained: True})

    assert result.application_state is ApplicationState.UNKNOWN
    assert result.status is None
    assert not result.owned_cleanup_confirmed


def test_public_result_and_fresh_reducer_import_do_not_retain_secrets() -> None:
    canary = b"execution-result-secret-canary"
    result = _reduce(_candidate(_observation(stdout=_stream(canary), stderr=_stream())))

    assert canary.decode() not in repr(result)
    script = r"""
import importlib.abc
import sys

retired = ("agentworks.transports", "agentworks.ssh", "agentworks.remote_exec")
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in retired):
            raise ImportError(fullname)

sys.meta_path.insert(0, BlockRetired())
from agentworks.execution._execution_result import reduce_owned_inline_result
assert reduce_owned_inline_result
assert not any(loaded == name or loaded.startswith(name + ".") for loaded in sys.modules for name in retired)
"""
    imported = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)
    assert imported.returncode == 0, imported.stderr.decode(errors="replace")
