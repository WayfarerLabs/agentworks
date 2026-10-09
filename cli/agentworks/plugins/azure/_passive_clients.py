"""ARM clients for exact passive reads, separate from lifecycle clients.

The default SDK pipeline registers resource providers after some failed GETs.
These public policies deliberately omit registration and redirect following.
ARM credential challenges can still resend a GET; request budgets and callers'
late-result checks remain best effort, not a hard total deadline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from azure.core.pipeline.policies import RetryPolicy
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.core.policies import ARMChallengeAuthenticationPolicy
from azure.mgmt.network import NetworkManagementClient

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential
    from azure.core.pipeline.transport import HttpRequest as LegacyHttpRequest
    from azure.core.pipeline.transport import HttpResponse as LegacyHttpResponse
    from azure.core.pipeline.transport import HttpTransport
    from azure.core.rest import HttpRequest, HttpResponse


def compute_read_client(
    credential: object,
    subscription_id: str,
    *,
    transport: HttpTransport[HttpRequest | LegacyHttpRequest, HttpResponse | LegacyHttpResponse] | None = None,
) -> ComputeManagementClient:
    return ComputeManagementClient(
        cast("TokenCredential", credential), subscription_id, policies=_read_policies(credential), transport=transport
    )


def network_read_client(
    credential: object,
    subscription_id: str,
    *,
    transport: HttpTransport[HttpRequest | LegacyHttpRequest, HttpResponse | LegacyHttpResponse] | None = None,
) -> NetworkManagementClient:
    return NetworkManagementClient(
        cast("TokenCredential", credential), subscription_id, policies=_read_policies(credential), transport=transport
    )


def _read_policies(
    credential: object,
) -> list[RetryPolicy[HttpRequest, HttpResponse] | ARMChallengeAuthenticationPolicy]:
    return [
        RetryPolicy(retry_total=0, retry_connect=0, retry_read=0, retry_status=0),
        ARMChallengeAuthenticationPolicy(cast("TokenCredential", credential), "https://management.azure.com/.default"),
    ]
