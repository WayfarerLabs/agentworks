"""One owned GCE native SSH firewall rule with explicit foreground settlement.

Name-addressed deletion has no atomic numeric-incarnation precondition. Fresh
numeric ID and contents checks establish observations, not provider reservations.
The caller owns the shared credential and the surrounding operation claim.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
from uuid import uuid4

from agentworks.db import VMStatus
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation_operation import MAX_BODY_BYTES, ActivationState
from agentworks.plugins.gcp._native_access import GCEInstanceObservation
from agentworks.plugins.gcp._native_route_evidence import (
    TERMINAL,
    RoutePayload,
    acknowledgment,
    base_url,
    completion,
    decode_payload,
    encode_payload,
    json_object,
    numeric_id,
    rule_url,
)
from agentworks.plugins.gcp._native_route_topology import (
    RULE_FIELDS,
    conflict,
    rule_body,
    validate_baseline,
    validate_network,
    validate_owned,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.plugins.gcp._native_identity import NativeIdentity
    from agentworks.plugins.gcp._owned_auth import _OwnedAuth

OBLIGATION_KIND = "gcp-gce-native-route"
PAYLOAD_VERSION = 1


@contextmanager
def _finalizing(actions: tuple[Callable[[], None], ...]) -> Iterator[None]:
    """Run every cleanup step while preserving the first escaping control."""
    failure: BaseException | None = None
    try:
        yield
    except BaseException as error:
        failure = error
    for action in actions:
        try:
            action()
        except BaseException as error:
            if failure is None or (isinstance(failure, Exception) and not isinstance(error, Exception)):
                failure = error
    if failure is not None:
        raise failure from None


class GCENativeRoute:
    """Passive single-use route selection; terminal close and explicit cleanup.

    Each request borrows the audited public credential middleware. Actual service
    Response and Session originals stay attributable until normal public closure.
    Synchronous SDK budgets do not promise hard elapsed preemption or socket drain.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        auth: _OwnedAuth,
        identity: NativeIdentity,
        prefixes: tuple[str, ...],
    ) -> None:
        if (
            type(owner) is not OperationOwner
            or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm_name
        ):
            raise ValidationError("GCE route requires exact selected VM ownership")
        self._payload = RoutePayload(identity, prefixes, uuid4().hex, str(uuid4()))
        self._durable = encode_payload(self._payload)
        self._revision = 0
        # Only an unresolved publication retains its before/after bytes.
        self._publication: tuple[bytes, int, bytes] | None = None
        self._owner, self._auth = owner, auth
        self._obligation_id = uuid4().hex
        self._obligation: LifecycleObligation | None = None
        self._attempted = self._closed = self._settled = False
        self._register_began = self._mark_began = self._resolution_began = False
        # This originating controller knows whether request dispatch was entered.
        # Persisted possible effect never proves that a recovered request was unsent.
        self._create_entered = self._delete_entered = False
        self._response: Any = None
        self._service_session: Any = None
        self._lock = Lock()

    @property
    def payload(self) -> RoutePayload:
        return self._payload

    @property
    def obligation_id(self) -> str:
        return self._obligation_id

    @property
    def cleanup_incomplete(self) -> bool:
        return self._response is not None or self._service_session is not None

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("GCE route requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise LimitExceededError("GCE route deadline expired")
        return remaining

    @contextmanager
    def _transition(self, deadline: Deadline) -> Iterator[None]:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise LimitExceededError("GCE route transition deadline expired")
        with _finalizing((self._lock.release,)):
            self._remaining(deadline)
            yield

    def _close_handles(self) -> None:
        control: BaseException | None = None
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
        if control is not None:
            raise control

    def _body(self, deadline: Deadline) -> bytes:
        body = bytearray()
        while True:
            self._remaining(deadline)
            amount = min(8192, MAX_BODY_BYTES - len(body) + 1)
            chunk = self._response.raw.read(amount, decode_content=False)
            if type(chunk) is not bytes or len(chunk) > amount:
                raise ValidationError("GCE route raw body read is invalid")
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise ValidationError("GCE route raw body exceeds its ingestion bound")
            # A complete late receipt must be consumed before late-budget refusal.
            if not chunk or self._response.raw.isclosed():
                return bytes(body)

    @contextmanager
    def _exchange(
        self,
        method: str,
        url: str,
        deadline: Deadline,
        *,
        missing: bool = False,
        body: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any] | None]:
        if self.cleanup_incomplete:
            raise StateError("GCE route retains unretired service originals")
        self._auth.begin(deadline)
        with _finalizing((self._close_handles, self._auth.retire_responses, self._auth.end)):
            self._auth.acquire()
            headers = self._auth.apply_headers(method, url)
            import requests  # type: ignore[import-untyped]

            self._service_session = requests.Session()
            self._service_session.trust_env = False

            def retain(response: Any, **kwargs: Any) -> None:
                self._response = response
                encodings = [v for k, v in response.headers.items() if k.lower() == "content-encoding"]
                if (
                    type(response.status_code) is not int
                    or response.status_code not in ((200, 404) if missing and method == "GET" else (200,))
                    or len(encodings) > 1
                    or (encodings and (type(encodings[0]) is not str or encodings[0].strip().lower() != "identity"))
                    or response.request.method != method
                    or response.request.url != url
                    or response.url != url
                ):
                    raise ValidationError("GCE route response identity or encoding is invalid")

            remaining = self._remaining(deadline)
            self._auth.remaining()
            if method == "POST":
                assert self._obligation is not None
                self._mark_began = True
                self._obligation.mark_possible_effect()
                self._remaining(deadline)
                self._create_entered = True
            elif method == "DELETE":
                self._reconcile(deadline)
                self._remaining(deadline)
                self._delete_entered = True
            self._service_session.request(
                method,
                url,
                json=body,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=(remaining, remaining),
                hooks={"response": retain},
            )
            value = None if self._response.status_code == 404 else json_object(self._body(deadline))
            # The consumer retains receipts while these actual originals remain owned.
            yield value
            self._remaining(deadline)

    def _get(self, url: str, deadline: Deadline, *, missing: bool = False) -> dict[str, Any] | None:
        with self._exchange("GET", url, deadline, missing=missing) as value:
            return value

    def _reconcile(self, deadline: Deadline) -> None:
        """Repair only an exact selected row/revision under the owner's fence."""
        self._remaining(deadline)
        if not self._register_began or self._settled:
            return
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            if self._create_entered or self._mark_began:
                raise StateError("GCE route registration remains uncertain")
            return
        if row.obligation_kind != OBLIGATION_KIND or row.payload_version != PAYLOAD_VERSION:
            raise StateError("GCE route custody conflicts")
        decode_payload(row.payload)
        if self._publication is None:
            if (row.payload, row.payload_revision) != (self._durable, self._revision):
                raise StateError("GCE route custody conflicts")
        else:
            before, revision, after = self._publication
            if (row.payload, row.payload_revision) not in ((before, revision), (after, revision + 1)):
                raise StateError("GCE route publication conflicts")
        self._obligation = LifecycleObligation(self._owner, row)
        if row.state is LifecycleObligationState.RESOLVED:
            if not self._resolution_began or self._publication is not None:
                raise StateError("GCE route custody was prematurely resolved")
            self._settled = True
            return
        if self._create_entered and row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("GCE route possible effect disappeared")
        if self._publication is not None:
            before, revision, after = self._publication
            if row.payload == before and row.payload_revision == revision:
                if row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
                    raise StateError("GCE route publication requires possible effect")
                self._remaining(deadline)
                self._obligation.publish_payload(
                    expected_revision=revision,
                    payload_version=PAYLOAD_VERSION,
                    payload=after,
                )
            self._durable, self._revision = after, revision + 1
            self._publication = None

    def _publish(self, deadline: Deadline) -> None:
        if self._publication is not None:
            raise StateError("GCE route has unresolved publication evidence")
        after = encode_payload(self._payload)
        if after != self._durable:
            self._publication = self._durable, self._revision, after
        self._reconcile(deadline)

    def _receipt(self, value: dict[str, Any], deadline: Deadline, *, deleting: bool) -> None:
        name, target = acknowledgment(value, self._payload, deleting=deleting)
        self._payload = (
            replace(self._payload, delete_operation=name, delete_state=ActivationState.PENDING)
            if deleting
            else replace(
                self._payload,
                create_operation=name,
                rule_id=target,
                create_state=ActivationState.PENDING,
            )
        )
        try:
            state = completion(value, self._payload, deleting=deleting)
            self._payload = (
                replace(self._payload, delete_state=state) if deleting else replace(self._payload, create_state=state)
            )
        finally:
            # Matching identity survives malformed status and late or lost publication.
            self._publish(deadline)

    def _poll(self, deadline: Deadline, *, deleting: bool = False) -> None:
        while (self._payload.delete_state if deleting else self._payload.create_state) not in TERMINAL:
            operation = self._payload.delete_operation if deleting else self._payload.create_operation
            if operation is None:
                raise StateError("GCE route has no acknowledged operation")
            self._reconcile(deadline)
            with self._exchange("GET", f"{base_url(self._payload)}/operations/{operation}", deadline) as value:
                assert value is not None
                state = completion(value, self._payload, deleting=deleting)
                self._payload = (
                    replace(self._payload, delete_state=state)
                    if deleting
                    else replace(self._payload, create_state=state)
                )
                self._publish(deadline)
            if state not in TERMINAL:
                time.sleep(min(0.05, self._remaining(deadline)))

    def _instance(self, observed: GCEInstanceObservation) -> None:
        if (
            type(observed) is not GCEInstanceObservation
            or observed.locator != self._payload.identity.locator
            or observed.raw_power != "RUNNING"
            or observed.power is not VMStatus.RUNNING
            or self._payload.identity.network_tag not in observed.tags
        ):
            raise StateError("GCE route requires the selected running instance and tag")

    def _policy(self, deadline: Deadline, observed: GCEInstanceObservation) -> None:
        identity = self._payload.identity
        network = self._get(
            f"https://compute.googleapis.com/compute/v1/{identity.network_url}?"
            + urlencode({"fields": "kind,name,selfLink,networkFirewallPolicyEnforcementOrder"}),
            deadline,
        )
        assert network is not None
        validate_network(network, identity)
        baseline = self._get(
            f"{base_url(self._payload)}/firewalls/{identity.deny_rule}?" + urlencode({"fields": RULE_FIELDS}),
            deadline,
        )
        assert baseline is not None
        validate_baseline(baseline, identity)
        tokens: set[str] = set()
        token: str | None = None
        count = 0
        names: set[str] = set()
        rule_ids: set[str] = set()
        for _page in range(64):
            query = {"maxResults": "100", "fields": f"items({RULE_FIELDS}),nextPageToken,warning(code)"}
            if token is not None:
                query["pageToken"] = token
            page = self._get(f"{base_url(self._payload)}/firewalls?{urlencode(query)}", deadline)
            assert page is not None
            items = page.get("items", [])
            if "warning" in page or type(items) is not list or len(items) > 100:
                raise ValidationError("GCE firewall inventory is incomplete")
            count += len(items)
            if count > 4096:
                raise ValidationError("GCE firewall inventory exceeds its bound")
            for item in items:
                if type(item) is not dict:
                    raise ValidationError("GCE firewall inventory is malformed")
                name = item.get("name")
                if (
                    item.get("kind") != "compute#firewall"
                    or type(name) is not str
                    or re.fullmatch(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", name) is None
                    or name in names
                ):
                    raise ValidationError("GCE firewall inventory identity is invalid")
                rule_id = numeric_id(item.get("id"))
                if rule_id in rule_ids:
                    raise ValidationError("GCE firewall inventory has duplicate identities")
                rule_ids.add(rule_id)
                names.add(name)
                if item.get("name") == self._payload.name:
                    validate_owned(item, self._payload)
                elif item.get("name") == identity.deny_rule:
                    validate_baseline(item, identity)
                else:
                    conflict(item, self._payload, observed.tags, observed.service_accounts)
            token = page.get("nextPageToken")
            if token is None or token == "":
                return
            try:
                valid = type(token) is str and len(token.encode("utf-8")) <= 4096 and token not in tokens
            except UnicodeEncodeError:
                valid = False
            if not valid:
                raise ValidationError("GCE firewall inventory token is invalid")
            tokens.add(token)
        raise ValidationError("GCE firewall inventory pagination exceeds its bound")

    def _rule(self, deadline: Deadline, *, missing: bool = False) -> bool:
        value = self._get(rule_url(self._payload) + "?" + urlencode({"fields": RULE_FIELDS}), deadline, missing=missing)
        if value is None:
            return False
        validate_owned(value, self._payload)
        return True

    def open(self, deadline: Deadline, *, observed_instance: GCEInstanceObservation) -> None:
        """Validate policy, durably fence one insert and prove exact realization."""
        with self._transition(deadline):
            if self._closed or self._attempted or self.cleanup_incomplete:
                raise StateError("GCE route opening is single use")
            self._attempted = True
            self._instance(observed_instance)
            self._policy(deadline, observed_instance)
            if (
                self._get(rule_url(self._payload) + "?" + urlencode({"fields": RULE_FIELDS}), deadline, missing=True)
                is not None
            ):
                raise StateError("GCE route nonce name already exists")
            self._register_began = True
            self._obligation = self._owner.register_lifecycle_obligation(
                OBLIGATION_KIND,
                payload_version=PAYLOAD_VERSION,
                payload=self._durable,
                obligation_id=self._obligation_id,
            )
            with self._exchange(
                "POST",
                f"{base_url(self._payload)}/firewalls?requestId={self._payload.create_request_id}",
                deadline,
                body=rule_body(self._payload),
            ) as value:
                assert value is not None
                self._receipt(value, deadline, deleting=False)
            self._poll(deadline)
            if self._payload.create_state is not ActivationState.SUCCEEDED:
                raise StateError("GCE route insertion did not succeed")
            self._rule(deadline)

    def check(self, deadline: Deadline, *, observed_instance: GCEInstanceObservation) -> None:
        with self._transition(deadline):
            if self._closed or self.cleanup_incomplete or self._payload.create_state is not ActivationState.SUCCEEDED:
                raise StateError("GCE route cannot admit a check")
            self._instance(observed_instance)
            self._reconcile(deadline)
            self._policy(deadline, observed_instance)
            self._rule(deadline)

    def close(self, deadline: Deadline) -> bool:
        """Terminally bar body admission and retry only original local closes."""
        self._closed = True
        with self._transition(deadline):
            self._close_handles()
            self._remaining(deadline)
            return not self.cleanup_incomplete

    def _resolve(self, deadline: Deadline) -> bool:
        self._reconcile(deadline)
        if self._settled:
            return True
        if self.cleanup_incomplete:
            return False
        self._remaining(deadline)
        self._resolution_began = True
        if self._obligation is not None:
            self._obligation.resolve()
        self._settled = True
        return True

    def settle(self, deadline: Deadline) -> bool:
        """Reconcile and remove only this acknowledged rule, never replay uncertainty."""
        self._closed = True
        with self._transition(deadline):
            if self._settled:
                return True
            retry_delete = self._delete_entered and self._payload.delete_state is ActivationState.FAILED
            self._close_handles()
            self._reconcile(deadline)
            if self._settled:
                return True
            if self.cleanup_incomplete:
                return False
            if not self._create_entered:
                return self._resolve(deadline)
            if self._payload.create_operation is None:
                return False
            self._poll(deadline)
            if self._delete_entered:
                if self._payload.delete_operation is None:
                    return False
                self._poll(deadline, deleting=True)
                if self._payload.delete_state is ActivationState.FAILED and not retry_delete:
                    return False
            if not self._rule(deadline, missing=True):
                self._payload = replace(self._payload, absent=True)
                self._publish(deadline)
                return self._resolve(deadline)
            if self._delete_entered and not retry_delete:
                return False
            self._payload = replace(
                self._payload,
                delete_request_id=str(uuid4()),
                delete_operation=None,
                delete_state=ActivationState.UNKNOWN,
            )
            self._delete_entered = False
            self._publish(deadline)
            with self._exchange(
                "DELETE",
                rule_url(self._payload) + f"?requestId={self._payload.delete_request_id}",
                deadline,
            ) as value:
                assert value is not None
                self._receipt(value, deadline, deleting=True)
            self._poll(deadline, deleting=True)
            if self._payload.delete_state is ActivationState.FAILED:
                return False
            if self._rule(deadline, missing=True):
                return False
            self._payload = replace(self._payload, absent=True)
            self._publish(deadline)
            return self._resolve(deadline)
