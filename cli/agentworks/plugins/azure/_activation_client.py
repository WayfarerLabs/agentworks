"""Lazy public SDK construction for a single ARM mutation dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential
    from azure.core.pipeline import PipelineRequest, PipelineResponse
    from azure.core.pipeline.transport import HttpTransport
    from azure.core.rest import HttpRequest, HttpResponse
    from azure.mgmt.compute import ComputeManagementClient


def compute_start_client(
    credential: object, subscription_id: str, *, transport: HttpTransport[HttpRequest, HttpResponse] | None = None
) -> ComputeManagementClient:
    """Construct fresh policies without challenge replay, redirects or registration."""
    from azure.core.pipeline.policies import BearerTokenCredentialPolicy
    from azure.mgmt.compute import ComputeManagementClient

    class RefuseChallenge(BearerTokenCredentialPolicy["HttpRequest", "HttpResponse"]):
        def on_challenge(
            self, request: PipelineRequest[HttpRequest], response: PipelineResponse[HttpRequest, HttpResponse]
        ) -> bool:
            return False

    return ComputeManagementClient(
        cast("TokenCredential", credential),
        subscription_id,
        policies=[
            RefuseChallenge(cast("TokenCredential", credential), "https://management.azure.com/.default"),
        ],
        transport=transport,
    )
