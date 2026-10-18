"""Public network SDK pipeline for one literal ARM request transmission."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential
    from azure.core.pipeline import PipelineRequest, PipelineResponse
    from azure.core.pipeline.transport import HttpTransport
    from azure.core.rest import HttpRequest, HttpResponse
    from azure.mgmt.network import NetworkManagementClient


def network_route_client(
    credential: object, subscription_id: str, *, transport: HttpTransport[HttpRequest, HttpResponse] | None = None
) -> NetworkManagementClient:
    """Omit retry, redirect, registration and authentication-challenge replay."""
    from azure.core.pipeline.policies import BearerTokenCredentialPolicy
    from azure.mgmt.network import NetworkManagementClient

    class RefuseChallenge(BearerTokenCredentialPolicy["HttpRequest", "HttpResponse"]):
        def on_challenge(
            self, request: PipelineRequest[HttpRequest], response: PipelineResponse[HttpRequest, HttpResponse]
        ) -> bool:
            return False

    return NetworkManagementClient(
        cast("TokenCredential", credential),
        subscription_id,
        policies=[RefuseChallenge(cast("TokenCredential", credential), "https://management.azure.com/.default")],
        transport=transport,
    )
