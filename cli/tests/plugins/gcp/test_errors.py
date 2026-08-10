"""Provider-shaped GCE extended-operation outcome classification."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.api_core import exceptions as api_exceptions
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


def _provider_503() -> api_exceptions.ServiceUnavailable:
    provider = cast(
        "Callable[[str], api_exceptions.ServiceUnavailable]",
        api_exceptions.ServiceUnavailable,
    )(_SENTINEL)
    provider.__cause__ = RuntimeError(_SENTINEL)
    return provider


def _operation_error(*details: object) -> SimpleNamespace:
    return SimpleNamespace(errors=list(details), message=_SENTINEL, details=_SENTINEL)


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


def test_successful_wait_does_not_probe_error_fields_or_refresh() -> None:
    operation = _ExtendedOperation(status=compute_v1.Operation.Status.DONE, error=None, failure=None)

    wait_for_extended_operation(operation, label="instance vm-a", zone=_ZONE, timeout=29)

    assert operation.result_calls == [29]
    assert operation.done_calls == 0
