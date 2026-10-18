"""Bounded source discovery retires only its own actual HTTP response."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from agentworks.capabilities.vm_platform import ssh_exposure as legacy
from agentworks.errors import ConfigError, ConnectivityError, LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.vms import _ssh_native_exposure as discovery
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure


@dataclass
class Response:
    body: object = b"203.0.113.7\n"
    read_error: BaseException | None = None
    close_error: BaseException | None = None
    before_read: Callable[[], None] = lambda: None
    before_close: Callable[[], None] = lambda: None
    reads: list[int] = field(default_factory=list)
    closes: int = 0

    def read(self, limit: int) -> object:
        self.reads.append(limit)
        self.before_read()
        if self.read_error is not None:
            raise self.read_error
        return self.body

    def close(self) -> None:
        self.closes += 1
        self.before_close()
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    return now


def install(
    monkeypatch: pytest.MonkeyPatch,
    response: Response,
    *,
    error: BaseException | None = None,
    after_open: Callable[[], None] = lambda: None,
) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []

    def open_response(url: str, *, timeout: float) -> Response:
        calls.append((url, timeout))
        after_open()
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(discovery, "urlopen", open_response)
    return calls


def test_passive_constructor_and_terminal_close(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response()
    calls = install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    assert not owner.cleanup_incomplete and not calls
    assert owner.close(Deadline.after(10))
    with pytest.raises(StateError):
        owner.resolve((), Deadline.after(10))
    assert not calls and not response.reads and response.closes == 0


@pytest.mark.parametrize("remaining", [0.25, 3.0, 5.0, 20.0])
def test_timeout_uses_original_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, clock: list[float], remaining: float
) -> None:
    response = Response()
    calls = install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    deadline = Deadline(100 + remaining)
    result = owner.resolve(["198.51.100.3/24", "203.0.113.7", "198.51.100.0/24"], deadline)
    assert result == ("203.0.113.7/32", "198.51.100.0/24")
    assert not owner.cleanup_incomplete
    assert calls == [("https://checkip.amazonaws.com", min(5.0, remaining))]
    assert response.reads == [65] and response.closes == 1
    assert owner.close(deadline) and response.closes == 1
    with pytest.raises(StateError):
        owner.resolve((), deadline)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"", b"x" * 65, b"\xff", b"::1", b"203.0.113.7/32", b"127.1", "203.0.113.7", None])
def test_invalid_external_body_is_retired_without_a_plan(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    response = Response(body=body)
    install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    with pytest.raises(ConnectivityError) as caught:
        owner.resolve((), Deadline.after(10))
    assert caught.value.entity_kind == "vm" and caught.value.entity_name == "box"
    assert caught.value.__cause__ is None
    assert not owner.cleanup_incomplete
    assert response.reads == [65] and response.closes == 1


def test_body_size_limit_accepts_exactly_64_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"203.0.113.7" + b" " * 53
    assert len(body) == 64
    response = Response(body=body)
    install(monkeypatch, response)
    assert OwnedSSHExposure("box").resolve((), Deadline.after(10)) == ("203.0.113.7/32",)


@pytest.mark.parametrize("deadline", [Deadline(None), Deadline(99)])
def test_finite_live_deadline_required_before_io(
    monkeypatch: pytest.MonkeyPatch, clock: list[float], deadline: Deadline
) -> None:
    calls = install(monkeypatch, Response())
    owner = OwnedSSHExposure("box")
    with pytest.raises(ValidationError if deadline.expires_at is None else LimitExceededError):
        owner.resolve(["198.51.100.1"], deadline)
    assert not calls and not owner.cleanup_incomplete


@pytest.mark.parametrize("expiry_stage", ["open", "read", "close"])
def test_late_work_never_publishes_and_cleanup_retries_only_original(
    monkeypatch: pytest.MonkeyPatch, clock: list[float], expiry_stage: str
) -> None:
    def expire() -> None:
        clock[0] = 111

    response = Response()
    if expiry_stage == "read":
        response.before_read = expire
    if expiry_stage == "close":
        response.before_close = expire
    calls = install(monkeypatch, response, after_open=expire if expiry_stage == "open" else lambda: None)
    owner = OwnedSSHExposure("box")
    with pytest.raises(LimitExceededError):
        owner.resolve(["198.51.100.1"], Deadline(110))
    assert owner.cleanup_incomplete == (expiry_stage != "close")
    assert response.reads == ([] if expiry_stage == "open" else [65])
    assert owner.close(Deadline(120))
    assert not owner.cleanup_incomplete and response.closes == 1 and len(calls) == 1


@pytest.mark.parametrize(
    "failure", [URLError("external secret"), ValueError("external secret"), OSError("external secret")]
)
@pytest.mark.parametrize("extras", [False, True])
def test_detection_failure_fallback_requires_explicit_canonical_extras(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, extras: bool
) -> None:
    response = Response(read_error=failure)
    calls = install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    if extras:
        assert owner.resolve(["198.51.100.7/24", "198.51.100.0/24"], Deadline.after(10)) == ("198.51.100.0/24",)
    else:
        with pytest.raises(ConnectivityError) as caught:
            owner.resolve((), Deadline.after(10))
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert response.closes == 1 and len(calls) == 1 and not owner.cleanup_incomplete


def test_open_failure_can_use_extras_without_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Response()
    calls = install(monkeypatch, response, error=OSError("unavailable"))
    owner = OwnedSSHExposure("box")
    assert owner.resolve(["198.51.100.7"], Deadline.after(10)) == ("198.51.100.7/32",)
    assert len(calls) == 1 and not response.reads and response.closes == 0


def test_config_validation_precedes_io(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install(monkeypatch, Response())
    with pytest.raises(ConfigError):
        OwnedSSHExposure("box").resolve(["::1"], Deadline.after(10))
    assert not calls


def test_every_access_detects_fresh_despite_legacy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy, "_egress_ip_cache", "192.0.2.99")
    first, second = Response(), Response(body=b"198.51.100.8")
    first_calls = install(monkeypatch, first)
    assert OwnedSSHExposure("box").resolve((), Deadline.after(10)) == ("203.0.113.7/32",)
    second_calls = install(monkeypatch, second)
    assert OwnedSSHExposure("box").resolve((), Deadline.after(10)) == ("198.51.100.8/32",)
    assert len(first_calls) == len(second_calls) == 1 and first.closes == second.closes == 1


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt, SystemExit])
def test_actual_original_is_retained_before_body_failure(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[BaseException]
) -> None:
    owner = OwnedSSHExposure("box")

    def check_retained() -> None:
        assert owner._response is response and owner.cleanup_incomplete

    failure = failure_type("read failed")
    response = Response(read_error=failure, before_read=check_retained, close_error=OSError("close failed"))
    calls = install(monkeypatch, response)
    with pytest.raises(ConnectivityError if failure_type is OSError else failure_type) as caught:
        owner.resolve((), Deadline.after(10))
    if failure_type is not OSError:
        assert caught.value is failure
    assert owner._response is response and owner.cleanup_incomplete
    assert not owner.close(Deadline.after(10)) and response.closes == 2
    response.close_error = None
    assert owner.close(Deadline.after(10))
    assert response.closes == 3 and response.reads == [65] and len(calls) == 1


@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("body_fails", [False, True])
def test_uncertain_retirement_blocks_success_and_fallback(
    monkeypatch: pytest.MonkeyPatch, close_type: type[BaseException], body_fails: bool
) -> None:
    failure = close_type("close failed")
    response = Response(read_error=OSError("read failed") if body_fails else None, close_error=failure)
    calls = install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    with pytest.raises(StateError if close_type is OSError else close_type) as caught:
        owner.resolve(["198.51.100.1"], Deadline.after(10))
    if close_type is not OSError:
        assert caught.value is failure
    assert owner._response is response and owner.cleanup_incomplete
    response.close_error = None
    assert owner.close(Deadline.after(10))
    with pytest.raises(StateError):
        owner.resolve((), Deadline.after(10))
    assert response.closes == 2 and response.reads == [65] and len(calls) == 1


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("cleanup_type", [OSError, KeyboardInterrupt, SystemExit])
def test_primary_control_survives_cleanup_interruption(
    monkeypatch: pytest.MonkeyPatch, primary_type: type[BaseException], cleanup_type: type[BaseException]
) -> None:
    primary = primary_type("primary")
    response = Response(read_error=primary, close_error=cleanup_type("cleanup"))
    install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    with pytest.raises(primary_type) as caught:
        owner.resolve(["198.51.100.1"], Deadline.after(10))
    assert caught.value is primary and owner._response is response
    response.close_error = None
    assert owner.close(Deadline.after(10))


@pytest.mark.parametrize("failure_type", [StateError, LimitExceededError])
def test_existing_typed_error_survives_ordinary_close_failure(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[Exception]
) -> None:
    primary = failure_type("primary")
    response = Response(read_error=primary, close_error=OSError("cleanup"))
    install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    with pytest.raises(failure_type) as caught:
        owner.resolve(["198.51.100.1"], Deadline.after(10))
    assert caught.value is primary and owner._response is response
    response.close_error = None
    assert owner.close(Deadline.after(10))


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_cleanup_retry_control_retains_original_without_replay(
    monkeypatch: pytest.MonkeyPatch, control_type: type[BaseException]
) -> None:
    response = Response(close_error=OSError("first close"))
    calls = install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    with pytest.raises(StateError):
        owner.resolve((), Deadline.after(10))
    control = control_type("retry close")
    response.close_error = control
    with pytest.raises(control_type) as caught:
        owner.close(Deadline.after(10))
    assert caught.value is control and owner._response is response
    response.close_error = None
    assert owner.close(Deadline.after(10))
    assert response.closes == 3 and response.reads == [65] and len(calls) == 1


@pytest.mark.parametrize("close_fails", [False, True])
def test_http_error_itself_is_retained_and_retired(monkeypatch: pytest.MonkeyPatch, close_fails: bool) -> None:
    response = Response(close_error=OSError("close failed") if close_fails else None)
    error = HTTPError("https://checkip.amazonaws.com", 503, "unavailable", Message(), response)
    close_calls: list[HTTPError] = []
    original_close = error.close

    def close_error_response() -> None:
        close_calls.append(error)
        original_close()

    monkeypatch.setattr(error, "close", close_error_response)
    calls = install(monkeypatch, response, error=error)
    owner = OwnedSSHExposure("box")
    if close_fails:
        with pytest.raises(StateError):
            owner.resolve(["198.51.100.1"], Deadline.after(10))
        assert owner._response is error
        response.close_error = None
        assert owner.close(Deadline.after(10))
    else:
        assert owner.resolve(["198.51.100.1"], Deadline.after(10)) == ("198.51.100.1/32",)
    assert close_calls == [error] * (2 if close_fails else 1)
    assert not response.reads and response.closes == 1 and len(calls) == 1


def test_expired_cleanup_defers_actual_close(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    response = Response(close_error=OSError("close failed"))
    calls = install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    with pytest.raises(StateError):
        owner.resolve((), Deadline(110))
    response.close_error = None
    assert not owner.close(Deadline(99))
    with pytest.raises(ValidationError):
        owner.close(Deadline(None))
    assert owner.cleanup_incomplete and response.closes == 1
    assert owner.close(Deadline(110))
    assert len(calls) == 1 and response.reads == [65] and response.closes == 2


@pytest.mark.parametrize("count", [63, 64, 65])
@pytest.mark.parametrize("detection_fails", [False, True])
def test_combined_prefix_limit(monkeypatch: pytest.MonkeyPatch, count: int, detection_fails: bool) -> None:
    response = Response(read_error=OSError("read failed") if detection_fails else None)
    install(monkeypatch, response)
    owner = OwnedSSHExposure("box")
    extras = [f"198.51.100.{index}" for index in range(count)]
    if count + (not detection_fails) > 64:
        with pytest.raises(LimitExceededError):
            owner.resolve(extras, Deadline.after(10))
    else:
        assert len(owner.resolve(extras, Deadline.after(10))) == count + (not detection_fails)
    assert response.closes == 1 and not owner.cleanup_incomplete
