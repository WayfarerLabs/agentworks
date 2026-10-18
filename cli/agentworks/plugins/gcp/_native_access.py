"""Private retained GCE instance reads and public-original retirement."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
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
from agentworks.plugins.gcp._native_identity import NativeIdentity, canonical_path
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
NATIVE_INSTANCE_FIELDS = (
    INSTANCE_FIELDS
    + ",networkInterfaces(network,subnetwork,accessConfigs(name,type,natIP)),tags(items),serviceAccounts(email)"
)


@dataclass(frozen=True)
class GCEInstanceObservation:
    locator: ProviderLocator
    raw_power: str | None
    power: VMStatus
    endpoint: str | None
    service_accounts: tuple[str, ...]
    tags: tuple[str, ...]


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
        self._identity = platform._locator_metadata(vm)
        if not _uint64(self._identity[3]):
            raise StateError(
                f"VM '{vm.name}' has invalid GCE provider identity metadata",
                entity_kind="vm",
                entity_name=vm.name,
            )
        self._auth = _OwnedAuth(vm.name, platform.config.auth, ctx, platform.site_name)
        self._service_session: Any = None
        self._response: Any = None
        self._lock = Lock()
        self._read_closed = False

    @property
    def cleanup_incomplete(self) -> bool:
        """Active credential dependencies become cleanup debt only on final close."""
        return self._response is not None or self._service_session is not None or self._auth.cleanup_incomplete

    def observe_power(self, deadline: Deadline) -> VMStatus:
        return self._read_exact_instance(deadline).power

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        return self._read_exact_instance(deadline).locator

    def observe_instance(self, deadline: Deadline, *, identity: NativeIdentity) -> GCEInstanceObservation:
        return self._read_exact_instance(deadline, identity=identity)

    def borrow_auth(self, deadline: Deadline) -> _OwnedAuth:
        """Borrow this reader's selected helper without acquiring credentials."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            self._remaining(deadline)
            self._check_admission()
            return self._auth
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

    def _check_admission(self) -> None:
        if self._read_closed or self._auth.closed or self.cleanup_incomplete:
            raise StateError("GCE read cannot admit an observation", entity_kind="vm", entity_name=self._vm_name)

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
            return self._project_identity(value)
        except (ValueError, TypeError, RecursionError):
            raise ValidationError("GCE read instance identity is invalid") from None

    def _project_identity(self, value: object) -> tuple[ProviderLocator, VMStatus]:
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

    def _project_instance(self, body: bytes, identity: NativeIdentity) -> GCEInstanceObservation:
        """Select bounded provider facts for the frozen native access selection."""
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            locator, power = self._project_identity(value)
            raw_power = value.get("status")
            if raw_power is not None and (
                type(raw_power) is not str or re.fullmatch(r"[A-Z_]{1,64}", raw_power) is None
            ):
                raise ValueError("invalid power")
            interfaces = value.get("networkInterfaces")
            if type(interfaces) is not list or len(interfaces) != 1 or type(interfaces[0]) is not dict:
                raise ValueError("invalid interfaces")
            interface = interfaces[0]
            if (
                canonical_path(interface.get("network"), project_id=identity.project_id, kind="network")
                != identity.network_url
            ):
                raise ValueError("foreign network")
            subnet = (
                canonical_path(interface["subnetwork"], project_id=identity.project_id, kind="subnet")
                if "subnetwork" in interface
                else None
            )
            # An unrecorded subnet leaves selection to the provider, not a promise of absence.
            if identity.subnet_url is not None and subnet != identity.subnet_url:
                raise ValueError("foreign subnet")
            configs = interface.get("accessConfigs")
            if type(configs) is not list or len(configs) != 1 or type(configs[0]) is not dict:
                raise ValueError("invalid access configs")
            config = configs[0]
            if config.get("name") != identity.access_config_name or config.get("type") != "ONE_TO_ONE_NAT":
                raise ValueError("foreign access config")
            endpoint = config.get("natIP")
            if "natIP" in config and (type(endpoint) is not str or str(ipaddress.IPv4Address(endpoint)) != endpoint):
                raise ValueError("invalid endpoint")
            tags = value.get("tags")
            if type(tags) is not dict:
                raise ValueError("invalid tags")
            items = tags.get("items", [])
            if (
                type(items) is not list
                or len(items) > 64
                or any(
                    type(tag) is not str or re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", tag) is None
                    for tag in items
                )
                or len(set(items)) != len(items)
                or identity.network_tag not in items
            ):
                raise ValueError("invalid tags")
            accounts = value.get("serviceAccounts", [])
            if type(accounts) is not list or len(accounts) > 16:
                raise ValueError("invalid service accounts")
            emails: list[str] = []
            for account in accounts:
                if type(account) is not dict:
                    raise ValueError("invalid service account")
                email = account.get("email")
                if (
                    type(email) is not str
                    or len(email) > 320
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", email)
                    is None
                    or ".." in email
                    or email in emails
                ):
                    raise ValueError("invalid service account email")
                emails.append(email)
            return GCEInstanceObservation(locator, raw_power, power, endpoint, tuple(emails), tuple(items))
        except (ValueError, TypeError, RecursionError):
            raise ValidationError("GCE native instance observation is invalid") from None

    def _read_exact_instance(
        self, deadline: Deadline, *, identity: NativeIdentity | None = None
    ) -> GCEInstanceObservation:
        self._acquire(deadline)
        control: BaseException | None = None
        admitted = False
        auth_begun = False
        completed = False
        try:
            self._remaining(deadline)
            self._check_admission()
            if (
                identity is not None
                and (identity.project_id, identity.zone, identity.instance_name, identity.instance_id) != self._identity
            ):
                raise StateError("GCE native identity does not match the selected reader")
            admitted = True
            self._auth.begin(deadline)
            auth_begun = True
            import requests  # type: ignore[import-untyped]

            failure: AgentworksError | None = None
            try:
                self._auth.acquire()
                headers = self._auth.apply_headers("GET", self._url())
                self._service_session = requests.Session()
                self._service_session.trust_env = False
                remaining = self._remaining(deadline)
                self._auth.remaining()
                self._service_session.request(
                    "GET",
                    self._url(),
                    params={"fields": INSTANCE_FIELDS if identity is None else NATIVE_INSTANCE_FIELDS},
                    stream=True,
                    allow_redirects=False,
                    headers=headers,
                    timeout=(remaining, remaining),
                    hooks={"response": self._retain_response},
                )
                self._remaining(deadline)
                body = self._read_body(deadline)
                if identity is None:
                    locator, power = self._project(body)
                    result = GCEInstanceObservation(locator, None, power, None, (), ())
                else:
                    result = self._project_instance(body, identity)
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
                        try:
                            self._close_handles()
                        finally:
                            if auth_begun:
                                self._auth.retire_responses()
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
                    if auth_begun:
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

    def _close_handles(self) -> None:
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
        except BaseException:
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    def close_read_client(self, deadline: Deadline) -> bool:
        """Bar reader admission and retire service originals, leaving auth borrowed."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            self._read_closed = True
            self._remaining(deadline)
            self._close_handles()
            self._remaining(deadline)
            return self._response is None and self._service_session is None
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

    def close(self, deadline: Deadline) -> bool:
        """Stop admission and retry public closes only on retained originals."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            self._read_closed = True
            self._auth.bar_entry()
            self._remaining(deadline)
            try:
                self._close_handles()
            except BaseException as error:
                control = error
            try:
                self._auth.close(deadline)
            except BaseException:
                if control is not None:
                    raise control from None
                raise
            if control is not None:
                raise control
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
