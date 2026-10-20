"""Creation-local Compute REST custody using the configured owned credential."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._native_route_evidence import json_object
from agentworks.plugins.gcp._owned_auth import _OwnedAuth

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.base import RunContext
    from agentworks.plugins.gcp.config import GcpGCEConfig

ROOT = "https://compute.googleapis.com/compute/v1/"
MAX_BODY = 65_536


def remaining(deadline: Deadline) -> float:
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("GCE creation requires a finite deadline")
    seconds = deadline.remaining()
    assert seconds is not None
    if seconds <= 0:
        raise TimeoutError("GCE creation deadline expired")
    return seconds


class CreationAPI:
    """Retain actual responses before ingestion, and actual sessions before use.

    A request is dispatched once. Credential selection/refresh remains owned by
    the existing middleware, including its documented best-effort SDK limits.
    Public close establishes local retirement, never physical socket drain.
    """

    def __init__(self, name: str, config: GcpGCEConfig, ctx: RunContext, site: str) -> None:
        self.auth = _OwnedAuth(name, config.auth, ctx, site)
        self._session: Any = None
        self._response: Any = None

    @property
    def cleanup_incomplete(self) -> bool:
        return self._session is not None or self._response is not None or self.auth.cleanup_incomplete

    def retire(self) -> None:
        control: BaseException | None = None
        for attribute in ("_response", "_session"):
            original = getattr(self, attribute)
            if original is None:
                continue
            try:
                original.close()
            except Exception:
                pass
            except BaseException as error:
                if control is None:
                    control = error
            else:
                setattr(self, attribute, None)
        if control is not None:
            raise control

    def _body(self, deadline: Deadline) -> bytes:
        data = bytearray()
        while True:
            remaining(deadline)
            amount = min(8192, MAX_BODY - len(data) + 1)
            chunk = self._response.raw.read(amount, decode_content=False)
            if type(chunk) is not bytes or len(chunk) > amount:
                raise ValidationError("GCE creation response read is invalid")
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise ValidationError("GCE creation response exceeds its bound")
            # Consume a complete late receipt before testing the budget again.
            if not chunk or self._response.raw.isclosed():
                return bytes(data)

    @contextmanager
    def exchange(
        self,
        method: str,
        path: str,
        deadline: Deadline,
        *,
        body: dict[str, Any] | None = None,
        missing: bool = False,
    ) -> Iterator[dict[str, Any] | None]:
        """Keep original HTTP custody through the consumer's receipt retention."""
        if self.cleanup_incomplete:
            raise StateError("GCE creation retains unretired HTTP or credential work")
        remaining(deadline)
        url = ROOT + path
        self.auth.begin(deadline)
        failure: BaseException | None = None
        try:
            self.auth.acquire()
            headers = self.auth.apply_headers(method, url)
            import requests  # type: ignore[import-untyped]

            self._session = requests.Session()
            self._session.trust_env = False

            def retain(response: Any, **kwargs: Any) -> None:
                self._response = response
                encodings = [v for k, v in response.headers.items() if k.lower() == "content-encoding"]
                if (
                    type(response.status_code) is not int
                    or response.status_code not in ((200, 404) if missing and method == "GET" else (200,))
                    or response.request.method != method
                    or response.request.url != url
                    or response.url != url
                    or len(encodings) > 1
                    or (encodings and (type(encodings[0]) is not str or encodings[0].strip().lower() != "identity"))
                ):
                    raise ValidationError("GCE creation response identity, status or encoding is invalid")

            seconds = remaining(deadline)
            self.auth.remaining()
            try:
                self._session.request(
                    method,
                    url,
                    json=body,
                    headers=headers,
                    stream=True,
                    allow_redirects=False,
                    timeout=(seconds, seconds),
                    hooks={"response": retain},
                )
            except requests.exceptions.RequestException:
                raise ConnectivityError("GCE creation HTTP request failed") from None
            yield None if self._response.status_code == 404 else json_object(self._body(deadline))
            remaining(deadline)
        except BaseException as error:
            failure = error
        for action in (self.retire, self.auth.retire_responses, self.auth.end):
            try:
                action()
            except BaseException as error:
                if failure is None or (isinstance(failure, Exception) and not isinstance(error, Exception)):
                    failure = error
        if failure is not None:
            raise failure from None

    def get(self, path: str, deadline: Deadline, *, missing: bool = False) -> dict[str, Any] | None:
        with self.exchange("GET", path, deadline, missing=missing) as value:
            return value

    def settle(self, deadline: Deadline, *, final: bool) -> bool:
        remaining(deadline)
        self.retire()
        if self.auth.closed:
            self.auth.close(deadline)
            return not self.cleanup_incomplete
        if final:
            self.auth.close(deadline)
            return not self.cleanup_incomplete
        return self.auth.retry_cleanup(deadline) and not self.cleanup_incomplete
