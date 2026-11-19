"""The bound on each Azure call a route makes.

A route's Azure calls are the reads and long-running operations of the
public-IP heal, the NSG convergence and the poke that open it, and the
removal that closes it. The route opens before the script's deadline
starts, so the run_as_root contract needs each of them bounded by itself:
every read, and every long-running operation's first request and wait
together, ends within ``_OPERATION_TIMEOUT_SECONDS`` on the wall clock or
raises :class:`_RouteOperationTimeout`. GCP bounds its firewall operations
the same way.

Two layers make that hold. azure-core's per-call options
(:func:`_route_call_options`) keep a stalled call from spending its whole
bound on one silent try. They bound only silence, though: a response whose
body keeps arriving, however slowly, resets ``read_timeout`` with every
chunk, and neither requests nor azure-core bounds a request's whole time.
So :func:`_bounded_route_call` runs each call on a daemon thread and stops
waiting for it at the deadline, whatever Azure sends, a ``Retry-After``
included. A call given up on keeps running, and what it sent may still take
effect, so a caller that creates something removes it.

The removal runs with interrupts deferred, so a Ctrl-C can wait the whole
bound for it.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import TYPE_CHECKING, Any, cast

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

# How often _bounded_route_call's wait wakes. Only the main thread runs
# Python's signal handlers, and a signal delivered to the call's thread instead,
# or one a platform cannot interrupt a blocked wait with, is handled only once
# the main thread wakes, so it wakes this often to keep Ctrl-C prompt.
_ROUTE_CALL_WAKE_SECONDS = 0.25


class _RouteOperationTimeout(AzureError):
    """An Azure call on a route did not finish within ``_OPERATION_TIMEOUT_SECONDS``."""

    def __init__(self, what: str) -> None:
        summary = f"Azure did not finish {what} within {_OPERATION_TIMEOUT_SECONDS:.0f}s"
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
    """``call()``'s result, waiting until ``deadline`` on the monotonic clock, or :class:`_RouteOperationTimeout`.

    ``call`` runs on a daemon thread, in a copy of the caller's context, and
    the wait for it is what ends at ``deadline``: by default
    ``_OPERATION_TIMEOUT_SECONDS`` from now, or for an operation's first
    request the deadline its :func:`_finish_route_operation` wait shares. What
    ``call`` raised is raised here. The wait holds no interrupts, so Ctrl-C
    outside :func:`~agentworks.interrupts.interrupts_deferred` ends it at
    once, or within ``_ROUTE_CALL_WAKE_SECONDS`` when the signal reached
    another thread. A call given up on keeps running (see the module
    docstring).
    """
    if deadline is None:
        deadline = time.monotonic() + _OPERATION_TIMEOUT_SECONDS
    context = contextvars.copy_context()
    outcome: list[tuple[T, None] | tuple[None, BaseException]] = []
    finished = threading.Event()

    def _run() -> None:
        try:
            outcome.append((context.run(call), None))
        except BaseException as exc:  # handed to the waiting caller, which raises it
            outcome.append((None, exc))
        finally:
            finished.set()

    threading.Thread(target=_run, name="azure-route-call", daemon=True).start()
    while not finished.wait(min(_ROUTE_CALL_WAKE_SECONDS, max(0.0, deadline - time.monotonic()))):
        if time.monotonic() >= deadline:
            raise _RouteOperationTimeout(what)
    ((result, error),) = outcome
    if error is not None:
        raise error
    return cast("T", result)


def _start_route_operation(start: Callable[[], Any], what: str) -> tuple[Any, float]:
    """The poller ``start`` returns, from a first request bounded like any call, and the deadline its wait shares."""
    deadline = time.monotonic() + _OPERATION_TIMEOUT_SECONDS
    return _bounded_route_call(start, what, deadline), deadline


def _finish_route_operation(poller: Any, what: str, deadline: float) -> Any:
    """``poller``'s result, waiting until ``deadline`` on the monotonic clock, or :class:`_RouteOperationTimeout`.

    ``deadline`` is the one the operation's first request was given (see
    :func:`_start_route_operation`), so the wait takes what that request
    left. The operation may still finish after the wait ends, so a caller
    that created something removes it.
    """
    result = poller.result(timeout=max(0.0, deadline - time.monotonic()))
    if not poller.done():
        raise _RouteOperationTimeout(what)
    return result


def _run_route_operation(start: Callable[[], Any], what: str) -> Any:
    """The result of the long-running operation ``start`` begins, its first request and wait sharing one bound."""
    poller, deadline = _start_route_operation(start, what)
    return _finish_route_operation(poller, what, deadline)
