"""Public execution-result invariants and success behavior."""

from __future__ import annotations

import traceback
from dataclasses import FrozenInstanceError, dataclass

import pytest

from agentworks.errors import ExternalError, ValidationError
from agentworks.execution.carrier import Dispatch, Retention
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
