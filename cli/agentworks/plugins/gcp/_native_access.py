"""Private retained GCE power/locator reads and public-original retirement."""

from __future__ import annotations

import json
import re
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.base import ProviderLocator, provider_locator_remaining
from agentworks.db import VMStatus
from agentworks.errors import (
    AgentworksError,
    AuthorizationError,
    ConnectivityError,
    NotFoundError,
    StateError,
    TokenRejectedError,
    ValidationError,
)
from agentworks.plugins.gcp._owned_auth import _OwnedAuth
from agentworks.plugins.gcp.errors import google_error

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp.platform import GCEPlatform

MAX_BODY_BYTES = 65_536
READ_CHUNK_BYTES = 8_192
INSTANCE_FIELDS = "id,kind,name,selfLink,status,zone"


def _uint64(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None and int(value) <= 2**64 - 1


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


class GCEOwnedRead:
    """Caller-retained exact reads, without activation or native preparation.

    Construction is passive. Caller ownership precedes the first observation.
    Stock ADC internals, credential retries and public-close constructor handoff
    remain best-effort limits, not a promise of socket/process drain.
    """

    def __init__(self, vm: VMRow, platform: GCEPlatform, ctx: RunContext) -> None:
        self._vm_name = vm.name
        self._site_name = platform.site_name
        self._identity = platform._locator_metadata(vm)
        if not _uint64(self._identity[3]):
            raise StateError(
                f"VM '{vm.name}' has invalid GCE provider identity metadata",
                entity_kind="vm",
                entity_name=vm.name,
            )
        self._auth_config = platform.config.auth
        self._ctx = ctx
        self._auth = _OwnedAuth(vm.name)
        self._service_session: Any = None
        self._response: Any = None
        self._closed = False
        self._lock = Lock()

    @property
    def cleanup_incomplete(self) -> bool:
        """Active credential dependencies become cleanup debt only on final close."""
        return (
            self._response is not None
            or self._service_session is not None
            or self._auth.cleanup_incomplete
            or (self._closed and self._auth.session_retained)
        )

    def observe_power(self, deadline: Deadline) -> VMStatus:
        _, power = self._read_exact_instance(deadline)
        return power

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        locator, _ = self._read_exact_instance(deadline)
        return locator

    def _remaining(self, deadline: Deadline) -> float:
        return provider_locator_remaining(deadline, vm_name=self._vm_name)

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise StateError(
                "GCE read transition exceeded its deadline",
                entity_kind="vm",
                entity_name=self._vm_name,
            )

    def _url(self) -> str:
        project, zone, name, _ = self._identity
        return f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances/{name}"

    def _retain_response(self, response: Any, **kwargs: Any) -> None:
        """Capture before Requests can consume refused bodies or prepare redirects."""
        if self._response is not None:
            raise StateError("GCE read response custody is already occupied")
        self._response = response
        if type(response.status_code) is not int:
            raise ValidationError("GCE read response is invalid")
        if response.status_code == 404:
            raise NotFoundError(
                f"GCE instance '{self._identity[2]}' no longer exists",
                entity_kind="vm",
                entity_name=self._vm_name,
            )
        if response.status_code == 401:
            raise TokenRejectedError(
                "Google Cloud rejected the selected credential", entity_kind="vm", entity_name=self._vm_name
            )
        if response.status_code == 403:
            raise AuthorizationError(
                "Google Cloud denied the selected instance read", entity_kind="vm", entity_name=self._vm_name
            )
        encodings = [value for key, value in response.headers.items() if key.lower() == "content-encoding"]
        if (
            response.status_code != 200
            or len(encodings) > 1
            or (encodings and (type(encodings[0]) is not str or encodings[0].strip().lower() != "identity"))
        ):
            raise ValidationError("GCE read response is invalid")

    def _read_body(self, deadline: Deadline) -> bytes:
        body = bytearray()
        while True:
            self._remaining(deadline)
            amount = min(READ_CHUNK_BYTES, MAX_BODY_BYTES - len(body) + 1)
            chunk = self._response.raw.read(amount, decode_content=False)
            if type(chunk) is not bytes or len(chunk) > amount:
                raise ValidationError("GCE read body is invalid")
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise ValidationError("GCE read body exceeds its ingestion bound")
            if not chunk or self._response.raw.isclosed():
                self._remaining(deadline)
                return bytes(body)

    def _project(self, body: bytes) -> tuple[ProviderLocator, VMStatus]:
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            project, zone, name, incarnation = self._identity
            zone_path = f"/compute/v1/projects/{project}/zones/{zone}"
            instance_path = f"{zone_path}/instances/{name}"
            hosts = ("https://compute.googleapis.com", "https://www.googleapis.com")
            if (
                type(value) is not dict
                or value.get("kind") != "compute#instance"
                or value.get("name") != name
                or not _uint64(value.get("id"))
                or value["id"] != incarnation
                or value.get("zone") not in tuple(f"{host}{zone_path}" for host in hosts)
                or value.get("selfLink") not in tuple(f"{host}{instance_path}" for host in hosts)
            ):
                raise ValueError("foreign instance")
            status = value.get("status")
            power = (
                {"RUNNING": VMStatus.RUNNING, "TERMINATED": VMStatus.STOPPED, "STOPPED": VMStatus.STOPPED}.get(
                    status, VMStatus.UNKNOWN
                )
                if type(status) is str
                else VMStatus.UNKNOWN
            )
            return ProviderLocator(f"gcp-gce:{project}:{zone}:{incarnation}"), power
        except (ValueError, TypeError, RecursionError):
            raise ValidationError("GCE read instance identity is invalid") from None

    def _read_exact_instance(self, deadline: Deadline) -> tuple[ProviderLocator, VMStatus]:
        self._acquire(deadline)
        control: BaseException | None = None
        admitted = False
        completed = False
        try:
            self._remaining(deadline)
            if self._closed or self.cleanup_incomplete:
                raise StateError("GCE read cannot admit an observation", entity_kind="vm", entity_name=self._vm_name)
            admitted = True
            self._auth.begin(deadline)
            import requests  # type: ignore[import-untyped]

            failure: AgentworksError | None = None
            try:
                self._auth.acquire(self._auth_config, self._ctx, self._site_name)
                headers = self._auth.apply_headers()
                self._service_session = requests.Session()
                self._service_session.trust_env = False
                remaining = self._remaining(deadline)
                self._service_session.request(
                    "GET",
                    self._url(),
                    params={"fields": INSTANCE_FIELDS},
                    stream=True,
                    allow_redirects=False,
                    headers=headers,
                    timeout=(remaining, remaining),
                    hooks={"response": self._retain_response},
                )
                self._remaining(deadline)
                result = self._project(self._read_body(deadline))
            except AgentworksError:
                raise
            except Exception as error:
                if isinstance(error, requests.exceptions.RequestException):
                    failure = ConnectivityError(
                        "Google Cloud instance read failed in transport", entity_kind="vm", entity_name=self._vm_name
                    )
                else:
                    failure = google_error(error, operation="reading the selected instance", resource=self._vm_name)
            if failure is not None:
                raise failure
            self._remaining(deadline)
            completed = True
            return result
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                try:
                    if admitted:
                        self._close_handles(final=False)
                    if completed:
                        self._remaining(deadline)
                except BaseException as error:
                    if control is not None:
                        raise control from None
                    if not isinstance(error, Exception):
                        control = error
                    raise
            finally:
                try:
                    self._auth.end()
                except BaseException as error:
                    if control is not None:
                        raise control from None
                    if not isinstance(error, Exception):
                        control = error
                    raise
                finally:
                    try:
                        self._lock.release()
                    except BaseException:
                        if control is not None:
                            raise control from None
                        raise

    def _close_handles(self, *, final: bool) -> None:
        control: BaseException | None = None
        try:
            for attribute in ("_response", "_service_session"):
                handle = getattr(self, attribute)
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception:
                    pass
                except BaseException as error:
                    if control is None:
                        control = error
                else:
                    setattr(self, attribute, None)
            try:
                self._auth.retire_responses(retry=final)
            except BaseException as error:
                if control is None:
                    control = error
            if final:
                try:
                    self._auth.close_session()
                except BaseException as error:
                    if control is None:
                        control = error
        except BaseException:
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    def close(self, deadline: Deadline) -> bool:
        """Stop admission and retry public closes only on retained originals."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            self._closed = True
            self._remaining(deadline)
            self._close_handles(final=True)
            self._remaining(deadline)
            return not self.cleanup_incomplete
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                self._lock.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise
