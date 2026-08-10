"""Secret-free Google API and extended-operation error translation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    AuthorizationError,
    ConnectivityError,
    NotFoundError,
    ProvisioningError,
    TokenRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class GCEError(ProvisioningError):
    """A sanitized Compute Engine provider failure."""


class GCEQuotaError(GCEError):
    """Google Cloud refused an operation because quota is exhausted."""


class GCEOperationError(GCEError):
    """A bounded Compute Engine operation failed or did not finish."""


def google_error(exc: Exception, *, operation: str, resource: str | None = None) -> AgentworksError:
    """Map one Google exception without retaining its text or object.

    Provider exceptions can retain credentials, request bodies, or reflected
    server text. The returned error contains only caller-authored labels.
    """
    from google.api_core import exceptions as api_exceptions
    from google.auth import exceptions as auth_exceptions

    target = f" for {resource}" if resource else ""
    common = {
        "entity_kind": "gcp-resource" if resource else None,
        "entity_name": resource,
    }
    if isinstance(exc, auth_exceptions.RefreshError | api_exceptions.Unauthenticated):
        return TokenRejectedError(
            f"Google Cloud rejected the selected credential while {operation}{target}",
            hint="check the vm-site auth mode and selected credential",
            **common,
        )
    if isinstance(exc, api_exceptions.PermissionDenied):
        return AuthorizationError(
            f"Google Cloud denied permission while {operation}{target}",
            hint="grant the selected identity the documented Compute Engine permissions",
            **common,
        )
    if isinstance(exc, api_exceptions.NotFound):
        return NotFoundError(f"Google Cloud resource was not found while {operation}{target}", **common)
    if isinstance(exc, (api_exceptions.AlreadyExists, api_exceptions.Conflict)):
        return AlreadyExistsError(f"Google Cloud resource already exists while {operation}{target}", **common)
    if isinstance(exc, api_exceptions.ResourceExhausted):
        return GCEQuotaError(
            f"Google Cloud quota was exhausted while {operation}{target}",
            hint="review the project's Compute Engine quotas before retrying",
            **common,
        )
    if isinstance(
        exc,
        (
            auth_exceptions.TransportError,
            auth_exceptions.TimeoutError,
            api_exceptions.BadGateway,
            api_exceptions.DeadlineExceeded,
            api_exceptions.GatewayTimeout,
            api_exceptions.ServiceUnavailable,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return ConnectivityError(
            f"Google Cloud was unreachable while {operation}{target}",
            hint="check network access to Google Cloud and retry the operation",
            **common,
        )
    return GCEError(
        f"Google Cloud failed while {operation}{target}",
        hint="inspect the Agentworks debug log and the named Google Cloud resource before retrying",
        **common,
    )


def call_google[T](call: Callable[[], T], *, operation: str, resource: str | None = None) -> T:
    """Invoke one provider call and raise a detached sanitized error."""
    failure: AgentworksError | None = None
    try:
        return call()
    except Exception as exc:
        failure = google_error(exc, operation=operation, resource=resource)
    raise failure


def call_google_optional[T](call: Callable[[], T], *, operation: str, resource: str) -> T | None:
    """Invoke one provider get, returning ``None`` only for not-found."""
    failure: AgentworksError | None = None
    try:
        return call()
    except Exception as exc:
        mapped = google_error(exc, operation=operation, resource=resource)
        if isinstance(mapped, NotFoundError):
            return None
        failure = mapped
    raise failure


def wait_for_extended_operation(operation: Any, *, label: str, timeout: float) -> None:
    """Wait once for a Compute extended operation and sanitize failure data."""
    failure: AgentworksError | None = None
    try:
        operation.result(timeout=timeout)
    except Exception as exc:
        mapped = google_error(exc, operation=f"waiting for {label}", resource=label)
        if isinstance(mapped, ConnectivityError) or type(mapped) is GCEError:
            failure = GCEOperationError(
                f"Google Cloud operation did not complete while waiting for {label}",
                entity_kind="gcp-resource",
                entity_name=label,
                hint="inspect the named resource before retrying because the operation outcome is indeterminate",
            )
        else:
            failure = mapped
    if failure is not None:
        raise failure
    if getattr(operation, "error_code", None):
        raise GCEOperationError(
            f"Google Cloud operation failed while waiting for {label}",
            entity_kind="gcp-resource",
            entity_name=label,
        )
