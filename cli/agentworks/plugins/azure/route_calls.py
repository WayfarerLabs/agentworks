"""The bound on each Azure call a route makes.

A route's Azure calls are the reads and long-running operations of the
public-IP heal, the NSG convergence, the poke and the address read that open
it, and the removal that closes it. Each ends within
``_OPERATION_TIMEOUT_SECONDS`` on the wall clock or raises
:class:`_RouteCallTimeout`: a read on its own, and a long-running
operation's first request and wait together. The bound comes from the
shared :mod:`~agentworks.capabilities.vm_platform.wall_bound`, which says
why the SDK's own timeouts cannot give it, and which GCP and AWS use for
their route calls too. azure-core's per-call options
(:func:`_route_call_options`) keep a stalled call from spending its whole
bound on one silent try.

A call given up on may still take effect, its own azure-core retries
included. The poke removes the rule whose create it gave up on, at once,
with no settle (see ``network.poke_ssh_allow``); the heal's and the
convergence's writes are the desired state, so one that lands late does no
harm. The removal runs with interrupts deferred, so a Ctrl-C can wait the
whole bound for it.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.wall_bound import CallTimedOut, call_within
from agentworks.plugins.azure.errors import AzureError

if TYPE_CHECKING:
    from collections.abc import Callable

_OPERATION_TIMEOUT_SECONDS = 300.0

# The per-call bounds on every read a route call sends, and every operation's
# first request. The client's own (300s to read, several retries with backoff)
# would spend the bound on one silent try; with these a try gives up after 10s
# without a connection or 60s without data, and is retried at most twice, so
# three silent tries and azure-core's backoff of a few seconds between them end
# well inside the bound, and a long-running operation's wait takes what
# remains of it.
_ROUTE_CONNECTION_TIMEOUT_SECONDS = 10.0
_ROUTE_READ_TIMEOUT_SECONDS = 60.0
_ROUTE_REQUEST_RETRIES = 2


class _RouteCallTimeout(AzureError):
    """An Azure call on a route did not finish within ``_OPERATION_TIMEOUT_SECONDS``, ``seconds`` here."""

    def __init__(self, what: str) -> None:
        self.seconds = _OPERATION_TIMEOUT_SECONDS
        summary = f"Azure did not finish {what} within {self.seconds:.0f}s"
        super().__init__(summary, detail=summary)


def _route_call_options(*, retries: int = _ROUTE_REQUEST_RETRIES) -> dict[str, Any]:
    """The azure-core per-call options that bound a route call's requests (see ``_ROUTE_REQUEST_RETRIES``)."""
    return {
        "connection_timeout": _ROUTE_CONNECTION_TIMEOUT_SECONDS,
        "read_timeout": _ROUTE_READ_TIMEOUT_SECONDS,
        "retry_total": retries,
    }


def _route_operation_options(*, retries: int = _ROUTE_REQUEST_RETRIES) -> dict[str, Any]:
    """:func:`_route_call_options` for a long-running operation's first request only.

    The generated ``begin_*`` methods pass their per-call options on to the
    status polling they build, so the polling is given explicitly, and its
    status reads keep the client's own timeouts and retries: azure-mgmt-core's
    ``ARMPolling`` with its defaults, which are the ones the client builds
    with (its 30s polling interval among them), short of the endpoint that
    resolves a relative status URL. ARM sends absolute ones, and a status read
    that failed would count as the operation failing, not as its success.
    The interval matches only because ``ARMPolling`` and the clients both
    default to 30s: a ``polling_interval`` passed to the client constructors
    (``AzureVMPlatform._compute_client`` and ``_network_client``) would not
    apply to route operations.
    """
    from azure.mgmt.core.polling.arm_polling import ARMPolling

    return {**_route_call_options(retries=retries), "polling": ARMPolling()}


def _bounded_route_call[T](call: Callable[[], T], what: str, deadline: float | None = None) -> T:
    """``call()``'s result, waiting until ``deadline`` on the monotonic clock, or :class:`_RouteCallTimeout`.

    ``deadline`` defaults to ``_OPERATION_TIMEOUT_SECONDS`` from now; an
    operation's first request and wait share theirs (see
    :func:`_start_route_operation`). The call runs through
    :func:`~agentworks.capabilities.vm_platform.wall_bound.call_within`, so
    Ctrl-C still ends the wait promptly.
    """
    if deadline is None:
        deadline = time.monotonic() + _OPERATION_TIMEOUT_SECONDS
    try:
        return call_within(call, max(0.0, deadline - time.monotonic()))
    except CallTimedOut:
        raise _RouteCallTimeout(what) from None


def _start_route_operation(start: Callable[[], Any], what: str) -> tuple[Any, float]:
    """The poller ``start`` returns, from a first request bounded like any call, and the deadline its wait shares."""
    deadline = time.monotonic() + _OPERATION_TIMEOUT_SECONDS
    return _bounded_route_call(start, what, deadline), deadline


def _finish_route_operation(poller: Any, what: str, deadline: float) -> Any:
    """``poller``'s result, waiting until ``deadline`` on the monotonic clock, or :class:`_RouteCallTimeout`.

    ``deadline`` is the one the operation's first request was given, so the
    wait takes what that request left. The wait is itself a bounded call, so
    it wakes for Ctrl-C as the first request's does.
    """
    result = _bounded_route_call(partial(poller.result, timeout=max(0.0, deadline - time.monotonic())), what, deadline)
    if not poller.done():
        raise _RouteCallTimeout(what)
    return result


def _run_route_operation(start: Callable[[], Any], what: str) -> Any:
    """The result of the long-running operation ``start`` begins, its first request and wait sharing one bound."""
    poller, deadline = _start_route_operation(start, what)
    return _finish_route_operation(poller, what, deadline)
