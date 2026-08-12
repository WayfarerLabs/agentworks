"""Value-free guards for secret consumers whose syntax is one line."""

from __future__ import annotations

from enum import StrEnum

from agentworks.errors import ValidationError


class LineOrientedSecretUse(StrEnum):
    """The reviewed line-oriented boundaries that consume secrets."""

    ENVIRONMENT = "environment injection"
    ENVIRONMENT_REVEAL = "environment reveal"
    GIT_CREDENTIAL = "Git authentication and credential storage"
    PROXMOX_API = "Proxmox API authentication"
    TAILSCALE = "Tailscale authentication"


def require_line_safe_secret(
    value: str,
    *,
    use: LineOrientedSecretUse,
    secret_name: str | None = None,
) -> str:
    """Return ``value`` or reject syntax-breaking CR, LF, and NUL.

    Secret resolution intentionally treats carriage returns and line feeds as
    opaque content. Consumers whose protocols use one logical line call this
    pure guard immediately after delivery and again at their final material
    sink where useful. The failure contains only the secret reference, when
    known, and fixed consumer-owned text.
    """
    if "\r" not in value and "\n" not in value and "\0" not in value:
        return value

    subject = f"secret {secret_name!r}" if secret_name is not None else "the resolved secret"
    raise ValidationError(
        f"{subject} cannot be used for {use.value}",
        entity_kind="secret" if secret_name is not None else None,
        entity_name=secret_name,
        hint=("Use a value without carriage returns, line feeds, or NUL for this line-oriented consumer."),
    ) from None
