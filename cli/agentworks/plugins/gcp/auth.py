"""GCP credential construction and one-client-per-kind caching."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

from agentworks.errors import ProvisioningError
from agentworks.plugins.gcp.config import GcpAmbientAuth, GcpGCEConfig, GcpServiceAccountAuth

if TYPE_CHECKING:
    from collections.abc import Callable

    from google.auth.credentials import Credentials

    from agentworks.capabilities.base import RunContext

type GcpClientKind = Literal[
    "projects",
    "zones",
    "networks",
    "subnetworks",
    "machine-types",
    "disk-types",
    "images",
    "instances",
    "firewalls",
]

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def build_ambient_credential(site_name: str) -> Credentials:
    """Build Application Default Credentials with the cloud-platform scope."""
    import google.auth

    failure = False
    credential: Credentials | None = None
    try:
        credential, _detected_project = google.auth.default(scopes=(_CLOUD_PLATFORM_SCOPE,))
    except Exception:
        failure = True
    if failure or credential is None:
        raise ProvisioningError(
            f"could not construct Application Default Credentials for vm-site '{site_name}'",
            entity_kind="vm-site",
            entity_name=site_name,
            hint="configure Application Default Credentials or select auth.mode service-account",
        )
    return credential


def build_service_account_credential(
    auth: GcpServiceAccountAuth,
    secret_value: str,
    site_name: str,
) -> Credentials:
    """Build one credential from a complete service-account JSON secret.

    The raw string and parsed mapping remain local and are never attached to
    the returned platform state or any raised exception.
    """
    from google.oauth2 import service_account

    info: object | None = None
    invalid_json = False
    try:
        info = json.loads(secret_value)
    except (ValueError, TypeError, RecursionError):
        invalid_json = True

    if invalid_json or not isinstance(info, dict):
        raise _service_account_error(auth, site_name, "is not one complete JSON object")

    credential: Credentials | None = None
    invalid_document = False
    try:
        factory = cast("Callable[..., Credentials]", service_account.Credentials.from_service_account_info)
        credential = factory(
            info,
            scopes=(_CLOUD_PLATFORM_SCOPE,),
        )
    except Exception:
        invalid_document = True
    if invalid_document or credential is None:
        raise _service_account_error(auth, site_name, "is not a valid Google service-account document")
    return credential


def _service_account_error(
    auth: GcpServiceAccountAuth,
    site_name: str,
    reason: str,
) -> ProvisioningError:
    return ProvisioningError(
        f"could not authenticate vm-site '{site_name}': secret '{auth.secret}' {reason}",
        entity_kind="vm-site",
        entity_name=site_name,
        hint=(
            f"store the complete service-account key JSON in secret '{auth.secret}' exactly as downloaded; "
            "do not compact it or split credential fields into the vm-site"
        ),
    )


class GcpClientCache:
    """Credential and concrete Compute clients for one bound vm-site.

    Exactly one successful credential build and one client construction per
    concrete kind are retained. No context, secret value, or parsed JSON is
    stored.
    """

    def __init__(self, site_name: str, config: GcpGCEConfig) -> None:
        self._site_name = site_name
        self._config = config
        self._credential_cached: Credentials | None = None
        self._clients: dict[GcpClientKind, Any] = {}

    def credential(self, ctx: RunContext) -> Credentials:
        """Return the selected credential, constructing it at most once."""
        if self._credential_cached is not None:
            return self._credential_cached

        auth = self._config.auth
        if isinstance(auth, GcpAmbientAuth):
            credential = build_ambient_credential(self._site_name)
        else:
            credential = build_service_account_credential(
                auth,
                ctx.secret(auth.secret),
                self._site_name,
            )
        self._credential_cached = credential
        return credential

    def client(self, kind: GcpClientKind, ctx: RunContext) -> Any:
        """Return one cached typed Compute client for ``kind``."""
        cached = self._clients.get(kind)
        if cached is not None:
            return cached

        from google.cloud import compute_v1

        constructors: dict[GcpClientKind, type[Any]] = {
            "projects": compute_v1.ProjectsClient,
            "zones": compute_v1.ZonesClient,
            "networks": compute_v1.NetworksClient,
            "subnetworks": compute_v1.SubnetworksClient,
            "machine-types": compute_v1.MachineTypesClient,
            "disk-types": compute_v1.DiskTypesClient,
            "images": compute_v1.ImagesClient,
            "instances": compute_v1.InstancesClient,
            "firewalls": compute_v1.FirewallsClient,
        }
        credential = self.credential(ctx)
        construction_failed = False
        built: Any = None
        try:
            built = constructors[kind](credentials=credential)
        except Exception:
            construction_failed = True
        if construction_failed or built is None:
            auth = self._config.auth
            mode = auth.mode
            secret = f", secret '{auth.secret}'" if isinstance(auth, GcpServiceAccountAuth) else ""
            raise ProvisioningError(
                f"could not construct Google Compute client '{kind}' for vm-site '{self._site_name}' "
                f"(auth mode '{mode}'{secret})",
                entity_kind="vm-site",
                entity_name=self._site_name,
                hint="check the selected credential and Google client configuration",
            )
        self._clients[kind] = built
        return built
