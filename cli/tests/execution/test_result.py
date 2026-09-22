"""Public execution-result invariants and success behavior."""

from __future__ import annotations

import traceback
from dataclasses import FrozenInstanceError, dataclass

import pytest

from agentworks.errors import ExternalError, ValidationError
from agentworks.execution.carrier import Dispatch, Retention
from agentworks.execution.diagnostics import (
    ExecutionDiagnostic,
    ExecutionFailureReason,
    ExecutionPhase,
    check_execution_result,
)
from agentworks.execution.result import (
    ApplicationState,
    CheckedExecutionError,
    ExecutionFailure,
    ExecutionOutput,
    ExecutionResult,
    ExitCode,
    Signal,
    WaitCode,
)


def _result(**changes: object) -> ExecutionResult:
    values = {
        "dispatch": Dispatch.SENT,
        "application_state": ApplicationState.COMPLETED,
        "status": ExitCode(0),
        "stdout": ExecutionOutput(complete=True),
        "stderr": ExecutionOutput(complete=True),
        "owned_cleanup_confirmed": True,
    }
    values.update(changes)
    return ExecutionResult(**values)  # type: ignore[arg-type]


def test_every_supported_exit_value_keeps_its_exact_success_meaning() -> None:
    for value in range(256):
        assert _result(status=ExitCode(value)).ok is (value == 0)
        assert _result(status=WaitCode(value)).ok is (value == 0)


def test_wait_status_does_not_invent_signal_precision() -> None:
    wait_status = WaitCode(143)
    exit_status = ExitCode(143)
    assert not _result(status=wait_status).ok
    assert not _result(status=exit_status).ok
    assert not _result(status=Signal(15)).ok
    assert type(wait_status) is WaitCode and type(exit_status) is ExitCode


@pytest.mark.parametrize(
    "changes",
    [
        {"application_state": ApplicationState.UNKNOWN, "status": None},
        {"application_state": ApplicationState.STARTED, "status": None},
        {"failure": ExecutionFailure.OBSERVATION},
        {"deadline_exceeded": True},
        {"owned_cleanup_confirmed": False},
    ],
)
def test_success_requires_independent_completion_facts(changes: dict[str, object]) -> None:
    assert not _result(**changes).ok


def test_known_application_completion_can_survive_uncertain_carrier_dispatch() -> None:
    assert _result(dispatch=Dispatch.UNKNOWN).ok


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (ExecutionOutput(b"partial", complete=False), False),
        (ExecutionOutput(b"complete", complete=True), True),
        (ExecutionOutput(complete=False, retention=Retention.DELIVERED), False),
        (ExecutionOutput(complete=True, retention=Retention.DELIVERED), True),
        (ExecutionOutput(complete=False, retention=Retention.DISCARDED), True),
        (ExecutionOutput(complete=False, retention=Retention.SUPPRESSED), True),
    ],
)
def test_output_retention_preserves_requested_success_semantics(output: ExecutionOutput, expected: bool) -> None:
    assert _result(stdout=output).ok is expected


@pytest.mark.parametrize("retention", [Retention.DELIVERED, Retention.DISCARDED, Retention.SUPPRESSED])
def test_unretained_output_cannot_contain_bytes(retention: Retention) -> None:
    with pytest.raises(ValidationError):
        ExecutionOutput(b"private", retention=retention)


def test_falsey_bytes_subclass_cannot_bypass_unretained_output_validation() -> None:
    class FalseyBytes(bytes):
        def __bool__(self) -> bool:
            return False

    with pytest.raises(ValidationError):
        ExecutionOutput(FalseyBytes(b"private"), complete=True, retention=Retention.DELIVERED)


def test_result_rejects_output_extensions_with_diagnostic_fields() -> None:
    @dataclass(frozen=True)
    class ExtendedOutput(ExecutionOutput):
        provider_diagnostic: bytes = b"provider-output-canary"

    with pytest.raises(ValidationError) as caught:
        _result(stdout=ExtendedOutput(complete=True))
    assert "provider-output-canary" not in repr(caught.value)


