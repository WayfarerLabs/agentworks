"""The opt-in ``gcp`` vendor bundle and its Compute Engine contribution.

The bundle owns Google-specific composition, while each service implementation
keeps the existing capability contract it implements. Today that is the
``gcp-gce`` VM platform plus the optional guest-side ``gcloud-cli`` installer;
future GCP capabilities join this bundle under their own names rather than
through a provider-wide abstraction.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.gcp.platform import GCEPlatform

PLUGIN = Plugin(
    name="gcp",
    description="Google Compute Engine VM platform and optional gcloud CLI",
    capabilities={"vm-platform": (GCEPlatform,)},
    manifests="agentworks.plugins.gcp",
)
