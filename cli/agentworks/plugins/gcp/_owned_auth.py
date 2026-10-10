"""Selected GCP credentials and attributable public Request originals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from google.auth.transport.requests import Request

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.errors import AgentworksError, ProvisioningError, StateError, ValidationError
from agentworks.plugins.gcp.config import GcpAmbientAuth, GcpServiceAccountAuth

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp.config import GcpAuth

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_MAX_CREDENTIAL_RESPONSES = 31


class _OwnedRequest(Request):
    """Real public Request with its owner's active original observation budget.

    Keep the public session and certificate arguments available to credentials.
    SDK-created requests, discovery processes and constructor handoff are not
    retroactively owned by this supplied transport.
    """

    def __init__(self, owner: _OwnedAuth, session: Any) -> None:
        initialize = cast("Callable[..., None]", super().__init__)
        initialize(session=session)
        self._owner = owner

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        timeout: Any = 120,
        **kwargs: Any,
    ) -> Any:
        self._owner.remaining()
        self._owner.retire_responses()
        if self._owner.cleanup_incomplete:
            raise self._owner.refusal()
        remaining = self._owner.remaining()
        if timeout is None:
            bounded: Any = remaining
        elif isinstance(timeout, tuple) and len(timeout) == 2:
            bounded = tuple(self._cap(value, remaining) for value in timeout)
        else:
            bounded = self._cap(timeout, remaining)
        invoke = cast("Callable[..., Any]", super().__call__)
        response = invoke(url, method=method, body=body, headers=headers, timeout=bounded, **kwargs)
        self._owner.remaining()
        return response

    @staticmethod
    def _cap(value: Any, remaining: float) -> float:
        if value is None:
            return remaining
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < float("inf"):
            raise ValidationError("GCP credential request timeout is invalid")
        return min(float(value), remaining)


class _OwnedAuth:
    """Credential dependency and bounded responses, serialized by the reader.

    An active session is a dependency, not failed cleanup. There is no second
    terminal state: the reader closes admission and supplies final-close context.
    """

    def __init__(self, vm_name: str) -> None:
        self._vm_name = vm_name
        self._deadline: Deadline | None = None
        self._credential: Any = None
        self._session: Any = None
        self._request: _OwnedRequest | None = None
        self._responses: list[Any] = []
        self._responses_uncertain = False
        self._failed = False

    @property
    def cleanup_incomplete(self) -> bool:
        return bool(self._responses)

    @property
    def session_retained(self) -> bool:
        return self._session is not None

    def refusal(self) -> StateError:
        return StateError(
            "Selected GCP credential work cannot be admitted",
            entity_kind="vm",
            entity_name=self._vm_name,
        )

    def begin(self, deadline: Deadline) -> None:
        if self._deadline is not None or self._failed or self.cleanup_incomplete:
            raise self.refusal()
        provider_locator_remaining(deadline, vm_name=self._vm_name)
        self._deadline = deadline

    def end(self) -> None:
        self._deadline = None

    def remaining(self) -> float:
        if self._deadline is None:
            raise self.refusal()
        return provider_locator_remaining(self._deadline, vm_name=self._vm_name)

    def _retain_response(self, response: Any, **kwargs: Any) -> None:
        # Stock Session permits 30 redirects and their final response. Retain an
        # unexpected overflow original before refusing; never drop its custody.
        self._responses.append(response)
        if len(self._responses) > _MAX_CREDENTIAL_RESPONSES:
            raise self.refusal()

    def acquire(self, auth: GcpAuth, ctx: RunContext, site_name: str) -> None:
        self.remaining()
        if self._credential is not None:
            return
        # A failed partial acquisition cannot silently construct a second original.
        self._failed = True
        import requests  # type: ignore[import-untyped]

        self._session = requests.Session()
        self._session.trust_env = False
        self._session.hooks["response"].append(self._retain_response)
        self._request = _OwnedRequest(self, self._session)
        self.remaining()
        if isinstance(auth, GcpAmbientAuth):
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
                    f"could not construct Application Default Credentials for vm-site '{site_name}'",
                    entity_kind="vm-site",
                    entity_name=site_name,
                    hint="configure Application Default Credentials or select auth.mode service-account",
                )
            if failure is not None:
                raise failure
            if self._credential is None:
                raise self.refusal()
        else:
            self._credential = _service_account(auth, ctx.secret(auth.secret), site_name)
        self._failed = False
        self.remaining()

    def apply_headers(self) -> dict[str, str]:
        from google.auth.credentials import TokenState

        self.remaining()
        if self._credential.token_state in (TokenState.STALE, TokenState.INVALID):
            self._credential.refresh(self._request)
        self.remaining()
        self.retire_responses()
        if self.cleanup_incomplete:
            raise self.refusal()
        if self._credential.token_state not in (TokenState.STALE, TokenState.FRESH):
            raise self.refusal()
        headers = {"Accept-Encoding": "identity"}
        self._credential.apply(headers)
        self.remaining()
        return headers

    def retire_responses(self, *, retry: bool = False) -> None:
        """Retire each actual response only after its SDK consumer returned."""
        if self._responses_uncertain and not retry:
            return
        control: BaseException | None = None
        try:
            for response in tuple(self._responses):
                try:
                    response.close()
                except Exception:
                    self._responses_uncertain = True
                except BaseException as error:
                    self._responses_uncertain = True
                    if control is None:
                        control = error
                else:
                    self._responses.remove(response)
        except BaseException:
            if control is not None:
                raise control from None
            raise
        self._responses_uncertain = bool(self._responses)
        if control is not None:
            raise control

    def close_session(self) -> None:
        """Retry only the retained original through its public close method."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            else:
                self._session = None


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