def test_result_rejects_status_extensions_with_diagnostic_fields() -> None:
    @dataclass(frozen=True)
    class ExtendedExitCode(ExitCode):
        provider_diagnostic: bytes = b"provider-status-canary"

    with pytest.raises(ValidationError) as caught:
        _result(status=ExtendedExitCode(0))
    assert "provider-status-canary" not in repr(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"data": bytearray(b"output")},
        {"complete": 1},
        {"retention": "captured"},
    ],
)
def test_output_refuses_invalid_public_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExecutionOutput(**changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status_type", "value"),
    [
        (WaitCode, -1),
        (WaitCode, 256),
        (ExitCode, -1),
        (ExitCode, 256),
        (Signal, 0),
        (Signal, 256),
        (ExitCode, True),
    ],
)
def test_status_values_are_bounded_for_untyped_clients(status_type: type, value: object) -> None:
    with pytest.raises(ValidationError):
        status_type(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 0},
        {"application_state": ApplicationState.COMPLETED, "status": None},
        {"application_state": ApplicationState.UNKNOWN, "status": ExitCode(0)},
        {"dispatch": Dispatch.NOT_SENT, "application_state": ApplicationState.STARTED, "status": None},
        {"dispatch": Dispatch.NOT_SENT},
        {"dispatch": "sent"},
        {"application_state": "completed"},
        {"stdout": b"output"},
        {"failure": "output"},
        {"owned_cleanup_confirmed": 1},
        {"deadline_exceeded": 0},
    ],
)
def test_result_refuses_invalid_public_combinations(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _result(**changes)


def test_check_returns_the_unchanged_successful_result() -> None:
    result = _result()
    assert result.check() is result


def test_check_carries_the_same_immutable_safe_result_without_rendering_context() -> None:
    output_canary = b"private-output-canary"
    context_canary = "provider-exception-canary"
    result = _result(stdout=ExecutionOutput(output_canary, complete=True), status=ExitCode(1))

    try:
        raise RuntimeError(context_canary)
    except RuntimeError:
        with pytest.raises(CheckedExecutionError) as caught:
            result.check()

    assert isinstance(caught.value, ExternalError)
    assert caught.value.result is result
    with pytest.raises(FrozenInstanceError):
        caught.value.result.deadline_exceeded = True  # type: ignore[misc]
    rendered = repr(result) + repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert output_canary.decode() not in rendered
    assert context_canary not in rendered


def test_diagnostic_check_binds_safe_target_and_application_status_to_the_same_result() -> None:
    result = _result(status=ExitCode(1))
    diagnostic = ExecutionDiagnostic.from_result(result, entity_kind="vm", entity_name="test-vm")

    with pytest.raises(CheckedExecutionError) as caught:
        diagnostic.check()

    error = caught.value
    assert error.result is result
    assert error.entity_kind == "vm"
    assert error.entity_name == "test-vm"
    assert error.details is not None
    assert error.details.phase is ExecutionPhase.APPLICATION
    assert error.details.reason is ExecutionFailureReason.APPLICATION_STATUS


@pytest.mark.parametrize(
    ("failure", "phase", "reason"),
    [
        (ExecutionFailure.PREPARATION, ExecutionPhase.PREPARATION, ExecutionFailureReason.PREPARATION),
        (ExecutionFailure.DELIVERY, ExecutionPhase.DELIVERY, ExecutionFailureReason.DELIVERY),
        (ExecutionFailure.DEADLINE, ExecutionPhase.UNKNOWN, ExecutionFailureReason.DEADLINE),
        (ExecutionFailure.OBSERVATION, ExecutionPhase.UNKNOWN, ExecutionFailureReason.OBSERVATION),
        (ExecutionFailure.PROTOCOL, ExecutionPhase.UNKNOWN, ExecutionFailureReason.PROTOCOL),
        (ExecutionFailure.INPUT, ExecutionPhase.UNKNOWN, ExecutionFailureReason.INPUT),
        (ExecutionFailure.OUTPUT, ExecutionPhase.UNKNOWN, ExecutionFailureReason.OUTPUT),
        (ExecutionFailure.OUTPUT_LIMIT, ExecutionPhase.UNKNOWN, ExecutionFailureReason.OUTPUT_LIMIT),
        (ExecutionFailure.CLEANUP, ExecutionPhase.CLEANUP, ExecutionFailureReason.CLEANUP),
    ],
)
def test_result_diagnostics_keep_only_failure_phase_precision(
    failure: ExecutionFailure,
    phase: ExecutionPhase,
    reason: ExecutionFailureReason,
) -> None:
    diagnostic = ExecutionDiagnostic.from_result(_result(failure=failure), entity_kind="vm", entity_name="test-vm")

    assert diagnostic.phase is phase
    assert diagnostic.reason is reason


def test_result_diagnostic_does_not_infer_phase_from_deadline_or_incomplete_ownership() -> None:
    deadline = ExecutionDiagnostic.from_result(_result(deadline_exceeded=True), entity_kind="vm", entity_name="test-vm")
    incomplete = ExecutionDiagnostic.from_result(
        _result(owned_cleanup_confirmed=False), entity_kind="vm", entity_name="test-vm"
    )

    assert (deadline.phase, deadline.reason) == (ExecutionPhase.UNKNOWN, ExecutionFailureReason.DEADLINE)
    assert (incomplete.phase, incomplete.reason) == (ExecutionPhase.UNKNOWN, ExecutionFailureReason.INCOMPLETE)


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_kind": ""},
        {"entity_kind": " vm"},
        {"entity_name": "vm/name"},
        {"entity_name": "vm\\name"},
        {"entity_name": "vm\nname"},
        {"entity_name": "vm\x00name"},
        {"entity_name": "vm-caf\u00e9"},
        {"entity_name": ""},
        {"phase": "application"},
        {"reason": "application_status"},
    ],
)
def test_execution_diagnostic_refuses_untyped_or_incomplete_context(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "result": _result(status=ExitCode(1)),
        "entity_kind": "vm",
        "entity_name": "test-vm",
        "phase": ExecutionPhase.APPLICATION,
        "reason": ExecutionFailureReason.APPLICATION_STATUS,
    }
    values.update(changes)

    with pytest.raises(ValidationError):
        ExecutionDiagnostic(**values)  # type: ignore[arg-type]


