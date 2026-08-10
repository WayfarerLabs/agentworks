"""Secret-free Google API and extended-operation error translation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.errors import ProvisioningError

if TYPE_CHECKING:
    from collections.abc import Callable


class GCEError(ProvisioningError):
    """A sanitized Compute Engine provider failure."""


class GCEAuthenticationError(GCEError):
    """Google could not construct or accept the selected identity."""


class GCEPermissionError(GCEError):
    """The selected identity lacks a required Google Cloud permission."""


class GCENotFoundError(GCEError):
    """A requested Google Cloud resource does not exist."""


class GCEConflictError(GCEError):
    """A provider resource collides with an expected owned identity."""


class GCEQuotaError(GCEError):
    """Google Cloud refused an operation because quota is exhausted."""


class GCEOperationError(GCEError):
    """A bounded Compute Engine operation failed or did not finish."""


def google_error(exc: Exception, *, operation: str, resource: str | None = None) -> GCEError:
    """Map one Google exception without retaining its text or object.

    Provider exceptions can retain credentials, request bodies, or reflected
    server text. The returned error contains only caller-authored labels.
    """
    from google.api_core import exceptions as api_exceptions

    target = f" for {resource}" if resource else ""
    common = {
        "entity_kind": "gcp-resource" if resource else None,
        "entity_name": resource,
    }
    if isinstance(exc, api_exceptions.Unauthenticated):
        return GCEAuthenticationError(
            f"Google Cloud authentication failed while {operation}{target}",
            hint="check the vm-site auth mode and selected credential",
            **common,
        )
    if isinstance(exc, api_exceptions.PermissionDenied):
        return GCEPermissionError(
            f"Google Cloud denied permission while {operation}{target}",
            hint="grant the selected identity the documented Compute Engine permissions",
            **common,
        )
    if isinstance(exc, api_exceptions.NotFound):
        return GCENotFoundError(f"Google Cloud resource was not found while {operation}{target}", **common)
    if isinstance(exc, (api_exceptions.AlreadyExists, api_exceptions.Conflict)):
        return GCEConflictError(f"Google Cloud resource already exists while {operation}{target}", **common)
    if isinstance(exc, api_exceptions.ResourceExhausted):
        return GCEQuotaError(
            f"Google Cloud quota was exhausted while {operation}{target}",
            hint="review the project's Compute Engine quotas before retrying",
            **common,
        )
    if isinstance(exc, (api_exceptions.DeadlineExceeded, TimeoutError)):
        return GCEOperationError(
            f"Google Cloud did not finish while {operation}{target}",
            hint="inspect the named resource before retrying because the operation outcome is indeterminate",
            **common,
        )
    return GCEError(
        f"Google Cloud failed while {operation}{target}",
        hint="inspect the Agentworks debug log and the named Google Cloud resource before retrying",
        **common,
    )


def call_google[T](call: Callable[[], T], *, operation: str, resource: str | None = None) -> T:
    """Invoke one provider call and raise a detached sanitized error."""
    failure: GCEError | None = None
    try:
        return call()
    except Exception as exc:
        failure = google_error(exc, operation=operation, resource=resource)
    raise failure


def call_google_optional[T](call: Callable[[], T], *, operation: str, resource: str) -> T | None:
    """Invoke one provider get, returning ``None`` only for not-found."""
    failure: GCEError | None = None
    try:
        return call()
    except Exception as exc:
        mapped = google_error(exc, operation=operation, resource=resource)
        if isinstance(mapped, GCENotFoundError):
            return None
        failure = mapped
    raise failure


def wait_for_extended_operation(operation: Any, *, label: str, timeout: float) -> None:
    """Wait once for a Compute extended operation and sanitize failure data."""
    call_google(
        lambda: operation.result(timeout=timeout),
        operation=f"waiting for {label}",
        resource=label,
    )
    if getattr(operation, "error_code", None):
        raise GCEOperationError(
            f"Google Cloud operation failed while waiting for {label}",
            entity_kind="gcp-resource",
            entity_name=label,
        )
