"""A GCE route's provider calls end within their bounds on the wall clock, and an unconfirmed insert is named.

Opening the route bounds each call on its own; each held removal (the route's, and the provisioning
access's close) shares one bound across its calls.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest
from google.api_core import exceptions as api_exceptions

from agentworks.capabilities.vm_platform import wall_bound
from agentworks.errors import AuthorizationError, ConnectivityError
from agentworks.plugins.gcp import platform as gcp_platform
from agentworks.plugins.gcp.errors import GCEIndeterminateOperationError, wait_for_extended_operation
from tests._gcp_platform_fakes import FakeOperation, api_error, gce_ctx, gce_guest, gce_platform, gce_request
from tests.plugins.gcp.test_platform import _vm

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.db import VMRow
    from agentworks.plugins.gcp.platform import GCEPlatform
    from tests._gcp_platform_fakes import FakeClients
    from tests.conftest import CapturedOutput

# The shrunk bound, and how long a held call waits for release before it goes on anyway, so code that waits
# for the call itself fails its test rather than hanging it.
_BOUND = 0.5
_CAP = 10.0


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> tuple[GCEPlatform, FakeClients, VMRow]:
    """A created VM on the GCE fakes, its bound then shrunk."""
    platform, cache = gce_platform(monkeypatch, gce_guest())
    vm = _vm(platform.create(gce_request(), gce_ctx()).platform_metadata)
    monkeypatch.setattr(gcp_platform, "_OPERATION_TIMEOUT_SECONDS", _BOUND)
    return platform, cache, vm


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Holds a fake client's method (its first ``times`` calls, or every call), then releases and joins at teardown."""
    release = threading.Event()
    senders: list[threading.Thread] = []

    def _hold(client: Any, method: str, *, times: int | None = None, seconds: float | None = None) -> None:
        real = getattr(client, method)
        held = [0]

        def _held(**kwargs: Any) -> Any:
            if times is None or held[0] < times:
                held[0] += 1
                senders.append(threading.current_thread())
                release.wait(_CAP if seconds is None else seconds)
            return real(**kwargs)

        monkeypatch.setattr(client, method, _held)

    yield _hold
    release.set()
    for sender in senders:  # so a call given up on does not outlive the test
        if sender is not threading.current_thread():
            sender.join(_CAP)


def _route_name(cache: FakeClients, vm: VMRow) -> str:
    (route,) = set(cache.firewalls.resources) - {vm.platform_metadata["deny_rule"], vm.platform_metadata["allow_rule"]}
    return route


def test_a_route_removal_whose_delete_outlives_the_bound_warns_at_the_bound(
    created: tuple[GCEPlatform, FakeClients, VMRow], hold: Callable[..., None], captured_output: CapturedOutput
) -> None:
    platform, cache, vm = created
    with platform._transient_route(vm, gce_ctx()):
        route = _route_name(cache, vm)
        hold(cache.firewalls, "delete")
        started = time.monotonic()
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
    assert any(route in warning for warning in captured_output.warnings)


def test_a_route_removal_shares_one_bound_across_its_reads_delete_and_wait(
    created: tuple[GCEPlatform, FakeClients, VMRow], hold: Callable[..., None], captured_output: CapturedOutput
) -> None:
    """Each slow read alone is inside the bound; one bound per call would hold Ctrl-C for several."""
    platform, cache, vm = created
    with platform._transient_route(vm, gce_ctx()):
        hold(cache.firewalls, "get", seconds=0.4)
        hold(cache.firewalls, "delete")
        started = time.monotonic()
    elapsed = time.monotonic() - started

    assert elapsed < 0.9  # one 0.5s bound, where per-call bounds would take 0.4 + 0.5 + 0.4
    assert captured_output.warnings


def test_the_provisioning_access_close_is_bounded(
    created: tuple[GCEPlatform, FakeClients, VMRow], hold: Callable[..., None], captured_output: CapturedOutput
) -> None:
    """The close runs with interrupts held (``close_provisioning_access_or_warn``), so it needs the bound too."""
    platform, cache, vm = created
    hold(cache.firewalls, "delete")

    started = time.monotonic()
    platform.close_provisioning_access(vm, gce_ctx())
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2
    assert any(vm.platform_metadata["allow_rule"] in warning for warning in captured_output.warnings)


