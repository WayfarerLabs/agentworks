"""Selected GCP credentials and attributable public Request originals."""

from __future__ import annotations

import json
from copy import deepcopy
from threading import TIMEOUT_MAX, Lock, current_thread
from typing import TYPE_CHECKING, Any, cast

from google.auth.transport.requests import Request

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.errors import AgentworksError, LimitExceededError, ProvisioningError, StateError, ValidationError
from agentworks.plugins.gcp.config import GcpAmbientAuth, GcpServiceAccountAuth

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp.config import GcpAuth

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_MAX_CREDENTIAL_RESPONSES = 31
_MAX_RETAINED_COPIES = 32


class _OwnedRequest(Request):
    """Real public Request with root binding or a copied original deadline.

    Keep the public session and certificate arguments available to credentials.
    SDK-created requests, discovery processes and constructor handoff are not
    retroactively owned by this supplied transport.
    """

    def __init__(self, owner: _OwnedAuth, deadline: Deadline | None = None) -> None:
        self._owner = owner
        self._deadline = deadline
        self.session: Any = None
        self._response_hook = self._retain_response
        self._responses: list[Any] = []
        self._uncertain = False
        self._completion_unconfirmed = False
        self._barred = False
        self._worker: Any = None
        self._active = False

    def attach(self, session: Any) -> None:
        # Retain the returned original before the public wrapper initializer.
        self.session = session
        initialize = cast("Callable[..., None]", super().__init__)
        initialize(session=session)

    def __deepcopy__(self, memo: dict[int, Any]) -> _OwnedRequest:
        self._owner.remaining()
        clone = _OwnedRequest(self._owner, self._owner._deadline)
        with self._owner._entry:
            if self._owner.closed or len(self._owner._copies) >= _MAX_RETAINED_COPIES:
                self._owner._failed = True
                self._owner._copy_failed = True
                raise self._owner.refusal()
            self._owner._copies.append(clone)
        memo[id(self)] = clone
        # This exact installed bound method must not traverse the owner/locks.
        memo[id(self._response_hook)] = clone._response_hook
        try:
            clone.session = deepcopy(self.session, memo)
            clone.attach(clone.session)
        except BaseException:
            clone._barred = True
            self._owner._failed = True
            self._owner._copy_failed = True
            raise
        return clone

    def remaining(self) -> float:
        if self._deadline is None:
            return self._owner.remaining()
        return provider_locator_remaining(self._deadline, vm_name=self._owner._vm_name)

    def _retain_response(self, response: Any, **kwargs: Any) -> None:
        self._responses.append(response)
        if len(self._responses) > _MAX_CREDENTIAL_RESPONSES:
            raise self._owner.refusal()

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        timeout: Any = 120,
        **kwargs: Any,
    ) -> Any:
        with self._owner._entry:
            if self._owner.closed or self._barred or self._active or self._uncertain or self._completion_unconfirmed:
                raise self._owner.refusal()
            self.remaining()
            if self._deadline is None and self._owner._caller is not current_thread():
                raise self._owner.refusal()
            if self._deadline is not None:
                worker = current_thread()
                if self._worker is not None and self._worker is not worker:
                    raise self._owner.refusal()
                # Observe the actual consumer before Session or prior Responses.
                self._worker = worker
            self._active = True
        try:
            self.retire_responses()
            if self._uncertain:
                raise self._owner.refusal()
            remaining = self.remaining()
            if timeout is None:
                bounded: Any = remaining
            elif isinstance(timeout, tuple) and len(timeout) == 2:
                bounded = tuple(self._cap(value, remaining) for value in timeout)
            else:
                bounded = self._cap(timeout, remaining)
            invoke = cast("Callable[..., Any]", super().__call__)
            response = invoke(url, method=method, body=body, headers=headers, timeout=bounded, **kwargs)
            self.remaining()
            return response
        finally:
            with self._owner._entry:
                self._active = False

    def retire_responses(self, *, retry: bool = False) -> None:
        """Close only after this lane's SDK consumer advanced or finished."""
        if self._completion_unconfirmed or (self._uncertain and not retry):
            return
        control: BaseException | None = None
        try:
            for response in tuple(self._responses):
                try:
                    response.close()
                except Exception:
                    self._uncertain = True
                except BaseException as error:
                    self._uncertain = True
                    if control is None:
                        control = error
                else:
                    self._responses.remove(response)
        except BaseException:
            self._uncertain = True
            if control is not None:
                raise control from None
            raise
        self._uncertain = bool(self._responses)
        if control is not None:
            raise control

    def close_session(self) -> None:
        if not self._completion_unconfirmed and self.session is not None:
            try:
                self.session.close()
            except Exception:
                self._uncertain = True
            else:
                self.session = None

    @staticmethod
    def _cap(value: Any, remaining: float) -> float:
        if value is None:
            return remaining
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < float("inf"):
            raise ValidationError("GCP credential request timeout is invalid")
        return min(float(value), remaining)


