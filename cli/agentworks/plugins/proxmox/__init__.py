"""The ``proxmox`` system plugin: the Proxmox VE VM platform, shipped as a
separable, opt-in plugin (R11, R11.1).

A capability-only migration (Phase 10, no bundled manifests): the plugin
seats its ``ProxmoxPlatform`` into ``VM_PLATFORM_REGISTRY`` through the
``vm-platform`` adapter and publishes a ``vm-platform`` row with a
``system-plugin`` origin. The row is present-but-disabled until an operator
opts in with ``[plugins] system = ["proxmox"]``; while disabled a
``vm-site`` on the ``proxmox`` platform is not-ready with the "enable plugin
`proxmox`" hint and ``resolve_site`` refuses it. The deprecated legacy
``[proxmox]`` flat-section site gets the same hint (a feature: legacy configs
are guided, not broken with an unknown-name error).

The platform's REST client (``api.py``, formerly the ``proxmox_api.py``
sibling in the core capability package) ships alongside as a package sibling,
as does the teardown plumbing (``teardown.py``, the stop-then-delete sequence
shared by the delete op and create's rollback arms).
``base`` / ``bootstrap_script`` / ``cloud_init`` stay in the core
``vm-platform`` capability package: ``base`` is the platform contract every
platform extends, and ``bootstrap_script`` / ``cloud_init`` are shared with
both the core VM initializer and the azure platform, so they are core
machinery, not proxmox's to carry.
"""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

PLUGIN = Plugin(
    name="proxmox",
    description="Proxmox VE VM platform",
    capabilities={"vm-platform": (ProxmoxPlatform,)},
)
