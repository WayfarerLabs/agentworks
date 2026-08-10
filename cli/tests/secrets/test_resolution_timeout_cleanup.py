"""Resolution timeout boundaries and cleanup containment."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, ClassVar, cast

import pytest

from agentworks import output
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.errors import StateError
from agentworks.schema import AgwModel
from agentworks.secrets import SecretDecl
from agentworks.secrets.outcomes import ResolutionDetail
from agentworks.secrets.resolve import (
    CompletionPolicy,
    _SourceContextDriver,
    resolve_batch,
)
from tests.secrets.test_resolution_lifecycle import _Backend, _Client, _policy, _source


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TimedClient(_Client):
    phase: ClassVar[str] = ""
    clock: ClassVar[_Clock]

    def prepare(self, requests: tuple[SecretLookupRequest, ...], *, remaining_time: RemainingTime) -> None:
        self.events.append("prepare")
        if self.phase == "prepare":
            self.clock.now = 2.0

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> dict[str, str]:
        self.events.append("resolve")
        if self.phase == "resolve":
            self.clock.now = 2.0
        return {request.name: "discarded" for request in requests}


class _TimedContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> SecretSourceClient:
        self.events.append("enter")
        if _TimedClient.phase == "enter":
            _TimedClient.clock.now = 2.0
        return _TimedClient(self.events, {}, None)

    def __exit__(self, *args: object) -> None:
        self.events.append("exit")


class _TimedBackend(_Backend):
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None

    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float:
        return 1.0

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.events.append("factory")
        if _TimedClient.phase == "factory":
            _TimedClient.clock.now = 2.0
        return _TimedContext(cls.events)


class _InvalidTimeoutBackend(_Backend):
    timeout_value: ClassVar[object] = None

    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float | None:
        return cast("float | None", cls.timeout_value)


@pytest.mark.parametrize("timeout", [True, False, "1", object(), float("nan"), float("inf"), 0, -1])
def test_invalid_external_operation_timeout_is_framework_state_error_before_factory(timeout: object) -> None:
    _InvalidTimeoutBackend.events = []
    _InvalidTimeoutBackend.timeout_value = timeout
    with pytest.raises(StateError, match="invalid external-operation timeout"):
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source(backend_class=_InvalidTimeoutBackend)],
            policy=_policy(completion=CompletionPolicy.PARTIAL),
            interaction_broker=None,
        )
    assert _InvalidTimeoutBackend.events == []


@pytest.mark.parametrize(
    ("phase", "events"),
    [
        ("factory", ["factory"]),
        ("enter", ["factory", "enter", "exit"]),
        ("prepare", ["factory", "enter", "prepare", "exit"]),
        ("resolve", ["factory", "enter", "prepare", "resolve", "exit"]),
    ],
)
def test_timeout_at_each_external_boundary_stops_later_work(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    events: list[str],
) -> None:
    clock = _Clock()
    _TimedBackend.events = []
    _TimedClient.phase = phase
    _TimedClient.clock = clock
    monkeypatch.setattr("agentworks.secrets.resolve.time.monotonic", clock)
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(backend_class=_TimedBackend)],
        policy=_policy(completion=CompletionPolicy.PARTIAL),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is ResolutionDetail.DEADLINE_EXCEEDED
    assert _TimedBackend.events == events


class _ExitContext(AbstractContextManager[object]):
    def __init__(self, *, result: object = False, failure: BaseException | None = None) -> None:
        self.result = result
        self.failure = failure
        self.exc_info: tuple[object, object, object] | None = None

    def __enter__(self) -> object:
        return object()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        exc_info = (exc_type, exc, traceback)
        self.exc_info = exc_info
        if self.failure is not None:
            raise self.failure
        return bool(self.result)


def test_cleanup_receives_exact_exc_info_and_never_suppresses(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)
    inner = _ExitContext(result=True)
    driver = _SourceContextDriver(inner, source_name="fixture-source", remaining_time=lambda: None)
    with pytest.raises(KeyboardInterrupt) as caught, driver:
        raise KeyboardInterrupt
    assert inner.exc_info is not None
    assert inner.exc_info[0] is KeyboardInterrupt
    assert inner.exc_info[1] is caught.value
    assert inner.exc_info[2] is caught.value.__traceback__
    assert warnings == ["secret source 'fixture-source': cleanup failed; primary result unchanged"]


@pytest.mark.parametrize(
    ("start", "finish", "result", "failure", "warns"),
    [
        (1.0, 0.0, False, None, True),
        (0.0, 0.0, False, None, False),
        (1.0, 1.0, True, None, True),
        (0.0, 0.0, False, RuntimeError("sentinel-cleanup"), True),
    ],
)
def test_cleanup_warning_matrix_is_non_masking(
    monkeypatch: pytest.MonkeyPatch,
    start: float,
    finish: float,
    result: object,
    failure: BaseException | None,
    warns: bool,
) -> None:
    samples = iter((start, finish))
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)
    inner = _ExitContext(result=result, failure=failure)
    driver = _SourceContextDriver(inner, source_name="fixture-source", remaining_time=lambda: next(samples))
    with driver:
        pass
    assert bool(warnings) is warns
    assert "sentinel-cleanup" not in repr(warnings)


def test_cleanup_warning_sink_failure_cannot_mask() -> None:
    inner = _ExitContext(result=True)
    driver = _SourceContextDriver(inner, source_name="fixture-source", remaining_time=lambda: None)
    original = output.warn
    output.warn = cast("Any", lambda message: (_ for _ in ()).throw(RuntimeError("sink")))
    try:
        with driver:
            pass
    finally:
        output.warn = original
