"""The Azure plugin's typed provider error and the translation of Azure SDK exceptions into it."""

from __future__ import annotations

from agentworks.errors import AgentworksError, ProvisioningError


class AzureError(ProvisioningError):
    """An Azure API operation failed.

    Attributes:
        summary: A concise, user-facing error message.
        detail: The full error details (for logs).

    The optional entity / hint keywords are the base ``AgentworksError``
    ones, forwarded so a failure that KNOWS which resource it is about
    can say so (the service-principal credential build names its site and
    its secret). :func:`wrap_azure_error`, which converts an arbitrary
    exception, has no such knowledge and passes only the hint of an
    Agentworks error it wraps.
    """

    def __init__(
        self,
        summary: str,
        detail: str,
        *,
        entity_kind: str | None = None,
        entity_name: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(summary, entity_kind=entity_kind, entity_name=entity_name, hint=hint)
        self.summary = summary
        self.detail = detail


def wrap_azure_error(exc: Exception) -> AzureError:
    """Convert an Azure SDK exception into an AzureError, keeping a wrapped Agentworks error's hint."""
    from azure.core.exceptions import HttpResponseError

    if isinstance(exc, HttpResponseError):
        # Walk inner errors to find the most specific message
        code = exc.error.code if exc.error else None
        message = exc.error.message if exc.error else str(exc)

        if exc.error and exc.error.details:
            inner = exc.error.details[0]
            code = inner.code or code
            message = inner.message or message

        summary = f"{code}: {_trim_message(str(message))}" if code else _trim_message(str(message))
        return AzureError(summary, detail=str(exc))

    return AzureError(str(exc), detail=str(exc), hint=exc.hint if isinstance(exc, AgentworksError) else None)


def _trim_message(message: str) -> str:
    """Trim an Azure error message to the first meaningful sentence."""
    # Cut at first URL or "Learn more" / "Submit a request" noise
    for marker in [". Setup Alerts", ". Learn more", ". Submit a request", " https://"]:
        idx = message.find(marker)
        if idx != -1:
            return message[: idx + 1] if marker.startswith(".") else message[:idx]
    return message
