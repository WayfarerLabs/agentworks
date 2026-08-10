"""Azure credential construction and SDK-log policy."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.plugins.azure.network import AzureError

if TYPE_CHECKING:
    from agentworks.plugins.azure.config import AzureServicePrincipalAuth

_AZURE_IDENTITY_LOGGER = "azure.identity"
_ARM_SCOPE = "https://management.azure.com/.default"


def _quiet_azure_identity_logging() -> None:
    """Keep azure-identity's credential-failure warning off stderr.

    Agentworks configures no logging handlers, so Python's last-resort handler
    prints WARNING+ directly. Raising only the ``azure.identity`` logger's
    threshold leaves the typed Agentworks error as the one operator-facing
    failure. Debug mode retains the SDK detail. The setting covers both the
    initial probe and lazy token requests and is safe to apply on every Azure
    platform construction.
    """
    if os.environ.get("AGW_DEBUG") == "1":
        return
    logging.getLogger(_AZURE_IDENTITY_LOGGER).setLevel(logging.ERROR)


def _build_ambient_credential() -> object:
    """Build the ambient credential, falling back to browser login.

    ``DefaultAzureCredential`` is probed once with a real ARM token request.
    Success returns that credential; a chain that cannot authenticate falls
    back to ``InteractiveBrowserCredential``, whose interaction remains lazy.
    The return type avoids importing Azure SDK types at module load time.
    """
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

    credential = DefaultAzureCredential()
    try:
        credential.get_token(_ARM_SCOPE)
        return credential
    except ClientAuthenticationError:
        output.info("No Azure credentials found, opening browser for login...")
        return InteractiveBrowserCredential()


def _build_service_principal_credential(
    service_principal: AzureServicePrincipalAuth,
    client_secret: str,
    site_name: str,
) -> object:
    """Build and probe the site's explicit service-principal credential.

    The selected service principal never falls back to ambient credentials.
    Construction and the ARM probe share one typed failure boundary so a bad
    or empty resolved secret carries the site, secret name, and remediation
    without exposing the value. Azure Identity does not distinguish an Entra
    rejection from an unreachable token service, so this remains an
    ``AzureError`` rather than the more specific ``TokenRejectedError``.
    """
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import ClientSecretCredential

    try:
        credential = ClientSecretCredential(
            service_principal.tenant_id,
            service_principal.client_id,
            client_secret,
        )
        credential.get_token(_ARM_SCOPE)
    except (ClientAuthenticationError, ValueError) as exc:
        raise AzureError(
            f"could not authenticate the Azure service principal for "
            f"vm-site '{site_name}' (client {service_principal.client_id} in tenant "
            f"{service_principal.tenant_id}, secret '{service_principal.secret}')",
            detail=str(exc),
            entity_kind="vm-site",
            entity_name=site_name,
            hint=(
                f"Check auth.tenant_id / auth.client_id and the value of the "
                f"'{service_principal.secret}' secret (an expired client secret is the usual cause; "
                "`az ad app credential list` shows expiry). If Entra ID is simply unreachable this fails "
                "the same way, because azure-identity reports both as an authentication failure."
            ),
        ) from exc
    return credential
