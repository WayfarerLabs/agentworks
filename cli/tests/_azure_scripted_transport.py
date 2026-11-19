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
from typing import TYPE_CHECKING

import requests
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport
from requests.adapters import BaseAdapter

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

    ``on_hold`` runs on the sending thread as each request is held. ``cap``
    ends a hold nobody released, so code that waits for the request itself
    fails its test instead of hanging. ``senders`` records each sending
    thread, for :meth:`finish` to join.
    """

    def __init__(self, *, cap: float, on_hold: Callable[[], None] | None = None) -> None:
        super().__init__([_refused(404)])
        self.release = threading.Event()
        self.senders: list[threading.Thread] = []
        self._cap = cap
        self._on_hold = on_hold

    def send(self, request: requests.PreparedRequest, *_args: object, **_kwargs: object) -> requests.Response:
        self.methods.append(str(request.method))
        self.senders.append(threading.current_thread())
        if self._on_hold is not None:
            self._on_hold()
        self.release.wait(self._cap)
        return self._answer(request)

    def finish(self) -> None:
        """Release every held request and wait for the threads that sent them, so none outlives the test."""
        self.release.set()
        for sender in self.senders:
            if sender is not threading.current_thread():
                sender.join(self._cap)


def _scripted_client[C](client_type: Callable[..., C], adapter: BaseAdapter) -> C:
    """A generated management client sending over ``adapter``, without azure-core's backoff between retries."""
    session = requests.Session()
    session.mount("https://", adapter)
    return client_type(
        _Credential(),
        "sub-A",
        transport=RequestsTransport(session=session, session_owner=False),
        retry_backoff_factor=0,
    )
