"""The real generated Azure clients over an in-memory ``requests`` adapter.

Tests that pin how azure-core sends a request, and what it records of the
answer, drive a generated management client and azure-core's pipeline and
pollers for real; only the adapter under the ``requests``
session is scripted, so no test reaches Azure.
"""

from __future__ import annotations

import io
import json
import threading
import time
from typing import TYPE_CHECKING

import requests
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport
from requests.adapters import BaseAdapter

from agentworks.plugins.azure import network as azure_network

if TYPE_CHECKING:
    from collections.abc import Callable

# An answer is a status, headers and a JSON body (raw bytes when not JSON), or an error the adapter raises.
_Answer = tuple[int, dict[str, str], object] | Exception


def _refused(status: int) -> _Answer:
    return (status, {}, {"error": {"code": "Refused", "message": f"status {status}"}})


class _Credential:
    def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
        return AccessToken("token", 2**31)


class _ScriptedAdapter(BaseAdapter):
    """Answers each request with the next scripted answer, repeating the last, and records its method."""

    def __init__(self, answers: list[_Answer]) -> None:
        super().__init__()
        self._answers = answers
        self.methods: list[str] = []

    def send(self, request: requests.PreparedRequest, *_args: object, **_kwargs: object) -> requests.Response:
        self.methods.append(str(request.method))
        return self._answer(request)

    def _answer(self, request: requests.PreparedRequest) -> requests.Response:
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, headers, body = answer
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.raw = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        response.request = request
        response.url = str(request.url)
        return response

    def close(self) -> None:
        return None


class _HeldAdapter(_ScriptedAdapter):
    """Holds every request until ``release`` is set, then answers it with a 404.

    ``on_hold`` runs on the sending thread as each request is held, and
    ``held_at`` then records when the first was. ``cap`` ends a hold nobody
    released, so code that waits for the request itself fails its test
    instead of hanging. ``senders`` records each sending thread, for
    :meth:`finish` to join.
    """

    def __init__(self, *, cap: float, on_hold: Callable[[], None] | None = None) -> None:
        super().__init__([_refused(404)])
        self.release = threading.Event()
        self.senders: list[threading.Thread] = []
        self.held_at: float | None = None
        self._cap = cap
        self._on_hold = on_hold

    def send(self, request: requests.PreparedRequest, *_args: object, **_kwargs: object) -> requests.Response:
        self.methods.append(str(request.method))
        self.senders.append(threading.current_thread())
        if self._on_hold is not None:
            self._on_hold()
        if self.held_at is None:
            self.held_at = time.monotonic()
        self.release.wait(self._cap)
        return self._answer(request)

    def finish(self) -> None:
        """Release every held request and wait for the threads that sent them, so none outlives the test."""
        self.release.set()
        for sender in self.senders:
            if sender is not threading.current_thread():
                sender.join(self._cap)


class _HeldClock:
    """Reads 0 until ``adapter`` holds a request, then the real seconds since.

    Patched in for the clock a bound counts on, it starts that bound at the
    hold rather than at the call, so a loaded host that is slow to send the
    request cannot spend the bound first.
    """

    def __init__(self, adapter: _HeldAdapter) -> None:
        self._adapter = adapter

    def monotonic(self) -> float:
        held_at = self._adapter.held_at
        return 0.0 if held_at is None else time.monotonic() - held_at


# azure-core exports RequestsTransport through a lazy module __getattr__, so mypy sees it as Any.
class _ClockedTransport(RequestsTransport):  # type: ignore[misc]
    """Sleeps on the network helpers' clock, the fake one once ``_install_fakes`` has run.

    So a wait azure-core asks for (a retry's backoff or Retry-After, a poll's
    interval) takes no real time there and shows on that clock instead: a
    request retried that should not be fails its test rather than stalling it.
    """

    def sleep(self, duration: float) -> None:
        azure_network.time.sleep(duration)  # type: ignore[attr-defined]  # the module's own import, which the fakes patch


def _scripted_client[C](client_type: Callable[..., C], adapter: BaseAdapter) -> C:
    """A generated management client sending over ``adapter``, sleeping on the network helpers' clock.

    See :class:`_ClockedTransport`.
    """
    session = requests.Session()
    session.mount("https://", adapter)
    return client_type(
        _Credential(),
        "sub-A",
        transport=_ClockedTransport(session=session, session_owner=False),
    )
