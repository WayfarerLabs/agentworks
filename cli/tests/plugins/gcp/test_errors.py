"""Provider-shaped GCE extended-operation outcome classification."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.api_core import extended_operation
from google.cloud import compute_v1

from agentworks.plugins.gcp.errors import (
    GCECapacityError,
    GCEIndeterminateOperationError,
    GCEOperationError,
    wait_for_extended_operation,
)

_SENTINEL = "provider-secret-'\"$()"
_ZONE = "us-central1-a"


class _ExtendedOperation:
    """The cached fields and result surface exposed by Google's wrapper."""

    def __init__(self, *, status: Any, error: Any, failure: Exception | None) -> None:
        self.status = status
        self.error = error
        self.failure = failure
        self.result_calls: list[float] = []
        self.done_calls = 0

    def result(self, *, timeout: float) -> None:
        self.result_calls.append(timeout)
        if self.failure is not None:
            raise self.failure

    def done(self) -> bool:
        self.done_calls += 1
        raise AssertionError("classification must not refresh provider state")

    @property
    def error_code(self) -> int:
        raise AssertionError("classification must use only the structured error code")

    @property
    def error_message(self) -> str:
        raise AssertionError("classification must not read provider error text")


def _provider_error(kind: type[Exception]) -> Exception:
    provider = cast("Callable[[str], Exception]", kind)(_SENTINEL)
    provider.__cause__ = RuntimeError(_SENTINEL)
    return provider


def _provider_503() -> Exception:
    return _provider_error(api_exceptions.ServiceUnavailable)


def _operation_error(*details: object) -> SimpleNamespace:
    return SimpleNamespace(errors=list(details), message=_SENTINEL, details=_SENTINEL)


def _real_done_operation(*details: compute_v1.Errors) -> tuple[Any, list[str]]:
    """Build the exact ExtendedOperation shape returned by Compute clients."""
    raw = compute_v1.Operation(
        name="operation-a",
        status=compute_v1.Operation.Status.DONE,
        error=compute_v1.Error(errors=list(details)),
    )
    refresh_calls: list[str] = []

    def refresh(*_args: object, **_kwargs: object) -> compute_v1.Operation:
        refresh_calls.append("refresh")
        return raw

    def cancel() -> None:
        return None

    class _ComputeOperation(extended_operation.ExtendedOperation):
        @property
        def error_message(self) -> str:
            return str(self._extended_operation.http_error_message)

        @property
        def error_code(self) -> int:
            return int(self._extended_operation.http_error_status_code)

    return _ComputeOperation.make(refresh, cancel, raw), refresh_calls  # type: ignore[no-untyped-call]


def _assert_detached_and_safe(error: BaseException) -> None:
    seen: set[int] = set()
    pending: list[object] = [error]
    graph: list[object] = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        graph.append(value)
        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend(item for item in vars(value).values() if item is not None)
            pending.extend(item for item in (value.__cause__, value.__context__) if item is not None)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset):
            pending.extend(value)
    assert _SENTINEL not in "\n".join(f"{value!s}\n{value!r}" for value in graph)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_done_http_503_capacity_is_definitive_typed_safe_and_uses_no_refresh() -> None:
    operation = _ExtendedOperation(
        status=compute_v1.Operation.Status.DONE,
        error=_operation_error(
            SimpleNamespace(code="ZONE_RESOURCE_POOL_EXHAUSTED", message=_SENTINEL, details=_SENTINEL)
        ),
        failure=_provider_503(),
    )

    with pytest.raises(GCECapacityError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=17)

    assert _ZONE in str(caught.value)
    assert caught.value.hint == f"retry later or select another zone instead of '{_ZONE}'"
    assert "ZONE_RESOURCE_POOL_EXHAUSTED" not in str(caught.value)
    assert operation.result_calls == [17]
    assert operation.done_calls == 0
    _assert_detached_and_safe(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        _operation_error(SimpleNamespace(code="UNKNOWN_CODE", message=_SENTINEL)),
        _operation_error(SimpleNamespace(message=_SENTINEL)),
        SimpleNamespace(errors=None, message=_SENTINEL),
        SimpleNamespace(message=_SENTINEL),
        None,
    ],
    ids=("unknown-code", "missing-code", "missing-entries", "missing-shape", "missing-error"),
)
def test_done_unknown_or_malformed_failure_is_definitive_generic_and_safe(error: object) -> None:
    operation = _ExtendedOperation(
        status=compute_v1.Operation.Status.DONE,
        error=error,
        failure=_provider_503(),
    )

    with pytest.raises(GCEOperationError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=19)

    assert type(caught.value) is GCEOperationError
    assert "indeterminate" not in f"{caught.value} {caught.value.hint}".lower()
    assert operation.result_calls == [19]
    assert operation.done_calls == 0
    _assert_detached_and_safe(caught.value)