def test_execution_diagnostic_refuses_success_and_reason_that_disagrees_with_its_result() -> None:
    success = _result()
    failed = _result(status=ExitCode(1))

    with pytest.raises(ValidationError):
        ExecutionDiagnostic.from_result(success, entity_kind="vm", entity_name="test-vm")
    with pytest.raises(ValidationError):
        ExecutionDiagnostic(
            failed,
            "vm",
            "test-vm",
            ExecutionPhase.APPLICATION,
            ExecutionFailureReason.OUTPUT,
        )


def test_check_execution_result_returns_success_without_constructing_a_diagnostic() -> None:
    result = _result()

    assert check_execution_result(result, entity_kind="vm", entity_name="test-vm") is result


def test_check_execution_result_validates_context_that_would_become_public_on_failure() -> None:
    result = _result()

    with pytest.raises(ValidationError):
        check_execution_result(result, entity_kind="vm", entity_name="unsafe/name")
    with pytest.raises(ValidationError):
        check_execution_result(
            result,
            entity_kind="vm",
            entity_name="test-vm",
            phase=ExecutionPhase.APPLICATION,
        )


def test_explicit_diagnostic_can_preserve_stronger_core_phase_evidence() -> None:
    result = _result(failure=ExecutionFailure.INPUT)
    diagnostic = ExecutionDiagnostic(
        result,
        "vm",
        "test-vm",
        ExecutionPhase.DELIVERY,
        ExecutionFailureReason.INPUT,
    )

    with pytest.raises(CheckedExecutionError) as caught:
        diagnostic.check()

    assert caught.value.details is not None
    assert caught.value.details.phase is ExecutionPhase.DELIVERY
    assert caught.value.details.reason is ExecutionFailureReason.INPUT