@pytest.mark.parametrize(
    ("failure", "warns"),
    [
        (TimeoutError("no answer"), True),
        (KeyboardInterrupt(), True),
        (api_error(api_exceptions.PermissionDenied, "denied"), False),
    ],
    ids=["unanswered", "interrupted", "refused"],
)
def test_an_insert_that_did_not_return_is_named_when_its_removal_finds_nothing(
    created: tuple[GCEPlatform, FakeClients, VMRow],
    captured_output: CapturedOutput,
    failure: BaseException,
    warns: bool,
) -> None:
    """The insert may land after a removal that found nothing, unless Google Cloud refused it outright."""
    platform, cache, vm = created
    cache.firewalls.insert_failures = [failure, failure]  # the insert, and its same-requestId retry
    before = set(cache.firewalls.resources)

    with (
        pytest.raises((ConnectivityError, KeyboardInterrupt, AuthorizationError)),
        platform._transient_route(vm, gce_ctx()),
    ):
        pytest.fail("the body must not run when the insert does not return")

    assert set(cache.firewalls.resources) == before
    if warns:
        route = cache.firewalls.insert_requests[-1].firewall_resource
        (warning,) = captured_output.warnings
        assert route.name in warning
        assert all(prefix in warning for prefix in route.source_ranges)
    else:
        assert captured_output.warnings == []


def test_an_insert_whose_request_outlives_the_bound_is_retried_with_its_request_id(
    monkeypatch: pytest.MonkeyPatch, created: tuple[GCEPlatform, FakeClients, VMRow], hold: Callable[..., None]
) -> None:
    """GCE's documented recovery for an insert whose answer never arrived, here one given up on."""
    platform, cache, vm = created
    hold(cache.firewalls, "insert", times=1)
    sent: list[Any] = []
    held = cache.firewalls.insert

    def _sent(**kwargs: Any) -> Any:
        sent.append(kwargs["request"])
        return held(**kwargs)

    monkeypatch.setattr(cache.firewalls, "insert", _sent)

    with platform._transient_route(vm, gce_ctx()):
        first, retry = sent

    assert first.request_id == retry.request_id
    assert first.firewall_resource.name == retry.firewall_resource.name


def test_an_operation_wait_ends_at_its_timeout_however_slowly_its_status_reads_answer() -> None:
    release = threading.Event()

    class _Stuck(FakeOperation):
        def result(self, *, timeout: float) -> None:
            release.wait(_CAP)

    started = time.monotonic()
    try:
        with pytest.raises(GCEIndeterminateOperationError):
            wait_for_extended_operation(_Stuck("r", "insert", "link", 1), label="rule", zone=None, timeout=0.2)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2


def test_every_call_on_a_route_runs_as_a_bounded_call(
    monkeypatch: pytest.MonkeyPatch, created: tuple[GCEPlatform, FakeClients, VMRow]
) -> None:
    platform, cache, vm = created
    threads: list[tuple[str, str]] = []

    def _record(owner: Any, method: str) -> None:
        real = getattr(owner, method)

        def _recorded(*args: Any, **kwargs: Any) -> Any:
            threads.append((method, threading.current_thread().name))
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, method, _recorded)

    for kind in ("instances", "networks", "firewalls"):
        for method in ("get", "insert", "delete"):
            if hasattr(cache.clients[kind], method):
                _record(cache.clients[kind], method)
    _record(FakeOperation, "result")

    with platform._transient_route(vm, gce_ctx()):
        platform._public_address(vm, gce_ctx())

    assert {method for method, _thread in threads} == {"get", "insert", "delete", "result"}
    assert {thread for _method, thread in threads} == {wall_bound.THREAD_NAME}


def test_a_route_call_that_runs_out_is_a_connectivity_error(
    created: tuple[GCEPlatform, FakeClients, VMRow], hold: Callable[..., None]
) -> None:
    platform, cache, vm = created
    hold(cache.instances, "get")

    with pytest.raises(ConnectivityError):
        platform._public_address(vm, gce_ctx())