def test_done_capacity_code_strict_superstring_is_definitive_generic() -> None:
    operation = _ExtendedOperation(
        status=compute_v1.Operation.Status.DONE,
        error=_operation_error(SimpleNamespace(code="PREFIX_ZONE_RESOURCE_POOL_EXHAUSTED", message=_SENTINEL)),
        failure=_provider_503(),
    )

    with pytest.raises(GCEOperationError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=21)

    assert type(caught.value) is GCEOperationError
    assert _ZONE not in str(caught.value)
    _assert_detached_and_safe(caught.value)


def test_global_operation_capacity_guidance_does_not_claim_the_vm_zone() -> None:
    operation = _ExtendedOperation(
        status=compute_v1.Operation.Status.DONE,
        error=_operation_error(SimpleNamespace(code="ZONE_RESOURCE_POOL_EXHAUSTED", message=_SENTINEL)),
        failure=_provider_503(),
    )

    with pytest.raises(GCECapacityError) as caught:
        wait_for_extended_operation(operation, label="firewall rule allow", zone=None, timeout=22)

    assert _ZONE not in f"{caught.value} {caught.value.hint}"
    assert caught.value.hint == "retry later"
    _assert_detached_and_safe(caught.value)


def test_non_done_timeout_is_the_only_indeterminate_operation_outcome() -> None:
    provider = TimeoutError(_SENTINEL)
    provider.__cause__ = RuntimeError(_SENTINEL)
    operation = _ExtendedOperation(status="RUNNING", error=None, failure=provider)

    with pytest.raises(GCEIndeterminateOperationError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=23)

    assert "inspect the named resource before retrying" in (caught.value.hint or "")
    assert operation.result_calls == [23]
    assert operation.done_calls == 0
    _assert_detached_and_safe(caught.value)


@pytest.mark.parametrize(
    "provider",
    [
        _provider_error(api_exceptions.PermissionDenied),
        _provider_error(api_exceptions.ResourceExhausted),
    ],
    ids=("permission-denied", "resource-exhausted"),
)
def test_non_done_provider_poll_failure_is_indeterminate_and_safe(provider: Exception) -> None:
    operation = _ExtendedOperation(status="RUNNING", error=None, failure=provider)

    with pytest.raises(GCEIndeterminateOperationError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=27)

    assert "inspect the named resource before retrying" in (caught.value.hint or "")
    assert operation.result_calls == [27]
    assert operation.done_calls == 0
    _assert_detached_and_safe(caught.value)


def test_normal_return_done_capacity_error_is_still_definitive_and_safe() -> None:
    operation, refresh_calls = _real_done_operation(
        compute_v1.Errors(
            code="ZONE_RESOURCE_POOL_EXHAUSTED",
            message=_SENTINEL,
            location=_SENTINEL,
        )
    )

    with pytest.raises(GCECapacityError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=29)

    assert _ZONE in str(caught.value)
    assert "ZONE_RESOURCE_POOL_EXHAUSTED" not in str(caught.value)
    assert refresh_calls == []
    _assert_detached_and_safe(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        _operation_error(SimpleNamespace(code="UNKNOWN_CODE", message=_SENTINEL)),
        _operation_error(SimpleNamespace(message=_SENTINEL, details=_SENTINEL)),
    ],
    ids=("unknown-code", "malformed-entry"),
)
def test_normal_return_done_unknown_or_malformed_nonempty_error_is_definitive(error: object) -> None:
    operation = _ExtendedOperation(status=compute_v1.Operation.Status.DONE, error=error, failure=None)

    with pytest.raises(GCEOperationError) as caught:
        wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=31)

    assert type(caught.value) is GCEOperationError
    assert operation.result_calls == [31]
    assert operation.done_calls == 0
    _assert_detached_and_safe(caught.value)


@pytest.mark.parametrize("error", [None, _operation_error()], ids=("no-error", "empty-errors"))
def test_true_normal_success_does_not_refresh_or_probe_http_error_fields(error: object) -> None:
    operation = _ExtendedOperation(status=compute_v1.Operation.Status.DONE, error=error, failure=None)

    wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=29)

    assert operation.result_calls == [29]
    assert operation.done_calls == 0
