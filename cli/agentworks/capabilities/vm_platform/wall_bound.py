"""Provider calls that end within a bound on the wall clock, however slowly the provider answers.

A cloud's route opens before a root script's deadline starts and closes
with interrupts deferred, so every provider call on it needs a bound of its
own. The cloud SDKs bound only silence: azure-core and google-cloud-compute
hand their timeout to requests, and botocore its ``read_timeout`` to
urllib3, as a limit on each socket read, so a response whose body keeps
arriving, however slowly, resets it with every chunk, and nothing bounds a
request's whole time. :func:`call_within` runs a call on a daemon thread and
stops waiting for it when its bound runs out, raising :class:`CallTimedOut`;
:class:`BoundedClient` does that for every method a client is called with.

A call given up on keeps running on its thread, its SDK's own retries
included, so what it sent may still take effect, or be sent again, after
the caller has moved on. Each platform says what its callers do about that.
"""

from __future__ import annotations

import contextvars
import threading
import time
from functools import partial
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

# How often a bounded wait wakes. Only the main thread runs Python's signal
# handlers, and a signal delivered to the call's thread instead, or one a
# platform cannot interrupt a blocked wait with, is handled only once the main
# thread wakes, so it wakes this often to keep Ctrl-C prompt.
WAKE_SECONDS = 0.25

# The name of the thread each bounded call runs on.
THREAD_NAME = "agw-bounded-call"


class CallTimedOut(TimeoutError):
    """A bounded call, ``what``, did not finish within its bound of ``seconds``."""

    def __init__(self, seconds: float, what: str = "the provider call") -> None:
        super().__init__(f"{what} did not finish within {seconds:.0f}s")
        self.seconds = seconds
        self.what = what


class Deadline:
    """One bound of ``seconds`` on the wall clock, from now, shared by several calls.

    Each call through it gets what the ones before it left, so a sequence of
    calls, such as a removal's reads, delete and wait, ends within the one
    bound however many calls it makes.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._at = time.monotonic() + seconds

    def remaining(self) -> float:
        """The seconds left, or 0 once the bound has run out."""
        return max(0.0, self._at - time.monotonic())


def call_within[T](call: Callable[[], T], bound: float | Deadline, *, what: str = "the provider call") -> T:
    """``call()``'s result, or :class:`CallTimedOut` once ``bound`` runs out on the monotonic clock.

    ``bound`` is the call's own seconds, or a :class:`Deadline` it shares
    with other calls. ``call`` runs on a daemon thread, in a copy of the
    caller's context (so it sees the caller's context variables, as an
    inline call would), and what it raised is raised here. The wait holds no
    interrupts, so Ctrl-C outside
    :func:`~agentworks.interrupts.interrupts_deferred` ends it promptly: at
    once, or within ``WAKE_SECONDS`` when the signal reached another thread.
    A call that finishes as its bound runs out counts as finished.
    """
    stated = bound.seconds if isinstance(bound, Deadline) else bound
    deadline = time.monotonic() + (bound.remaining() if isinstance(bound, Deadline) else bound)
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

    threading.Thread(target=_run, name=THREAD_NAME, daemon=True).start()
    while not finished.wait(min(WAKE_SECONDS, max(0.0, deadline - time.monotonic()))):
        if time.monotonic() >= deadline and not finished.is_set():
            raise CallTimedOut(stated, what)
    ((result, error),) = outcome
    if error is not None:
        raise error
    return cast("T", result)


class BoundedClient:
    """``client``, with each method it is called with run through :func:`call_within` within ``bound``.

    ``bound`` is each call's own seconds, or a :class:`Deadline` they all
    share. A ``BoundedClient`` given another is a new bound on the same
    client. Reading an attribute that is not a method raises ``TypeError``,
    so a nested group of methods cannot be reached unbounded. What a method
    returns is the client's own: an operation, poller, pager, paginator or
    waiter it returns makes further requests unbounded unless its caller
    bounds them too.
    """

    def __init__(self, client: Any, bound: float | Deadline) -> None:
        self._client: Any = client._client if isinstance(client, BoundedClient) else client
        self._bound = bound

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            raise TypeError(f"'{name}' is not a method, and a bounded client bounds only method calls")

        def bounded(*args: Any, **kwargs: Any) -> Any:
            return call_within(partial(attribute, *args, **kwargs), self._bound, what=name)

        return bounded