class _OwnedAuth:
    """Shared selected credentials, root/copy custody and terminal admission.

    Active copied consumers are dependencies, not failed cleanup. Borrowers use
    one operation lock; the retained access owner alone calls terminal close.
    """

    def __init__(self, vm_name: str, auth: GcpAuth, ctx: RunContext, site_name: str) -> None:
        self._vm_name = vm_name
        self._auth = auth
        self._ctx = ctx
        self._site_name = site_name
        self._deadline: Deadline | None = None
        self._credential: Any = None
        self._request: _OwnedRequest | None = None
        self._copies: list[_OwnedRequest] = []
        self._entry = Lock()
        self._operation = Lock()
        self._caller: Any = None
        self.closed = False
        self._failed = False
        self._copy_failed = False

    @property
    def cleanup_incomplete(self) -> bool:
        lanes = ([self._request] if self._request is not None else []) + self._copies
        return any(
            lane._completion_unconfirmed
            or lane._uncertain
            or (self.closed and (lane.session is not None or lane._responses))
            for lane in lanes
        )

    def refusal(self) -> StateError:
        return StateError(
            "Selected GCP credential work cannot be admitted",
            entity_kind="vm",
            entity_name=self._vm_name,
        )

    def begin(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        if self._caller is current_thread():
            raise self.refusal()
        if not self._operation.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            raise LimitExceededError("GCP authentication transition exceeded its deadline")
        try:
            if self.closed or self._failed or self.cleanup_incomplete:
                raise self.refusal()
            self._deadline = deadline
            self._caller = current_thread()
            self.retire_copies(deadline)
            if self.cleanup_incomplete or len(self._copies) >= _MAX_RETAINED_COPIES:
                raise self.refusal()
        except BaseException as error:
            self._deadline = None
            self._caller = None
            try:
                self._operation.release()
            except BaseException:
                raise error from None
            raise

    def end(self) -> None:
        self._deadline = None
        self._caller = None
        self._operation.release()

    def remaining(self) -> float:
        if self._deadline is None or self._copy_failed or self.cleanup_incomplete:
            raise self.refusal()
        return provider_locator_remaining(self._deadline, vm_name=self._vm_name)

    def acquire(self) -> None:
        self.remaining()
        if self._credential is not None:
            return
        # A failed partial acquisition cannot silently construct a second original.
        self._failed = True
        import requests  # type: ignore[import-untyped]

        self._request = _OwnedRequest(self)
        self._request.session = requests.Session()
        self._request.attach(self._request.session)
        self._request.session.trust_env = False
        self._request.session.hooks["response"].append(self._request._response_hook)
        self.remaining()
        if isinstance(self._auth, GcpAmbientAuth):
            import google.auth

            failure: ProvisioningError | None = None
            try:
                self._credential, _detected_project = google.auth.default(
                    scopes=(_CLOUD_PLATFORM_SCOPE,), request=self._request
                )
            except AgentworksError:
                raise
            except Exception:
                failure = ProvisioningError(
                    f"could not construct Application Default Credentials for vm-site '{self._site_name}'",
                    entity_kind="vm-site",
                    entity_name=self._site_name,
                    hint="configure Application Default Credentials or select auth.mode service-account",
                )
            if failure is not None:
                raise failure
            if self._credential is None:
                raise self.refusal()
        else:
            self._credential = _service_account(self._auth, self._ctx.secret(self._auth.secret), self._site_name)
        if self._copy_failed:
            raise self.refusal()
        self._failed = False
        self.remaining()

    def apply_headers(self, method: str, url: str) -> dict[str, str]:
        from google.auth.credentials import TokenState

        self.remaining()
        headers = {"Accept-Encoding": "identity"}
        try:
            self._credential.before_request(self._request, method, url, headers)
        except BaseException:
            # SDK middleware also observes its public worker. An escaping error
            # may poison that witness; do not inspect the same Thread again.
            with self._entry:
                for lane in self._copies:
                    lane._barred = True
                    if lane._worker is not None:
                        lane._completion_unconfirmed = True
            raise
        self.remaining()
        self.retire_responses()
        if self._failed or self.cleanup_incomplete:
            raise self.refusal()
        if self._credential.token_state not in (TokenState.STALE, TokenState.FRESH):
            raise self.refusal()
        self.remaining()
        return headers

    def retire_responses(self, *, retry: bool = False) -> None:
        if self._request is not None:
            self._request.retire_responses(retry=retry)

    def retire_copies(self, deadline: Deadline, *, final: bool = False) -> None:
        """Bar entry first; join only positively observed consumers outside the barrier."""
        control: BaseException | None = None
        with self._entry:
            candidates = []
            for lane in self._copies:
                if lane._completion_unconfirmed:
                    lane._barred = True
                    continue
                worker = lane._worker
                expired = lane._deadline is not None and lane._deadline.remaining() == 0
                retire = final or lane._barred or (expired and worker is None)
                if not retire and worker is not None:
                    try:
                        retire = not worker.is_alive()
                    except BaseException as error:
                        lane._completion_unconfirmed = True
                        lane._barred = True
                        if control is None:
                            control = error
                        continue
                if retire:
                    lane._barred = True
                    candidates.append(lane)
        try:
            for lane in candidates:
                try:
                    if lane._worker is not None:
                        timeout = min(provider_locator_remaining(deadline, vm_name=self._vm_name), TIMEOUT_MAX)
                        try:
                            lane._worker.join(timeout=timeout)
                            alive = lane._worker.is_alive()
                        except BaseException:
                            lane._completion_unconfirmed = True
                            raise
                        if alive:
                            lane._uncertain = True
                            continue
                except Exception:
                    lane._uncertain = True
                    continue
                except BaseException as error:
                    lane._uncertain = True
                    if control is None:
                        control = error
                    continue
                try:
                    lane.retire_responses(retry=final)
                except BaseException as error:
                    lane._uncertain = True
                    if control is None:
                        control = error
                try:
                    lane.close_session()
                except BaseException as error:
                    lane._uncertain = True
                    if control is None:
                        control = error
                if lane.session is None and not lane._responses:
                    self._copies.remove(lane)
        except BaseException:
            if control is not None:
                raise control from None
            raise
        if control is not None:
            raise control

    def bar_entry(self) -> None:
        with self._entry:
            self.closed = True

    def close(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        if not self._operation.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            raise LimitExceededError("GCP authentication close exceeded its deadline")
        control: BaseException | None = None
        try:
            self.bar_entry()
            try:
                self.retire_copies(deadline, final=True)
            except BaseException as error:
                control = error
            if self._request is not None:
                try:
                    self._request.retire_responses(retry=True)
                except BaseException as error:
                    if control is None:
                        control = error
                try:
                    self._request.close_session()
                except BaseException as error:
                    if control is None:
                        control = error
            if control is not None:
                raise control
        finally:
            try:
                self._operation.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise


def _service_account(auth: GcpServiceAccountAuth, secret: str, site_name: str) -> Any:
    """Build the selected complete JSON document with detached safe errors."""
    from google.oauth2 import service_account

    info: object | None = None
    invalid = False
    try:
        info = json.loads(secret)
    except (ValueError, TypeError, RecursionError):
        invalid = True
    if invalid or not isinstance(info, dict):
        raise _service_account_error(auth, site_name, "is not one complete JSON object")
    credential: Any = None
    try:
        factory = cast("Callable[..., Any]", service_account.Credentials.from_service_account_info)
        credential = factory(info, scopes=(_CLOUD_PLATFORM_SCOPE,))
    except Exception:
        invalid = True
    if invalid or credential is None:
        raise _service_account_error(auth, site_name, "is not a valid Google service-account document")
    return credential


def _service_account_error(auth: GcpServiceAccountAuth, site_name: str, reason: str) -> ProvisioningError:
    return ProvisioningError(
        f"could not authenticate vm-site '{site_name}': secret '{auth.secret}' {reason}",
        entity_kind="vm-site",
        entity_name=site_name,
        hint=(
            f"store the complete service-account key JSON in secret '{auth.secret}' exactly as downloaded; "
            "do not compact it or split credential fields into the vm-site"
        ),
    )
