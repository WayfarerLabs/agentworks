"""Pure provider-locator framing for already validated Proxmox facts."""

import hashlib
import json

from agentworks.capabilities.vm_platform.base import ProviderLocator


def proxmox_provider_locator(api_url: str, vmid: int, generation: str) -> ProviderLocator:
    """Frame exact public facts; observation callers validate the live generation."""
    framed = json.dumps(
        ("agentworks.proxmox.provider-locator.v1", api_url, vmid, generation),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return ProviderLocator("proxmox:v1:" + hashlib.sha256(framed).hexdigest())
